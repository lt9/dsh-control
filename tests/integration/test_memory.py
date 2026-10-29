"""记忆接线端到端（假 Hub + 假执行器）：开通第 68 步、memory 启停工单、
purge 处置、/api/instances/{slug}/memory、watcher 探针去重。

真值锚点（现场取证 2026-10-29，MEMORY-P0.md）：确定性 user_id、key 走 stdin、
P0 旧记号认作已接线、缺料 skipped 不挡开通。地址全用 RFC5737 文档段。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from dsh_control.api import memory as memory_api
from dsh_control.db import repo
from dsh_control.db.models import Credential, Event
from dsh_control.integrations import memory_hub as memhub
from dsh_control.integrations.base import ExecResult
from dsh_control.jobs.deploy import run_memory
from dsh_control.watcher import WatcherState, scan_once
from tests.conftest import SAFE_DUMP, XRW, create_instance, wait_job

MEM_SETTINGS = {
    "memory_enabled": True,
    "memory_core_url": "http://198.51.100.9:8420",
    "memory_proxy_url": "http://198.51.100.9:8096/dsh",
    "memory_team_id": "team-fam",
    "memory_model_id": "TestModel-1",
}
FAKE_KEY = "sk-mem-fakekey123456"
STATE_NO = "@@MEM@@ no\n@@MEM_P0@@ no\n@@REFS@@ no"
STATE_P0 = "@@MEM@@ no\n@@MEM_P0@@ yes\n@@REFS@@ yes"
STATE_MEM = "@@MEM@@ yes\n@@MEM_P0@@ no\n@@REFS@@ yes"


class FakeHub:
    """MemoryHub 的替身：形状照真类，行为可用构造参数调。"""

    manual_required = False
    last: FakeHub | None = None

    def __init__(self, *, core_url="", admin_key="", dry_run=False,
                 recorder=None, **_kw):
        FakeHub.last = self
        self.core_url = core_url
        self.admin_key = admin_key
        self.core_ok = True
        self.ensure_calls: list[str] = []
        self.verify_calls: list[str] = []
        self.revoke_calls: list[str] = []
        self.fail_ensure = False

    async def core_health(self, *, timeout: float = 5.0) -> bool:
        return self.core_ok

    async def ensure_instance_user(self, slug, *, team_id="", agent_name=""):
        self.ensure_calls.append(f"{slug}|{team_id}")
        if self.fail_ensure:
            from dsh_control.integrations.base import IntegrationError
            raise IntegrationError("假 core 500", code="boom", detail="internal")
        return {"user_id": f"dshctl-{slug}", "user_key": FAKE_KEY,
                "created": True, "team": "added"}

    async def verify_key(self, user_key):
        self.verify_calls.append(user_key)
        ok = user_key == FAKE_KEY
        return {"valid": ok, "user_id": "dshctl-dsh-01" if ok else ""}

    async def revoke_user_keys(self, user_id):
        self.revoke_calls.append(user_id)
        return {"user_id": user_id, "revoked": ["k1", "k2"], "failed": []}

    async def aclose(self):
        return None


def arm(env, monkeypatch, *, hub_ok=True):
    """站点开记忆 + admin key 入库 + 假 Hub 顶掉真类 + 假执行器接住容器侧脚本。"""
    for k, v in MEM_SETTINGS.items():
        setattr(env.settings, k, v)
    hub = FakeHub()
    hub.core_ok = hub_ok
    monkeypatch.setattr(memhub, "MemoryHub", lambda **kw: hub)
    monkeypatch.setattr(memhub, "probe_tcp", _true_tcp)
    ex = env.providers.executor
    ex.handlers += [
        ('printf %s "${DSH_HOME:-/data}"', ExecResult(0, "/data", "")),
        ("@@MEM@@", ExecResult(0, STATE_NO, "")),
        (".credentials.yaml", ExecResult(
            0, "[mem] refs.PROXY_USER_KEY 已合并写入（600，值不回显）", "")),
        ("DSHC_MEM_EOF", ExecResult(0, "[mem] PATCH_WIRED\n", "")),
        ("@@STRIP@@", ExecResult(
            0, "@@STRIP@@ patch=removed\n@@STRIP@@ refs=removed\n"
               "@@STRIP@@ RESTART_NEEDED", "")),
    ]
    return hub


async def _true_tcp(url, *, timeout: float = 4.0) -> bool:
    return True


def register_memory_flow(env):
    """lead 在 main.py 接上 "memory": run_memory 之前，测试自己把流注册进 runner。"""
    env.app.state.runner.register_flows({"memory": run_memory})


async def seed_admin_key(env):
    async with env.db.session() as s:
        await repo.upsert_credential(
            s, kind="memory_admin_key", name="memory-admin",
            secret={"user_key": "sk-mem-admin00000000"}, note="测试 admin")
        await s.commit()


async def events_of(env, kind: str) -> list[Event]:
    async with env.db.session() as s:
        return list((await s.scalars(select(Event).where(Event.kind == kind))).all())


def memory_step(job_json: dict, key: str = "memory_wiring") -> dict:
    return next(st for st in job_json["steps"] if st["key"] == key)


async def cred_row(env, name: str) -> Credential | None:
    async with env.db.session() as s:
        return await repo.get_credential_by_name(s, name)


def memory_dump() -> str:
    """把 SAFE_DUMP 的 llm-deepseek 换成「经记忆出口活着」的形状。"""
    return SAFE_DUMP.replace(
        "- id: llm-deepseek\n  name: '@deepseek-ai/dsh-llm-deepseek-api-key'\n"
        "  disabled: true",
        "- id: llm-deepseek\n  name: '@deepseek-ai/dsh-llm-deepseek-api-key'\n"
        "  disabled: false\n  config:\n"
        "    baseURL: http://198.51.100.9:8096/dsh/default")


# ---------------------------------------------------------------- 开通第 68 步

async def test_开通接线全成_key走stdin_重启后安全门认记忆出口(env, monkeypatch):
    hub = arm(env, monkeypatch)
    await seed_admin_key(env)
    env.providers.executor.handlers.append(("dump-config", ExecResult(0, memory_dump(), "")))
    r = await create_instance(env.client)
    assert r.status_code == 200, r.text
    body = r.json()
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]

    keys = [st["key"] for st in job["steps"]]
    assert keys.index("fonts") < keys.index("memory_wiring") < keys.index("token_capture")
    st = memory_step(job)
    assert st["state"] == "succeeded"
    assert "http://198.51.100.9:8096/dsh/default" in st["detail"]
    assert "auth/verify 回读通过" in st["detail"] and "已重启容器并确认健康" in st["detail"]

    assert hub.ensure_calls == ["dsh-01|team-fam"]     # user_id=dshctl-dsh-01 幂等键
    row = await cred_row(env, "dshctl-dsh-01")
    assert row is not None and row.kind == "memory_user_key"
    assert row.secret["user_key"] == FAKE_KEY
    journal = "\n".join(env.providers.executor.commands)
    assert "docker exec -i dsh-dsh-01" in journal      # key 只走 stdin
    assert FAKE_KEY not in journal                     # key 从不进命令行
    assert any(s and FAKE_KEY in s for s in env.providers.executor.stdins)
    assert "runtime.restart dsh-dsh-01" in "\n".join(env.journal)
    assert await events_of(env, "memory_wired")


async def test_栈不通_第68步skipped加警告事件_开通照走(env, monkeypatch):
    hub = arm(env, monkeypatch, hub_ok=False)
    await seed_admin_key(env)
    r = await create_instance(env.client)
    assert r.status_code == 200, r.text
    body = r.json()
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    st = memory_step(job)
    assert st["state"] == "skipped" and "core 不通" in st["detail"]
    assert hub.ensure_calls == []                      # 没对 Hub 写过
    assert (await events_of(env, "memory_wiring_skipped"))
    assert await cred_row(env, "dshctl-dsh-01") is None


async def test_P0旧记号_认作已接线_台账没key就补发接管(env, monkeypatch):
    arm(env, monkeypatch)
    await seed_admin_key(env)
    env.providers.executor.handlers.insert(
        1, ("@@MEM@@", ExecResult(0, STATE_P0, "")))   # 顶掉 STATE_NO（首个匹配生效）
    env.providers.executor.handlers.append(("dump-config", ExecResult(0, memory_dump(), "")))
    r = await create_instance(env.client)
    assert r.status_code == 200, r.text
    body = r.json()
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    st = memory_step(job)
    assert st["state"] == "succeeded" and "P0" in st["detail"]
    row = await cred_row(env, "dshctl-dsh-01")          # 接管：发 key 进台账
    assert row is not None and row.secret["user_key"] == FAKE_KEY
    # 但段不重复铺（P0 段还在文件里，没有成对记号就不动它）
    assert not any("DSHC_MEM_EOF" in c for c in env.providers.executor.commands)
    assert "runtime.restart" not in "\n".join(env.journal)   # 没铺新段就不重启


# ---------------------------------------------------------------- 启停工单

async def deploy_plain(env) -> dict:
    r = await create_instance(env.client)
    assert r.status_code == 200, r.text
    body = r.json()
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    return body


async def test_enable工单_全成并刷新token(env, monkeypatch):
    body = await deploy_plain(env)                     # 先按无记忆开通
    arm(env, monkeypatch)
    await seed_admin_key(env)
    env.app.include_router(memory_api.router, prefix="/api")
    register_memory_flow(env)
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "enable"}, headers=XRW)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert job["kind"] == "memory" and job["state"] == "succeeded", job["error"]
    assert [(st["seq"], st["key"]) for st in job["steps"]] == [
        (10, "memory_wiring"), (20, "token_capture")]
    assert memory_step(job)["state"] == "succeeded"
    row = await cred_row(env, "dshctl-dsh-01")
    assert row is not None and row.kind == "memory_user_key"
    detail = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert detail["state"] == "running"                # 工单成功不许动状态机


async def test_disable工单_回收加摘线_失败不把实例改红(env, monkeypatch):
    await deploy_plain(env)
    hub = arm(env, monkeypatch)
    await seed_admin_key(env)
    env.app.include_router(memory_api.router, prefix="/api")
    register_memory_flow(env)
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "enable"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "disable"}, headers=XRW)
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert [(st["seq"], st["key"]) for st in job["steps"]] == [
        (10, "memory_revoke"), (20, "memory_strip"), (30, "token_capture")]
    assert hub.revoke_calls == ["dshctl-dsh-01"]
    assert "回收 2 把" in memory_step(job, "memory_revoke")["detail"]
    assert await events_of(env, "memory_user_disabled")
    # Hub 挂了再停用：工单 failed，但 running 的实例不许被改判 error
    hub.fail_ensure = True
    from dsh_control.integrations import base

    async def boom(user_id):
        raise base.IntegrationError("假 core 又 500", code="boom")
    hub.revoke_user_keys = boom
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "disable"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    job = (await env.client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    assert job["state"] == "failed"
    detail = (await env.client.get("/api/instances/1")).json()
    assert detail["state"] == "running"


# ---------------------------------------------------------------- API 守门

async def test_api_动作校验_状态门_并发门(env):
    await deploy_plain(env)
    env.app.include_router(memory_api.router, prefix="/api")
    register_memory_flow(env)
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "toggle"}, headers=XRW)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_action"
    r = await env.client.post("/api/instances/no-such/memory",
                              json={"action": "enable"}, headers=XRW)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "enable"})      # 缺 XRW
    assert r.status_code == 403
    # released 不许操作记忆（routes 的 release 走 instance_id，记忆路由走 slug）
    iid = (await env.client.get("/api/instances")).json()["items"][0]["id"]
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    assert r.status_code == 200, r.text
    await wait_job(env.app, r.json()["job_id"])
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "enable"}, headers=XRW)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "cannot_memory_from_released"


async def test_api_同实例工单不双开(env, monkeypatch):
    await deploy_plain(env)
    arm(env, monkeypatch)
    await seed_admin_key(env)

    slow = FakeHub()
    slow.core_ok = True
    real_health = slow.core_health

    async def slow_health(*, timeout: float = 5.0):
        await asyncio.sleep(0.4)
        return await real_health()
    slow.core_health = slow_health
    monkeypatch.setattr(memhub, "MemoryHub", lambda **kw: slow)
    env.app.include_router(memory_api.router, prefix="/api")
    register_memory_flow(env)
    r1 = await env.client.post("/api/instances/dsh-01/memory",
                               json={"action": "enable"}, headers=XRW)
    assert r1.status_code == 200
    r2 = await env.client.post("/api/instances/dsh-01/memory",
                               json={"action": "disable"}, headers=XRW)
    assert r2.status_code == 409 and r2.json()["detail"]["error"] == "job_in_progress"
    await wait_job(env.app, r1.json()["job_id"])


async def test_api_状态出参打码_无记忆站点照答未配置(env, monkeypatch):
    await deploy_plain(env)
    env.app.include_router(memory_api.router, prefix="/api")
    register_memory_flow(env)
    r = await env.client.get("/api/instances/dsh-01/memory")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["hub"] is None

    arm(env, monkeypatch)
    await seed_admin_key(env)
    env.providers.executor.handlers.insert(
        1, ("@@MEM@@", ExecResult(0, STATE_MEM, "")))
    r = await env.client.post("/api/instances/dsh-01/memory",
                              json={"action": "enable"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    r = await env.client.get("/api/instances/dsh-01/memory")
    body = r.json()
    assert body["enabled"] is True and body["wired"] is True
    assert body["user"]["key_masked"] and FAKE_KEY not in r.text
    assert body["hub"]["core_ok"] is True
    assert body["egress"]["base_url"] == "http://198.51.100.9:8096/dsh/default"
    assert body["last_job"]["kind"] if "kind" in (body["last_job"] or {}) else True


# ---------------------------------------------------------------- purge 处置

async def release_instance(env) -> int:
    body = await deploy_plain(env)
    iid = body["instance_id"]
    env.providers.executor.handlers.insert(
        0, ("instances/dsh-01/.env", ExecResult(0, "DATA_DIR=/hdd1/dsh-data/dsh-01\n", "")))
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    return iid


async def run_purge_job(env, iid: int, params: dict) -> dict:
    async with env.db.session() as s:
        job = await repo.create_job(s, iid, "purge",
                                    {"source": "manual", "confirm": "dsh-01", **params})
        await s.commit()
        job_id = job.id
    env.app.state.runner.spawn(job_id)
    await wait_job(env.app, job_id)
    return (await env.client.get(f"/api/jobs/{job_id}")).json()


async def test_purge_revoke_停用业务用户并留痕(env, monkeypatch):
    iid = await release_instance(env)
    hub = arm(env, monkeypatch)
    await seed_admin_key(env)
    job = await run_purge_job(env, iid, {"memory_disposal": "revoke"})
    assert job["state"] == "succeeded", job["error"]
    st = memory_step(job, "memory_disposal")
    assert st["seq"] == 15 and st["state"] == "succeeded"
    assert "回收 2 把" in st["detail"] and "Hub 卷" in st["detail"]
    assert hub.revoke_calls == ["dshctl-dsh-01"]
    evs = await events_of(env, "purge_memory_revoked")
    assert evs and evs[0].data["user_id"] == "dshctl-dsh-01"


async def test_purge_revoke_材料不全标skipped不挡终态化(env, monkeypatch):
    iid = await release_instance(env)
    arm(env, monkeypatch)                       # 站点开了记忆，但 admin key 没入库
    job = await run_purge_job(env, iid, {"memory_disposal": "revoke"})
    # 缺 admin key 属「材料不全」：revoke 标 skipped 说破人工去向，purge 照走完
    assert job["state"] == "succeeded", job["error"]
    st = memory_step(job, "memory_disposal")
    assert st["state"] == "skipped" and "人工" in st["detail"]
    assert await events_of(env, "purge_memory_revoke_manual")


async def test_purge_revoke_中途失败则停手数据不删(env, monkeypatch):
    from dsh_control.integrations import base
    iid = await release_instance(env)
    hub = arm(env, monkeypatch)
    await seed_admin_key(env)

    async def boom(user_id):
        raise base.IntegrationError("假 core 不可达", code="core_unreachable")
    hub.revoke_user_keys = boom
    job = await run_purge_job(env, iid, {"memory_disposal": "revoke"})
    # 显式要求停用却没停成：宁可停下让用户重跑，也不能删了数据再宣布停用
    assert job["state"] == "failed" and "重跑" in job["error"]
    assert memory_step(job, "memory_disposal")["state"] == "failed"
    journal = "\n".join(env.providers.executor.commands)
    assert "rm -rf" not in journal and "docker rm" not in journal


# ---------------------------------------------------------------- watcher 探针

async def test_watcher_记忆栈探针只在跳变沿记事件(env, monkeypatch):
    for k, v in MEM_SETTINGS.items():
        setattr(env.settings, k, v)
    hub = FakeHub()
    hub.core_ok = False
    monkeypatch.setattr(env.app.state, "watcher_enabled", False, raising=False)
    async def fake_build(s, settings, *, recorder=None):
        return hub, ""
    monkeypatch.setattr("dsh_control.watcher.build_memory_hub", fake_build)
    monkeypatch.setattr("dsh_control.watcher.probe_tcp", _true_tcp)

    wstate = WatcherState()
    for _ in range(3):                                  # 一直挂：只记第一条
        await scan_once(env.db, env.providers, env.settings, None, wstate)
    assert len(await events_of(env, "memory_stack_down")) == 1
    hub.core_ok = True
    await scan_once(env.db, env.providers, env.settings, None, wstate)
    assert len(await events_of(env, "memory_stack_recovered")) == 1
    await scan_once(env.db, env.providers, env.settings, None, wstate)
    assert len(await events_of(env, "memory_stack_recovered")) == 1   # 不刷第二条
    assert not await events_of(env, "auto_restart")    # 探针不参与自动重启
