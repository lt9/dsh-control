"""ng_panel.py：token 流、location 必带 $host+websocket、幂等、防误删。MockTransport。"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from dsh_control.integrations.base import IntegrationError, NotManaged
from dsh_control.integrations.dry_run import DryRunRecorder
from dsh_control.integrations.ng_panel import NgPanel

BASE = "http://203.0.113.10:8600"
SITE = "dsh-alpha.example.com"
UP_NAME = "DSH_ALPHA"


def run(coro):
    return asyncio.run(coro)


def ok(obj=None):
    return httpx.Response(200, json={"success": True, "status": "200", "msg": "", "obj": obj or {}})


class FakeNg:
    def __init__(self, *, server=None, upstream=None, backends=None, locations=None,
                 flaky_token=False):
        self.server = server          # dict 或 None
        self.upstream = upstream
        self.backends = backends or []
        self.locations = locations or []
        self.flaky_token = flaky_token
        self.token_calls = 0
        self.requests: list[httpx.Request] = []
        self.writes: list[tuple[str, dict]] = []

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
        if path.startswith("/api/") and ("insertOrUpdate" in path or path.endswith("/delete")
                                         or path == "/api/nginx/reload"):
            self.writes.append((path, params))
            return ok({})
        return httpx.Response(200, json={"success": False, "status": "404", "msg": "no route"})

    def panel(self, dry_run=False, recorder=None):
        client = httpx.AsyncClient(base_url=BASE,
                                   transport=httpx.MockTransport(self._handler))
        return NgPanel(base_url=BASE, username="u", password="p",
                       dry_run=dry_run, recorder=recorder, client=client)


SERVER = {"id": "123", "serverName": SITE, "listen": "443", "ssl": 1,
          "proxyUpstreamId": ""}
UPSTREAM = {"id": "55", "name": UP_NAME, "proxyType": 0}
BACKEND = {"id": "7", "upstreamId": "55", "server": "192.0.2.10", "port": 4894}


def write_for(api, path_end):
    hits = [p for p in api.writes if p[0].endswith(path_end)]
    return hits[-1][1] if hits else None


# ------------------------------------------------------------------ 鉴权

def test_token_header_used_and_renewed_on_expiry():
    api = FakeNg(server=SERVER)
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


# ------------------------------------------------------------------ 反代头铁律

@pytest.mark.parametrize("bad_host", ["$host:$proxy_port", "$host:$server_port",
                                      "$host:8443", " $host : $proxy_port "])
def test_header_host_with_port_is_refused_before_any_request(bad_host):
    api = FakeNg()
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                    upstream_server="192.0.2.10:4894",
                                    cert_name="*.example.com", header_host=bad_host))
    assert ei.value.code == "header_host_port"
    assert api.requests == []   # 连 token 都不取，杜绝半写状态


def test_foreign_site_names_never_touched():
    api = FakeNg()
    with pytest.raises(NotManaged):
        run(api.panel().ensure_site(server_name="chat.example.com", upstream_name=UP_NAME,
                                    upstream_server="192.0.2.10:4894"))
    with pytest.raises(NotManaged):
        run(api.panel().ensure_site(server_name="dsh.legacy-names.example.com",
                                    upstream_name=UP_NAME,
                                    upstream_server="192.0.2.10:4894"))
    with pytest.raises(NotManaged):
        run(api.panel().delete_site(9, expect_name="chat.example.com"))
    assert api.writes == []


def test_foreign_upstream_name_blocked():
    api = FakeNg()
    with pytest.raises(NotManaged):
        run(api.panel().delete_upstream(9, expect_name="AISERVER_DSH"))
    assert api.writes == []


# ------------------------------------------------------------------ 幂等建站

def test_ensure_site_fresh_creates_upstream_site_and_location():
    api = FakeNg(server=None, upstream=None)

    # 让读接口按阶段逐渐"长出"对象，模拟真面板
    real_handler = api._handler

    def handler(request):
        resp = real_handler(request)
        p = request.url.path
        if p.endswith("/upstream/insertOrUpdate"):
            api.upstream = dict(UPSTREAM)
        elif p.endswith("/upstream/insertOrUpdateServer"):
            api.backends = [dict(BACKEND, upstreamId="55")]
        elif p.endswith("/server/insertOrUpdate"):
            api.server = dict(SERVER)
        elif p.endswith("/insertOrUpdateLocation"):
            api.locations = [{"id": "99", "path": "/", "type": 2, "headerHost": "$host",
                              "websocket": 1, "upstreamId": "55",
                              "serverId": "123"}]
        return resp

    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    panel = NgPanel(base_url=BASE, username="u", password="p", client=client)

    site = run(panel.ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                 upstream_server="192.0.2.10:4894",
                                 listen_port=443, cert_name="*.example.com"))
    assert (site.server_id, site.upstream_id, site.location_id) == (123, 55, 99)

    loc = write_for(api, "/server/insertOrUpdateLocation")
    assert loc["headerHost"] == "$host" and "$proxy_port" not in loc["headerHost"]
    assert loc["websocket"] == "1" and loc["type"] == "2" and loc["path"] == "/"
    assert loc["upstreamId"] == "55" and loc["serverId"] == "123"
    srv = write_for(api, "/api/server/insertOrUpdate")
    assert srv["serverName"] == SITE and srv["listen"] == "443" and srv["ssl"] == "1"
    assert "fullchain.cer" in srv["pem"] and srv["key"].endswith(".key")
    ups = write_for(api, "/api/upstream/insertOrUpdate")
    assert ups["name"] == UP_NAME
    back = write_for(api, "/upstream/insertOrUpdateServer")
    assert back["server"] == "192.0.2.10" and back["port"] == "4894"


def test_ensure_site_idempotent_updates_with_existing_ids():
    api = FakeNg(
        server=SERVER, upstream=UPSTREAM, backends=[BACKEND],
        locations=[{"id": "99", "path": "/", "type": 2, "headerHost": "$host",
                    "websocket": 1, "serverId": "123", "upstreamId": "55"}],
    )
    site = run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                       upstream_server="192.0.2.10:4894",
                                       cert_name="*.example.com"))
    assert site.server_id == 123 and site.location_id == 99
    srv = write_for(api, "/api/server/insertOrUpdate")
    assert srv.get("id") == "123"                     # 带原 id 更新
    loc = write_for(api, "/server/insertOrUpdateLocation")
    assert loc.get("id") == "99"                      # location 也是更新不是新建
    assert write_for(api, "/api/upstream/insertOrUpdate") is None  # upstream 已存在，不重建
    assert write_for(api, "/upstream/insertOrUpdateServer") is None  # 后端已在，不重加


def test_ensure_site_bad_upstream_server_format_rejected():
    api = FakeNg(server=SERVER, upstream=UPSTREAM)
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                    upstream_server="192.0.2.10"))
    assert ei.value.code == "bad_upstream_server"


# ------------------------------------------------------------------ 删除纪律

def test_delete_site_id_name_mismatch_blocks():
    api = FakeNg(server={"id": "123", "serverName": "dsh-other.example.com",
                         "listen": "443", "ssl": 1, "proxyUpstreamId": ""})
    with pytest.raises(NotManaged):
        run(api.panel().delete_site(123, expect_name=SITE))
    assert write_for(api, "/api/server/delete") is None


def test_delete_site_happy_then_upstream():
    api = FakeNg(server=SERVER, upstream=UPSTREAM)
    run(api.panel().delete_site(123, expect_name=SITE))
    run(api.panel().delete_upstream(55, expect_name=UP_NAME))
    assert write_for(api, "/api/server/delete") == {"id": "123"}
    assert write_for(api, "/api/upstream/delete") == {"id": "55"}


def test_delete_site_unknown_id_blocks():
    api = FakeNg(server=None)
    with pytest.raises(NotManaged):
        run(api.panel().delete_site(999, expect_name=SITE))
    assert write_for(api, "/api/server/delete") is None


# ------------------------------------------------------------------ reload / dry_run

def test_reload_posts_and_dry_run_suppresses_all_writes():
    rec = DryRunRecorder()
    api = FakeNg(server=SERVER, upstream=UPSTREAM, backends=[BACKEND], locations=[])
    panel = api.panel(dry_run=True, recorder=rec)
    site = run(panel.ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                 upstream_server="192.0.2.10:4894"))
    run(panel.reload())
    assert site.server_id == 0                       # 合成对象
    assert api.writes == []                          # 一个写请求都没发
    actions = [e.action for e in rec.entries]
    assert "server.insertOrUpdate" in actions
    assert "server.insertOrUpdateLocation" in actions
    assert "nginx.reload" in actions


def test_build_payload_location_defaults_are_the_safe_template():
    api = FakeNg()
    panel = api.panel()
    payload = panel._build_payload("location", server_id=1, upstream_id=2,
                                   header_host="$host")
    assert payload["headerHost"] == "$host"
    assert payload["websocket"] == 1 and payload["type"] == 2
    assert json.dumps(payload)  # 可序列化，能进日志


def test_ensure_site_passes_token_header_on_writes():
    api = FakeNg(server=SERVER, upstream=UPSTREAM, backends=[BACKEND], locations=[])
    run(api.panel().ensure_site(server_name=SITE, upstream_name=UP_NAME,
                                upstream_server="192.0.2.10:4894"))
    for r in api.requests:
        if r.url.path.startswith("/api/"):
            assert r.headers.get("token")
