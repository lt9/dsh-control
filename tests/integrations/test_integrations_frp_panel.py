"""frp_panel.py：编解码、幂等、防误删。全部走 httpx.MockTransport，不碰真面板。"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from dsh_control.integrations.base import IntegrationError, NotManaged
from dsh_control.integrations.dry_run import DryRunRecorder
from dsh_control.integrations.frp_panel import (
    PROXY_CONFIG_FIELDS,
    FrpPanel,
    build_config_envelope,
    decode_config,
    encode_config,
    tunnel_config_payload,
)

BASE = "http://203.0.113.10:8500"


def run(coro):
    return asyncio.run(coro)


def envelope(body):
    return {"code": 200, "msg": "success", "body": body}


def make_record(rid, name, client="c1.example@1", lip="192.0.2.10", lport=13000,
                rport=20002, config_b64=False):
    inner = tunnel_config_payload(name=name, local_ip=lip, local_port=lport,
                                  remote_port=rport)
    cfg = encode_config(inner) if config_b64 else json.dumps(inner)
    return {"id": rid, "name": name, "type": "tcp", "client_id": client,
            "server_id": "default", "config": cfg,
            "origin_client_id": client.split("@")[0]}


class FakePanel:
    """假 frp-panel：记录收到的请求，按面板信封回话。"""

    def __init__(self, tunnels, list_code=200):
        self.tunnels = tunnels
        self.list_code = list_code
        self.requests: list[httpx.Request] = []

    @property
    def paths(self):
        return [r.url.path for r in self.requests]

    def body_of(self, path_end):
        for r in self.requests:
            if r.url.path.endswith(path_end):
                return json.loads(r.content or b"{}")
        return None

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        payload = json.loads(request.content or b"{}")
        if path.endswith("/auth/login"):
            return httpx.Response(200, json=envelope({"status": {"code": 1}, "token": "tk"}),
                                  headers={"set-cookie": "frp-panel-cookie=tk; Path=/"})
        if path.endswith("/list_configs"):
            if self.list_code != 200:
                return httpx.Response(200, json={"code": self.list_code, "msg": "boom", "body": {}})
            if payload.get("clientId") is not None:
                # 真机实测：带 clientId 过滤的 list_configs 返回**空集**（同一时刻不带
                # 过滤 59 条、带过滤 0 条）。假面板必须照做，否则"把过滤交给面板"的写法
                # 在测试里永远是绿的，到真机上就查无此隧道。
                return httpx.Response(200, json=envelope({"total": 0, "proxy_configs": []}))
            rows = list(self.tunnels)
            return httpx.Response(200, json=envelope({"total": len(rows), "proxy_configs": rows}))
        if path.endswith("/create_config"):
            # CreateProxyConfigRequest{clientId, serverId, config=b64({proxies:[...]}), overwrite}
            envelope_obj = json.loads(base64.b64decode(payload["config"]).decode())
            assert set(payload) == {"clientId", "serverId", "config", "overwrite"}
            for inner in envelope_obj["proxies"]:
                exists = next((t for t in self.tunnels
                               if t["name"] == inner["name"]
                               and t["client_id"] == payload["clientId"]), None)
                if exists is not None:
                    if not payload.get("overwrite"):
                        return httpx.Response(200, json={
                            "code": 500, "msg": "proxy name already exists", "body": {}})
                    exists["config"] = json.dumps(inner)
                    continue
                self.tunnels.append({
                    "id": max((t["id"] for t in self.tunnels), default=9000) + 1,
                    "name": inner["name"], "type": inner.get("type", "tcp"),
                    "client_id": payload["clientId"], "server_id": payload["serverId"],
                    "config": json.dumps(inner), "origin_client_id": "c1.example",
                    "stopped": False,
                })
            return httpx.Response(200, json=envelope({}))
        if path.endswith("/delete_config"):
            # DeleteProxyConfigRequest{clientId, serverId, name}——没有 id
            assert set(payload) == {"clientId", "serverId", "name"}
            self.tunnels = [t for t in self.tunnels
                            if not (t["name"] == payload["name"]
                                    and t["client_id"] == payload["clientId"]
                                    and t["server_id"] == payload["serverId"])]
            return httpx.Response(200, json=envelope({}))
        return httpx.Response(404, json={"code": 404, "msg": "no route", "body": {}})

    def panel(self, dry_run=False, recorder=None):
        client = httpx.AsyncClient(base_url=BASE,
                                   transport=httpx.MockTransport(self._handler))
        return FrpPanel(base_url=BASE, username="u", password="p",
                        dry_run=dry_run, recorder=recorder, client=client)


# ------------------------------------------------------------------ config 编解码

def test_config_encode_is_base64_of_json_string():
    inner = tunnel_config_payload(name="dsh_a", local_ip="192.0.2.10",
                                  local_port=13000, remote_port=20002)
    enc = encode_config(inner)
    assert json.loads(base64.b64decode(enc).decode()) == inner
    # 键序与内部形状照契约
    assert list(json.loads(base64.b64decode(enc).decode()).keys()) == [
        "name", "type", "transport", "loadBalancer", "healthCheck",
        "localIP", "localPort", "plugin", "remotePort",
    ]


def test_decode_accepts_plain_json_and_base64_forms():
    inner = tunnel_config_payload(name="dsh_a", local_ip="127.0.0.1",
                                  local_port=1, remote_port=2)
    assert decode_config(json.dumps(inner)) == inner     # 实测读回的形态
    assert decode_config(encode_config(inner)) == inner  # protobuf-bytes-as-base64 形态


def test_decode_bad_config_raises_integration_error():
    with pytest.raises(IntegrationError):
        decode_config("@@@@not-config@@@@")


# ------------------------------------------------- 写报文形状（proto/源码核定）
# 依据：上游 VaalaCat/frp-panel idl/api_client.proto（CreateProxyConfigRequest
# {client_id, server_id, config bytes, overwrite}；DeleteProxyConfigRequest
# {client_id, server_id, name}）与控制台 www/api/proxy.ts、
# mutate_proxy_config.tsx（config = UTF-8 JSON of {proxies:[proxy]}）。
# 下面这份记录按线上面板读回形状钉死（值已脱敏，字段集合与键序保持真实）。

PANEL_RECORD_FIXTURE = {
    "id": 9007,
    "name": "dsh_03",
    "type": "tcp",
    "client_id": "c1.example@1",
    "server_id": "default",
    "config": json.dumps({
        "name": "dsh_03",
        "type": "tcp",
        "transport": {"bandwidthLimit": ""},
        "loadBalancer": {"group": ""},
        "healthCheck": {"type": "", "intervalSeconds": 0},
        "localIP": "192.0.2.10",
        "localPort": 13300,
        "plugin": None,
        "remotePort": 4902,
    }),
    "origin_client_id": "c1.example",
    "stopped": False,
}


def test_real_shape_record_parses_end_to_end():
    api = FakePanel([dict(PANEL_RECORD_FIXTURE)])
    t = run(api.panel().get(9007))
    assert (t.name, t.client_id, t.server_id) == ("dsh_03", "c1.example@1", "default")
    assert (t.local_ip, t.local_port, t.remote_port) == ("192.0.2.10", 13300, 4902)


def test_write_config_field_set_equals_read_record_field_set():
    """写出的代理对象字段集合必须与面板读回的同构——读写不对称就是这次的事故。"""
    read_inner = json.loads(PANEL_RECORD_FIXTURE["config"])
    api = FakePanel([dict(PANEL_RECORD_FIXTURE)])
    run(api.panel().ensure(name="dsh_04", client_id="c1.example@1", server_id="default",
                           local_ip="192.0.2.10", local_port=13301, remote_port=4903))
    sent = api.body_of("/create_config")
    written, = json.loads(base64.b64decode(sent["config"]).decode())["proxies"]
    assert list(written) == list(read_inner)  # 键序也一致
    assert set(written) == PROXY_CONFIG_FIELDS


def test_build_config_envelope_wraps_proxies_array():
    inner = tunnel_config_payload(name="dsh_a", local_ip="192.0.2.10",
                                  local_port=1, remote_port=2)
    decoded = json.loads(base64.b64decode(build_config_envelope(inner)).decode())
    assert decoded == {"proxies": [inner]}


def test_envelope_rejects_unknown_proxy_field_before_any_request():
    bad = tunnel_config_payload(name="dsh_a", local_ip="192.0.2.10",
                                local_port=1, remote_port=2)
    bad["useEncryption"] = True  # 白名单外字段：本地报错，绝不发给生产面板
    with pytest.raises(IntegrationError) as ei:
        build_config_envelope(bad)
    assert ei.value.code == "config_fields_not_allowed"
    assert "useEncryption" in str(ei.value)


# ------------------------------------------------------------------ 读

def test_list_maps_tunnel_fields_and_filters_client():
    api = FakePanel([
        make_record(9001, "dsh_a", rport=20002),
        make_record(9002, "someone_b", client="other.example@1", rport=20009),
    ])
    panel = api.panel()
    all_t = run(panel.list())
    assert len(all_t) == 2
    a = next(t for t in all_t if t.name == "dsh_a")
    assert (a.proxy_id, a.remote_port, a.local_ip, a.local_port) == (9001, 20002, "192.0.2.10", 13000)
    only = run(panel.list("other.example@1"))
    assert [t.name for t in only] == ["someone_b"]


def test_list_error_envelope_raises():
    api = FakePanel([], list_code=500)
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().list())
    assert "boom" in str(ei.value)


def test_remote_port_taken():
    api = FakePanel([make_record(9001, "dsh_a", rport=20002)])
    assert run(api.panel().remote_port_taken(20002)) is True
    assert run(api.panel().remote_port_taken(20003)) is False


# ------------------------------------------------------------------ ensure

def test_ensure_idempotent_when_identical_exists():
    api = FakePanel([make_record(9001, "dsh_a", rport=20002)])
    t = run(api.panel().ensure(name="dsh_a", client_id="c1.example@1", server_id="default",
                               local_ip="192.0.2.10", local_port=13000, remote_port=20002))
    assert t.proxy_id == 9001
    assert "/api/v1/proxy/create_config" not in api.paths  # 一个字都不写


def test_ensure_port_mismatch_refuses_to_touch_existing():
    api = FakePanel([make_record(9001, "dsh_a", rport=20002)])
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().ensure(name="dsh_a", client_id="c1.example@1", server_id="default",
                               local_ip="192.0.2.10", local_port=13000, remote_port=4900))
    assert ei.value.code == "frp_conflict"
    assert "/api/v1/proxy/create_config" not in api.paths


@pytest.mark.parametrize("bad_name", ["someone_els", "mac_ssh", "DSH_a", "dsh"])
def test_ensure_rejects_foreign_names_without_any_request(bad_name):
    api = FakePanel([make_record(9001, "dsh_a")])
    with pytest.raises(NotManaged):
        run(api.panel().ensure(name=bad_name, client_id="c1.example@1", server_id="default",
                               local_ip="192.0.2.10", local_port=1, remote_port=20001))
    assert api.requests == []  # 连 login 都不该发


def test_ensure_creates_with_base64_config_and_reads_back():
    api = FakePanel([make_record(9001, "someone_existing", client="other.example@1")])
    t = run(api.panel().ensure(name="dsh_new", client_id="c1.example@1", server_id="default",
                               local_ip="127.0.0.1", local_port=13300, remote_port=20001))
    assert t.name == "dsh_new" and t.proxy_id > 9001 and t.remote_port == 20001
    sent = api.body_of("/create_config")
    # CreateProxyConfigRequest 只有这四个字段（proto 核定）：没有 name/type
    assert set(sent) == {"clientId", "serverId", "config", "overwrite"}
    assert sent["clientId"] == "c1.example@1" and sent["overwrite"] is False
    envelope_obj = json.loads(base64.b64decode(sent["config"]).decode())
    assert list(envelope_obj) == ["proxies"]  # 控制台同款 {"proxies": [...]} 包装
    inner, = envelope_obj["proxies"]
    assert inner["name"] == "dsh_new"
    assert inner["remotePort"] == 20001 and inner["localIP"] == "127.0.0.1"
    assert inner["plugin"] is None


def test_list_filters_locally_and_never_asks_panel_to_filter():
    """面板的 clientId 过滤在真机上返回空集，过滤只能在本地做。

    这条不是形状洁癖：过滤一旦交给面板，find_by_name 永远返回 None，于是 ensure
    以为隧道不存在而重复建门，创建后的回读校验也永远失败——真机上就是这样把第一次
    真实 expose 判成"创建后回读不到"的，而隧道其实已经建成、frps 已经在监听。
    """
    api = FakePanel([
        make_record(9001, "dsh_a", client="c1.example@1"),
        make_record(9002, "dsh_b", client="c1.example@1"),
        make_record(9003, "someone_x", client="other.example@1"),
    ])
    got = run(api.panel().list("c1.example@1"))
    assert [t.name for t in got] == ["dsh_a", "dsh_b"]
    lists = [r for r in api.requests if r.url.path.endswith("/list_configs")]
    assert lists, "list 没打 list_configs"
    assert all("clientId" not in json.loads(r.content or b"{}") for r in lists)
    # 同一条路：find_by_name 必须还能查到（它以前就是在这里失明的）
    t = run(api.panel().find_by_name("dsh_b", "c1.example@1"))
    assert t is not None and t.proxy_id == 9002
    assert run(api.panel().find_by_name("someone_x", "c1.example@1")) is None


# ------------------------------------------------------------------ delete 防误删

def test_delete_name_mismatch_raises_and_never_posts_delete():
    api = FakePanel([make_record(9001, "dsh_beta")])
    with pytest.raises(NotManaged) as ei:
        run(api.panel().delete(9001, expect_name="dsh_alpha"))
    assert ei.value.code == "name_mismatch"
    assert "/api/v1/proxy/delete_config" not in api.paths
    assert len(api.tunnels) == 1  # 一条没少


def test_delete_foreign_named_tunnel_blocked_even_with_right_id():
    api = FakePanel([make_record(9001, "someone_else")])
    with pytest.raises(NotManaged):
        run(api.panel().delete(9001, expect_name="dsh_alpha"))
    assert "/api/v1/proxy/delete_config" not in api.paths


def test_delete_unknown_id_blocked():
    api = FakePanel([make_record(9001, "dsh_beta")])
    with pytest.raises(NotManaged):
        run(api.panel().delete(9999, expect_name="dsh_beta"))
    assert "/api/v1/proxy/delete_config" not in api.paths


def test_delete_happy_path_posts_name_triplet_not_id():
    api = FakePanel([make_record(9001, "dsh_alpha")])
    run(api.panel().delete(9001, expect_name="dsh_alpha"))
    sent = api.body_of("/delete_config")
    # DeleteProxyConfigRequest{client_id, server_id, name}——没有 id 字段
    assert sent == {"clientId": "c1.example@1", "serverId": "default", "name": "dsh_alpha"}
    assert api.tunnels == []


def test_delete_refuses_when_owner_fields_missing_never_posts():
    rec = make_record(9001, "dsh_alpha")
    rec["client_id"] = ""  # 归属读丢了：宁可不删，绝不盲发
    api = FakePanel([rec])
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().delete(9001, expect_name="dsh_alpha"))
    assert ei.value.code == "missing_owner"
    assert "/api/v1/proxy/delete_config" not in api.paths


# ------------------------------------------------------------------ dry_run

def test_dry_run_ensure_and_delete_record_but_send_nothing():
    rec = DryRunRecorder()
    api = FakePanel([make_record(9001, "dsh_alpha")])
    panel = api.panel(dry_run=True, recorder=rec)
    t = run(panel.ensure(name="dsh_new", client_id="c1.example@1", server_id="default",
                         local_ip="127.0.0.1", local_port=1, remote_port=20001))
    assert t.proxy_id == 0  # 合成对象，编排层不许当真 id 存
    run(panel.delete(9001, expect_name="dsh_alpha"))
    assert "/api/v1/proxy/create_config" not in api.paths
    assert "/api/v1/proxy/delete_config" not in api.paths
    actions = [e.action for e in rec.entries]
    assert "create_config" in actions and "delete_config" in actions
    upd = next(e for e in rec.entries if e.action == "create_config")
    assert upd.payload["config"] == "***"          # redact 生效
    assert upd.payload["clientId"] == "c1.example@1"
