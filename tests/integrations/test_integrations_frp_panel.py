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
    FrpPanel,
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
                rport=4894, config_b64=False):
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
            cid = payload.get("clientId")
            rows = [t for t in self.tunnels if not cid or t["client_id"] == cid]
            return httpx.Response(200, json=envelope({"total": len(rows), "proxy_configs": rows}))
        if path.endswith("/update_config"):
            inner = json.loads(base64.b64decode(payload["config"]).decode())
            self.tunnels.append({
                "id": max((t["id"] for t in self.tunnels), default=9000) + 1,
                "name": payload["name"], "type": "tcp",
                "client_id": payload["client_id"], "server_id": payload["server_id"],
                "config": json.dumps(inner), "origin_client_id": "c1.example",
            })
            return httpx.Response(200, json=envelope({}))
        if path.endswith("/delete_config"):
            self.tunnels = [t for t in self.tunnels if t["id"] != payload.get("id")]
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
                                  local_port=13000, remote_port=4894)
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


# ------------------------------------------------------------------ 读

def test_list_maps_tunnel_fields_and_filters_client():
    api = FakePanel([
        make_record(9001, "dsh_a", rport=4894),
        make_record(9002, "someone_b", client="other.example@1", rport=4899),
    ])
    panel = api.panel()
    all_t = run(panel.list())
    assert len(all_t) == 2
    a = next(t for t in all_t if t.name == "dsh_a")
    assert (a.proxy_id, a.remote_port, a.local_ip, a.local_port) == (9001, 4894, "192.0.2.10", 13000)
    only = run(panel.list("other.example@1"))
    assert [t.name for t in only] == ["someone_b"]


def test_list_error_envelope_raises():
    api = FakePanel([], list_code=500)
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().list())
    assert "boom" in str(ei.value)


def test_remote_port_taken():
    api = FakePanel([make_record(9001, "dsh_a", rport=4894)])
    assert run(api.panel().remote_port_taken(4894)) is True
    assert run(api.panel().remote_port_taken(4895)) is False


# ------------------------------------------------------------------ ensure

def test_ensure_idempotent_when_identical_exists():
    api = FakePanel([make_record(9001, "dsh_a", rport=4894)])
    t = run(api.panel().ensure(name="dsh_a", client_id="c1.example@1", server_id="default",
                               local_ip="192.0.2.10", local_port=13000, remote_port=4894))
    assert t.proxy_id == 9001
    assert "/api/v1/proxy/update_config" not in api.paths  # 一个字都不写


def test_ensure_port_mismatch_refuses_to_touch_existing():
    api = FakePanel([make_record(9001, "dsh_a", rport=4894)])
    with pytest.raises(IntegrationError) as ei:
        run(api.panel().ensure(name="dsh_a", client_id="c1.example@1", server_id="default",
                               local_ip="192.0.2.10", local_port=13000, remote_port=4900))
    assert ei.value.code == "frp_conflict"
    assert "/api/v1/proxy/update_config" not in api.paths


@pytest.mark.parametrize("bad_name", ["someone_els", "mac_ssh", "DSH_a", "dsh"])
def test_ensure_rejects_foreign_names_without_any_request(bad_name):
    api = FakePanel([make_record(9001, "dsh_a")])
    with pytest.raises(NotManaged):
        run(api.panel().ensure(name=bad_name, client_id="c1.example@1", server_id="default",
                               local_ip="192.0.2.10", local_port=1, remote_port=4893))
    assert api.requests == []  # 连 login 都不该发


def test_ensure_creates_with_base64_config_and_reads_back():
    api = FakePanel([make_record(9001, "someone_existing", client="other.example@1")])
    t = run(api.panel().ensure(name="dsh_new", client_id="c1.example@1", server_id="default",
                               local_ip="127.0.0.1", local_port=13300, remote_port=4893))
    assert t.name == "dsh_new" and t.proxy_id > 9001 and t.remote_port == 4893
    sent = api.body_of("/update_config")
    assert sent["name"] == "dsh_new" and sent["client_id"] == "c1.example@1"
    inner = json.loads(base64.b64decode(sent["config"]).decode())
    assert inner["remotePort"] == 4893 and inner["localIP"] == "127.0.0.1"
    assert inner["plugin"] is None


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


def test_delete_happy_path_posts_with_ids():
    api = FakePanel([make_record(9001, "dsh_alpha")])
    run(api.panel().delete(9001, expect_name="dsh_alpha"))
    sent = api.body_of("/delete_config")
    assert sent == {"id": 9001, "name": "dsh_alpha",
                    "client_id": "c1.example@1", "server_id": "default"}
    assert api.tunnels == []


# ------------------------------------------------------------------ dry_run

def test_dry_run_ensure_and_delete_record_but_send_nothing():
    rec = DryRunRecorder()
    api = FakePanel([make_record(9001, "dsh_alpha")])
    panel = api.panel(dry_run=True, recorder=rec)
    t = run(panel.ensure(name="dsh_new", client_id="c1.example@1", server_id="default",
                         local_ip="127.0.0.1", local_port=1, remote_port=4893))
    assert t.proxy_id == 0  # 合成对象，编排层不许当真 id 存
    run(panel.delete(9001, expect_name="dsh_alpha"))
    assert "/api/v1/proxy/update_config" not in api.paths
    assert "/api/v1/proxy/delete_config" not in api.paths
    actions = [e.action for e in rec.entries]
    assert "update_config(create)" in actions and "delete_config" in actions
    upd = next(e for e in rec.entries if e.action == "update_config(create)")
    assert upd.payload["config"] == "***"          # redact 生效
    assert upd.payload["name"] == "dsh_new"
