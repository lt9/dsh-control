"""ng_panel.py：token 流、反代头铁律、幂等/续建、写后回读、防误删。MockTransport。

线上真实返回形状钉成脱敏 fixture（NG_*_FIXTURE）：读回解析 + 写参数白名单
（来源：部署版 smart-doc /api/server/insertOrUpdateLocation 参数表 + 上游
cym1102/nginxWebUI 的 Location 实体 / ConfService type=2 分支）。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from dsh_control.integrations.base import IntegrationError, NotManaged
from dsh_control.integrations.dry_run import DryRunRecorder
from dsh_control.integrations.ng_panel import (
    LOCATION_PARAM_FIELDS,
    NgPanel,
)

BASE = "http://203.0.113.10:8600"
SITE = "dsh-alpha.example.com"
UP_NAME = "DSH_ALPHA"
CERT = "*.example.com"


def run(coro):
    return asyncio.run(coro)


def ok(obj=None):
    return httpx.Response(200, json={"success": True, "status": "200", "msg": "", "obj": obj or {}})


# ------------------------------------------------- 线上形状 fixture（值已脱敏）
# 键集合与真面板逐一对应；id 换成假雪花号，IP 用 TEST-NET，域名 example.com。

SITE_FIXTURE = {
    "id": "1310000000000000001", "serverName": SITE, "listen": "443",
    "def": 0, "ipv6": 0, "rewrite": 1, "rewriteListen": "80", "ssl": 1,
    "http2": 2, "proxyProtocol": 0, "proxyType": 0, "proxyUpstreamId": None,
    "pem": f"/home/nginxWebUI/.acme.sh/{CERT}/fullchain.cer",
    "key": f"/home/nginxWebUI/.acme.sh/{CERT}/{CERT}.key",
    "pemStr": None, "keyStr": None, "enable": True, "descr": "dsh-control",
    "protocols": "TLSv1 TLSv1.1 TLSv1.2 TLSv1.3", "passwordId": None,
    "seq": "1310000000000000001", "denyAllow": 0, "denyId": None,
    "allowId": None, "createTime": 1792512721482, "updateTime": 1792512721482,
}
UPSTREAM_FIXTURE = {"id": "1310000000000000000", "name": UP_NAME, "tactics": "",
                    "proxyType": 0, "monitor": 0, "descr": "dsh-control",
                    "seq": "1310000000000000000"}
BACKEND_FIXTURE = {"id": "1310000000000000002", "upstreamId": "1310000000000000000",
                   "server": "198.51.100.10", "port": 4894, "status": "none",
                   "param": "", "monitorStatus": -1,
                   "createTime": 1792512721419, "updateTime": 1792512721419}
LOCATION_FIXTURE = {
    "id": "1310000000000000003", "serverId": "1310000000000000001", "path": "/",
    "type": 2, "locationParamJson": None, "returnUrl": "", "returnPath": 0,
    "value": None, "upstreamType": "http", "upstreamId": "1310000000000000000",
    "upstreamPath": "", "rootPath": None, "rootPage": None, "rootType": None,
    "header": 1, "headerHost": "$host", "websocket": 1, "cros": 0,
    "descr": "dsh-control", "createTime": 1792512721563,
    "updateTime": 1792512721563,
}


class FakeNg:
    """假面板：默认按 fixture 长；growth=True 时写完才“长出”对象（模拟真面板）。"""

    def __init__(self, *, server=None, upstream=None, backends=None, locations=None,
                 flaky_token=False, growth=False, check_fails=False):
        self.server = server
        self.upstream = upstream
        self.backends = backends or []
        self.locations = locations or []
        self.flaky_token = flaky_token
        self.growth = growth
        self.check_fails = check_fails
        self.token_calls = 0
        self.requests: list[httpx.Request] = []
        self.writes: list[tuple[str, dict]] = []

    @property
    def paths(self):
        return [r.url.path for r in self.requests]

    def q(self, path_end):
        for r in self.requests:
            if r.url.path.endswith(path_end):
                return dict(r.url.params)
        return None

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = dict(request.url.params)
        if path == "/token/getToken":
            self.token_calls += 1
            if self.flaky_token and self.token_calls == 1:
                pass  # 面板重启：下面 getPage 会把旧 token 打回 401
            return ok({"token": f"tok-{self.token_calls}",
                       "tokenTimeout": int((time.time() + 7200) * 1000)})
        if request.headers.get("token") is None:
            return httpx.Response(200, json={"success": False, "status": "401",
                                             "msg": "header中token不正确或已过期"})
        if path == "/api/server/getPage":
            rows = [self.server] if self.server and params.get("keywords", "") in \
                json.dumps(self.server, ensure_ascii=False) else []
            return ok({"count": len(rows), "curr": 1, "limit": 100, "records": rows})
        if path == "/api/upstream/getPage":
            rows = [self.upstream] if self.upstream else []
            return ok({"count": len(rows), "curr": 1, "limit": 100, "records": rows})
        if path == "/api/upstream/getServerByUpstreamId":
            return ok(self.backends)
        if path == "/api/server/getLocationByServerId":
            return ok(self.locations)
        if path == "/api/nginx/check":
            self.writes.append((path, params))
            if self.check_fails:
                return httpx.Response(200, json={"success": False, "status": "500",
                                                 "msg": "效验失败"})
            return ok("效验成功")
        if path.startswith("/api/") and ("insertOrUpdate" in path or path.endswith("/delete")
                                         or path in ("/api/nginx/replace", "/api/nginx/reload")):
            self.writes.append((path, params))
            if self.growth:
                if path.endswith("/upstream/insertOrUpdate"):
                    self.upstream = dict(UPSTREAM_FIXTURE)
                elif path.endswith("/upstream/insertOrUpdateServer"):
                    # 带 id 就是原地改这条，不带 id 才是新增一条
                    self.backends = [dict(BACKEND_FIXTURE,
                                          id=params.get("id") or BACKEND_FIXTURE["id"],
                                          server=params.get("server", ""),
                                          port=int(params.get("port") or 0))]
                elif path.endswith("/api/server/insertOrUpdate"):
                    self.server = dict(SITE_FIXTURE)
                elif path.endswith("/insertOrUpdateLocation"):
                    self.locations = [dict(
                        LOCATION_FIXTURE,
                        upstreamId=params.get("upstreamId", ""),
                        headerHost=params.get("headerHost", "$host"),
                        websocket=int(params.get("websocket", 1)))]
            return ok({})
        return httpx.Response(200, json={"success": False, "status": "404", "msg": "no route"})

    def panel(self, dry_run=False, recorder=None):
        client = httpx.AsyncClient(base_url=BASE,
                                   transport=httpx.MockTransport(self._handler))
        return NgPanel(base_url=BASE, username="u", password="p",
                       dry_run=dry_run, recorder=recorder, client=client)


def write_for(api, path_end):
    hits = [p for p in api.writes if p[0].endswith(path_end)]
    return hits[-1][1] if hits else None


# ------------------------------------------------------------------ 鉴权

def test_token_header_used_and_renewed_on_expiry():
    api = FakeNg(server=SITE_FIXTURE)
    panel = api.panel()

    async def go():
        await panel.find_site(SITE)
        first = panel._token
        panel._token_exp = 0  # 假装过期
        await panel.find_site(SITE)
        return first, panel._token

    a, b = run(go())
    assert a == "tok-1" and b == "tok-2"
    assert api.token_calls == 2
    for r in api.requests:
        if r.url.path.startswith("/api/"):
            assert r.headers.get("token")


# ------------------------------------------------------ fixture 解析（读形状）

def test_real_shape_find_site_expands_location_ids():
    api = FakeNg(server=dict(SITE_FIXTURE), locations=[dict(LOCATION_FIXTURE)])
    site = run(api.panel().find_site(SITE))
    assert site.server_id == 1310000000000000001
    assert site.location_id == 1310000000000000003      # 从 location 读回，不是 None
    assert site.upstream_id == 1310000000000000000      # proxyUpstreamId 为空也要能从 location 补
    assert site.header_host == "$host" and site.websocket is True


def test_half_baked_site_reports_location_missing():
    api = FakeNg(server=dict(SITE_FIXTURE), locations=[])   # 线上首轮事故形状
    site = run(api.panel().find_site(SITE))
    assert site.server_id and site.location_id is None   # 半成品必须能被识别


# ------------------------------------------------------------------ 反代头铁律

@pytest.mark.parametrize("bad_host", ["$host:$proxy_port", "$host:$server_port",
                                      "$host:8443", " $host : $proxy_port "])
def test_header_host_with_port_is_refused_before_any_request(bad_host):
    api = FakeNg()
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                    upstream_server="198.51.100.10:4894",
                                    cert_name=CERT, header_host=bad_host))
    assert ei.value.code == "header_host_port"
    assert api.requests == []   # 连 token 都不取，杜绝半写状态


def test_location_payload_field_set_is_whitelist_subset():
    api = FakeNg(growth=True)
    run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                upstream_server="198.51.100.10:4894",
                                cert_name=CERT))
    loc = write_for(api, "/server/insertOrUpdateLocation")
    assert set(loc) <= LOCATION_PARAM_FIELDS          # 白名单外零字段
    # smart-doc 全集里我们该显式给的都在（别把面板默认值当运气）
    assert loc["returnPath"] == "0" and loc["value"] == "" and loc["rootType"] == "0"
    assert loc["type"] == "2" and loc["upstreamType"] == "http"
    assert loc["headerHost"] == "$host" and "$proxy_port" not in loc["headerHost"]
    assert loc["websocket"] == "1" and loc["path"] == "/"


def test_foreign_site_names_never_touched():
    api = FakeNg()
    with pytest.raises(NotManaged):
        run(api.panel().ensure_site(server_name="chat.example.com", upstream_name=UP_NAME,
                                    upstream_server="198.51.100.10:4894"))
    with pytest.raises(NotManaged):
        run(api.panel().ensure_site(server_name="dsh.legacy-names.example.com",
                                    upstream_name=UP_NAME,
                                    upstream_server="198.51.100.10:4894"))
    with pytest.raises(NotManaged):
        run(api.panel().delete_site(9, expect_name="chat.example.com"))
    assert api.writes == []


def test_foreign_upstream_name_blocked():
    api = FakeNg()
    with pytest.raises(NotManaged):
        run(api.panel().delete_upstream(9, expect_name="AISERVER_DSH"))
    assert api.writes == []


# ------------------------------------------------------------------ 建站/续建

def test_ensure_site_fresh_creates_upstream_site_and_location():
    api = FakeNg(growth=True)   # 全空白，写完才长出
    site = run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                       upstream_server="198.51.100.10:4894",
                                       listen_port=443, cert_name=CERT))
    assert (site.server_id, site.upstream_id) == (1310000000000000001,
                                                  1310000000000000000)
    assert site.location_id == 1310000000000000003     # 回读确认后才带回
    srv = write_for(api, "/api/server/insertOrUpdate")
    assert srv["serverName"] == SITE and srv["listen"] == "443" and srv["ssl"] == "1"
    assert "fullchain.cer" in srv["pem"] and srv["key"].endswith(".key")
    assert write_for(api, "/api/upstream/insertOrUpdate")["name"] == UP_NAME
    back = write_for(api, "/upstream/insertOrUpdateServer")
    assert back["server"] == "198.51.100.10" and back["port"] == "4894"


def test_continuation_half_baked_writes_only_location():
    """线上现状：upstream/站点都在且一致、location 缺——重跑只许发一个写请求。"""
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=[dict(BACKEND_FIXTURE)], locations=[], growth=True)
    site = run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                       upstream_server="198.51.100.10:4894",
                                       cert_name=CERT))
    assert [p for p, _ in api.writes] == ["/api/server/insertOrUpdateLocation"]
    assert site.location_id == 1310000000000000003


def test_all_matching_writes_nothing():
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=[dict(BACKEND_FIXTURE)], locations=[dict(LOCATION_FIXTURE)])
    site = run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                       upstream_server="198.51.100.10:4894",
                                       cert_name=CERT))
    assert api.writes == []                             # 全一致：一个字都不写
    assert (site.server_id, site.location_id) == (1310000000000000001,
                                                  1310000000000000003)


def test_backend_address_change_updates_in_place_and_never_appends():
    """换了 frps 上游地址：必须带 id 原地改那一条。

    两条后端是轮询关系——留着旧地址就等于一半请求打到死地址上，而且这种坏法
    是间歇性的，最难查。真机就配错过一次（回环地址在云上根本不通）。
    """
    stale = dict(BACKEND_FIXTURE, server="198.51.100.9")
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=[stale], locations=[dict(LOCATION_FIXTURE)], growth=True)
    site = run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                       upstream_server="198.51.100.10:4894",
                                       cert_name=CERT))
    back = write_for(api, "/upstream/insertOrUpdateServer")
    assert back is not None and str(back["id"]) == BACKEND_FIXTURE["id"]  # 带 id 改
    assert back["server"] == "198.51.100.10"
    assert len(api.backends) == 1                                  # 没有第二条
    assert api.backends[0]["server"] == "198.51.100.10"
    assert site.server_id == 1310000000000000001


def test_two_stale_backends_refuse_without_any_write():
    """同时挂多条非本控制台地址的后端时不猜，直接报，且零写请求。"""
    two = [dict(BACKEND_FIXTURE, server="198.51.100.9"),
           dict(BACKEND_FIXTURE, id="1310000000000000099", server="198.51.100.11")]
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=two, locations=[dict(LOCATION_FIXTURE)])
    with pytest.raises(IntegrationError) as got:
        run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                    upstream_server="198.51.100.10:4894",
                                    cert_name=CERT))
    assert got.value.code == "ng_backend_drift"
    assert api.writes == []


def test_location_pointing_elsewhere_is_updated_with_its_id():
    wrong = dict(LOCATION_FIXTURE, upstreamId="9999")
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=[dict(BACKEND_FIXTURE)], locations=[wrong], growth=True)
    run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                upstream_server="198.51.100.10:4894",
                                cert_name=CERT))
    loc = write_for(api, "/server/insertOrUpdateLocation")
    assert loc["id"] == LOCATION_FIXTURE["id"]          # 带原 id 更新，不新建
    assert loc["upstreamId"] == UPSTREAM_FIXTURE["id"]


def test_write_then_reread_missing_location_raises_never_lies():
    """写请求发了但回读看不见（真面板拒收/落错站）→ 必须抛错并写清缺什么。"""
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=[dict(BACKEND_FIXTURE)], locations=[])  # 无 growth：写不进库

    async def go():
        await api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                      upstream_server="198.51.100.10:4894",
                                      cert_name=CERT)

    with pytest.raises(IntegrationError) as ei:
        run(go())
    assert ei.value.code == "ng_verify_failed"
    msg = str(ei.value)
    assert "已确认" in msg and "location" in msg        # 说清已建什么、缺什么
    # 且绝不会走到 reload——半库重建 conf 会把新站漏出去，宁可不生效
    assert "/api/nginx/reload" not in api.paths


def test_ensure_site_bad_upstream_server_format_rejected():
    api = FakeNg(server=SITE_FIXTURE, upstream=UPSTREAM_FIXTURE)
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                    upstream_server="198.51.100.10"))
    assert ei.value.code == "bad_upstream_server"


# ------------------------------------------------------------------ reload 三步

def test_reload_sequence_check_replace_then_reload():
    api = FakeNg()
    run(api.panel().reload())
    seq = [p for p, _ in api.writes]
    assert seq == ["/api/nginx/check", "/api/nginx/replace", "/api/nginx/reload"]


def test_reload_stops_when_check_fails():
    api = FakeNg(check_fails=True)
    with pytest.raises(IntegrationError):
        run(api.panel().reload())
    assert "/api/nginx/replace" not in api.paths      # conf 一个字节没动
    assert "/api/nginx/reload" not in api.paths


# ------------------------------------------------------------------ 删除纪律

def test_delete_site_id_name_mismatch_blocks():
    api = FakeNg(server={"id": "123", "serverName": "dsh-other.example.com",
                         "listen": "443", "ssl": 1, "proxyUpstreamId": ""})
    with pytest.raises(NotManaged):
        run(api.panel().delete_site(123, expect_name=SITE))
    assert write_for(api, "/api/server/delete") is None


def test_delete_site_happy_then_upstream():
    api = FakeNg(server=SITE_FIXTURE, upstream=UPSTREAM_FIXTURE)
    run(api.panel().delete_site(1310000000000000001, expect_name=SITE))
    run(api.panel().delete_upstream(1310000000000000000, expect_name=UP_NAME))
    assert write_for(api, "/api/server/delete") == {"id": "1310000000000000001"}
    assert write_for(api, "/api/upstream/delete") == {"id": "1310000000000000000"}


def test_delete_site_unknown_id_blocks():
    api = FakeNg(server=None)
    with pytest.raises(NotManaged):
        run(api.panel().delete_site(999, expect_name=SITE))
    assert write_for(api, "/api/server/delete") is None


# ------------------------------------------------------------------ dry_run

def test_dry_run_records_all_writes_and_sends_none():
    rec = DryRunRecorder()
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=[dict(BACKEND_FIXTURE)], locations=[])
    panel = api.panel(dry_run=True, recorder=rec)
    site = run(panel.ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                 upstream_server="198.51.100.10:4894",
                                 cert_name=CERT))
    run(panel.reload())
    assert site.server_id == 0                       # 合成对象
    assert api.writes == []                          # 一个写请求都没发
    actions = [e.action for e in rec.entries]
    # 站点/upstream/后端都已一致 → 不许记它们；缺的 location 必须记
    assert "server.insertOrUpdate" not in actions
    assert "upstream.insertOrUpdate" not in actions
    assert "server.insertOrUpdateLocation" in actions
    assert {"nginx.check", "nginx.replace", "nginx.reload"} <= set(actions)


def test_ensure_site_passes_token_header_on_writes():
    api = FakeNg(server=dict(SITE_FIXTURE), upstream=dict(UPSTREAM_FIXTURE),
                 backends=[dict(BACKEND_FIXTURE)], locations=[], growth=True)
    run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                upstream_server="198.51.100.10:4894",
                                cert_name=CERT))
    for r in api.requests:
        if r.url.path.startswith("/api/"):
            assert r.headers.get("token")
