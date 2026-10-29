"""memory_hub 适配器单测：httpx MockTransport 替身，不发真请求。

判据形状全部照 MemoryCore/v3-api-memorycore-doc.md §3.7 与 §4.2 的实测契约：
信封 {code,message,data}、409 duplicate_entry、auth/verify 非法 key 也回 200/code=0、
错误 message 前缀是稳定机器码。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from dsh_control.integrations.base import IntegrationError
from dsh_control.integrations.dry_run import DryRunRecorder
from dsh_control.integrations.memory_hub import (
    MemoryHub,
    mask_key,
    memory_base_url,
    probe_tcp,
    resolve_proxy_base,
    user_id_for,
)
from dsh_control.settings import Settings, apply_site_overrides


def mk_hub(handler, *, dry_run=False, recorder=None) -> MemoryHub:
    transport = httpx.MockTransport(handler)
    return MemoryHub(
        core_url="http://core.test:8420", admin_key="sk-mem-admin00000000",
        client=httpx.AsyncClient(transport=transport,
                                 base_url="http://core.test:8420"),
        dry_run=dry_run, recorder=recorder)


def env(data=None, *, code=0, message="ok"):
    return httpx.Response(200, json={"code": code, "message": message,
                                     "data": data if data is not None else {}})


# ------------------------------------------------------------------ 用户生命周期

async def test_新建用户一步拿到默认_key():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.url.path == "/v3/meta/user/create"
        assert json.loads(req.content) == {"username": "dshctl-dsh-07",
                                           "user_id": "dshctl-dsh-07"}
        assert req.headers["x-tdai-service-id"] == "default"
        assert req.headers["x-tdai-user-key"] == "sk-mem-admin00000000"
        return env({"user_id": "dshctl-dsh-07", "user_type": "normal",
                    "default_user_key": "sk-mem-newuserkey123"})

    hub = mk_hub(handler)
    out = await hub.ensure_instance_user("dsh-07")
    assert out == {"user_id": "dshctl-dsh-07", "user_key": "sk-mem-newuserkey123",
                   "created": True, "team": "skipped"}


async def test_同名用户按_duplicate_entry_幂等短路并补发_key():
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path == "/v3/meta/user/create":
            # core 对重复 user_id 回 HTTP 409，信封里 message 前缀是机器码
            return httpx.Response(409, json={
                "code": 409, "message": "duplicate_entry: user_id exists",
                "data": None})
        if req.url.path == "/v3/meta/user/get":
            return env({"user_id": "dshctl-dsh-07", "user_type": "normal"})
        if req.url.path == "/v3/meta/user-key/create":
            assert json.loads(req.content)["user_id"] == "dshctl-dsh-07"
            return env({"key_id": "key-2", "key_value": "sk-mem-rotated12345"})
        raise AssertionError(f"不该请求 {req.url.path}")

    hub = mk_hub(handler)
    out = await hub.ensure_instance_user("dsh-07")
    assert paths == ["/v3/meta/user/create", "/v3/meta/user/get",
                     "/v3/meta/user-key/create"]
    assert out["created"] is False and out["user_key"] == "sk-mem-rotated12345"


async def test_拉群失败不毁接线但事实里说破():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v3/meta/team-member/add":
            return httpx.Response(404, json={
                "code": 404, "message": "team_not_found: team-x", "data": None})
        return env({"user_id": "dshctl-dsh-07",
                    "default_user_key": "sk-mem-k1k1k1k1k1"})

    hub = mk_hub(handler)
    out = await hub.ensure_instance_user("dsh-07", team_id="team-x")
    assert out["team"].startswith("failed") and "team_not_found" in out["team"]


async def test_成员已存在算成功():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v3/meta/team-member/add":
            return httpx.Response(409, json={
                "code": 409, "message": "member_already_exists", "data": None})
        return env({"user_id": "dshctl-dsh-07",
                    "default_user_key": "sk-mem-k1k1k1k1k1"})

    hub = mk_hub(handler)
    out = await hub.ensure_instance_user("dsh-07", team_id="team-fam")
    assert out["team"] == "already"


# ------------------------------------------------------------------ verify / 回收

async def test_verify_非法_key_也是_200_判据在_data_valid():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v3/meta/auth/verify"
        return env({"valid": False, "user": None})

    hub = mk_hub(handler)
    assert await hub.verify_key("sk-mem-revoked0000") == {"valid": False,
                                                          "user_id": ""}


async def test_verify_合法_key_带回_user_id():
    def handler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content) == {"user_key": "sk-mem-goodkey00000"}
        return env({"valid": True, "user": {"user_id": "dshctl-dsh-07"}})

    hub = mk_hub(handler)
    out = await hub.verify_key("sk-mem-goodkey00000")
    assert out == {"valid": True, "user_id": "dshctl-dsh-07"}


async def test_回收只动_active_单把失败不中断其余():
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        if req.url.path == "/v3/meta/user-key/list":
            return env({"items": [
                {"key_id": "k1", "status": "active"},
                {"key_id": "k2", "status": "revoked"},
                {"key_id": "k3", "status": "active"}], "total": 3})
        if body.get("key_id") == "k1":
            return env({"key_id": "k1", "status": "revoked"})
        if body.get("key_id") == "k3":
            return httpx.Response(409, json={
                "code": 409, "message": "last_key_cannot_revoke", "data": None})
        raise AssertionError("unexpected")

    hub = mk_hub(handler)
    out = await hub.revoke_user_keys("dshctl-dsh-07")
    assert out["revoked"] == ["k1"]
    assert out["failed"] == [{"key_id": "k3", "why": "last_key_cannot_revoke"}]
    # revoked 状态的那把绝不能被再回收
    assert [b.get("key_id") for p, b in calls if p.endswith("revoke")] == ["k1", "k3"]


# ------------------------------------------------------------------ 传输与探活

async def test_非零信封抛机器码与人话():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401,
                                         "message": "invalid_credentials",
                                         "data": None})

    hub = mk_hub(handler)
    with pytest.raises(IntegrationError) as ei:
        await hub.verify_key("sk-mem-whatever000")
    assert ei.value.code == "invalid_credentials"
    assert "记忆中枢" in str(ei.value)


async def test_core_不可达的文案给出下一步():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    hub = mk_hub(handler)
    with pytest.raises(IntegrationError) as ei:
        await hub.ensure_instance_user("dsh-09")
    assert ei.value.code == "core_unreachable"
    assert "重跑" in str(ei.value)


async def test_core_health_任何异常都是_False_不是抛():
    hub = mk_hub(lambda req: httpx.Response(500))
    assert await hub.core_health() is False
    hub2 = mk_hub(lambda req: httpx.Response(200, json={"status": "ok"}))
    assert await hub2.core_health() is True


async def test_probe_tcp_真连接与拒绝与烂_URL():
    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    async with srv:
        assert await probe_tcp(f"http://127.0.0.1:{port}/dsh") is True
    assert await probe_tcp(f"http://127.0.0.1:{port}/dsh") is False
    assert await probe_tcp("not-a-url") is False
    assert await probe_tcp("") is False


# ------------------------------------------------------------------ dry_run

async def test_dry_run_写动作不发读动作照跑():
    rec = DryRunRecorder()
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        assert req.url.path == "/v3/meta/auth/verify"   # 只有读动作能到这里
        return env({"valid": True, "user": {"user_id": "dshctl-dsh-07"}})

    hub = mk_hub(handler, dry_run=True, recorder=rec)
    out = await hub.ensure_instance_user("dsh-07")
    assert out["dry_run"] is True and out["user_key"] == ""
    assert (await hub.verify_key("sk-mem-x000000000"))["valid"] is True
    assert seen == ["/v3/meta/auth/verify"]
    assert [e.action for e in rec.entries] == ["user/create", "user-key/create"]


# ------------------------------------------------------------------ 纯函数

def test_key_打码与确定性_id():
    assert mask_key("sk-mem-hXG0abcdefghij1234") == "sk-mem-hXG0a****"
    assert mask_key("short") == "****"
    assert mask_key("") == ""
    assert user_id_for("dsh-02") == "dshctl-dsh-02"


def test_按主机出口覆盖_命中顺序_name_ssh_host_默认():
    s = Settings(memory_proxy_url="http://198.51.100.9:8096/dsh/",
                 memory_space_id="default",
                 memory_host_proxy={"vps-1": "http://172.16.9.1:7796/dsh",
                                    "198.51.100.77": "http://172.16.9.2:7796/dsh"})
    assert resolve_proxy_base(s, "vps-1", "198.51.100.77") == "http://172.16.9.1:7796/dsh"
    assert resolve_proxy_base(s, "别的", "198.51.100.77") == "http://172.16.9.2:7796/dsh"
    assert resolve_proxy_base(s, "nas", "198.51.100.5") == "http://198.51.100.9:8096/dsh"
    # baseURL = 出口前缀 + space（不带 /v1：proxy 的 anthropic 转发自拼 /v1/messages）
    assert memory_base_url(s, "vps-1") == "http://172.16.9.1:7796/dsh/default"


def test_site_memory_段解析_缺段行为不变():
    base = Settings()
    assert base.memory_enabled is False and base.memory_core_url == ""

    # apply_site_overrides 吃的是 credentials(kind=site).secret：形状 = [site] 内容
    # + providers 子字典；[memory] 独立段要由 cli seed 镜像成 secret["memory"]（lead 接线）
    s = apply_site_overrides(Settings(), {
        "public_base": "https://c.example.test",
        "memory": {"enabled": True, "core_url": "http://198.51.100.9:8420",
                   "proxy_url": "http://198.51.100.9:8096/dsh",
                   "team_id": "team-fam", "space_id": "default",
                   "host_proxy": {"vps-1": "http://172.16.9.1:7796/dsh"}}})
    assert s.memory_enabled is True
    assert s.memory_core_url == "http://198.51.100.9:8420"
    assert s.memory_host_proxy == {"vps-1": "http://172.16.9.1:7796/dsh"}

    # 无 [memory] 段：一个字段都不碰（与引入本功能之前逐字节一致的开关前提）
    s2 = apply_site_overrides(Settings(), {"dns_zone": "example.test"})
    assert s2.memory_enabled is False and s2.memory_proxy_url == ""

    # [site].memory_* 平铺写法同样生效（seed 现在就镜像 [site]，零改动可用）
    s3 = apply_site_overrides(Settings(), {"memory_enabled": True})
    assert s3.memory_enabled is True

    # enabled=false 照写覆盖：段在就以段为准，False 不是「缺省」
    s4 = apply_site_overrides(Settings(memory_enabled=True),
                              {"memory": {"enabled": False}})
    assert s4.memory_enabled is False
