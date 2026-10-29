"""能力 API：目录、台账、一键动作（ADR-0001 决策三 b）。

lead 还没在 main.py 挂本路由（wiring 清单在报告里），测试自挂自测——
挂法就一行 include_router，与 /api 前缀下其它路由无异。
"""

from __future__ import annotations

from sqlalchemy import select

from dsh_control.api.capabilities import router as cap_router
from dsh_control.db.models import Capability, DeployJob, DeployStep, InstanceCapability
from dsh_control.integrations import capability_engine as engine
from dsh_control.integrations.base import ExecResult
from tests.conftest import XRW, create_instance, wait_job


async def _setup(env):
    """挂路由 + 把 kit 里的 speckit 包发进目录 + 假主机上「包已到货」。"""
    env.app.include_router(cap_router, prefix="/api")
    packs = engine.packs_from_dir("kit/capabilities/speckit")
    async with env.db.session() as s:
        await engine.upsert_catalog(s, packs, kit_version="0.0.0-test", note="测试发布")
    r = await create_instance(env.client)
    assert r.status_code == 200, r.text
    body = r.json()
    await wait_job(env.app, body["job_id"])
    # 假执行器：pack 到货探针回 yes（部署完了再插，别干扰部署自身的判据）
    env.providers.executor.handlers.insert(0, ("[ -f ", ExecResult(0, "yes\n", "")))
    return body["instance_id"]


async def _job(env, job_id: int) -> dict:
    async with env.db.session() as s:
        job = await s.get(DeployJob, job_id)
        steps = (await s.scalars(
            select(DeployStep).where(DeployStep.job_id == job_id)
            .order_by(DeployStep.seq))).all()
        return {"status": job.state,
                "steps": {x.key: x.state for x in steps},
                "error": job.error or ""}


async def test_目录列表含speckit且current打星(env) -> None:
    await _setup(env)
    r = await env.client.get("/api/capabilities")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    row = next(x for x in items if x["capability_id"] == "speckit")
    assert row["version"] == "1.0.0" and row["kind"] == "skill"
    assert row["state"] == "published" and row["current"] is True
    assert row["compatible_dsh"] == ">=0.2.0-rc.1"


async def test_一键安装走工单并回写台账(env) -> None:
    iid = await _setup(env)
    r = await env.client.post(f"/api/instances/{iid}/capability",
                              json={"action": "install", "capability": "speckit"},
                              headers=XRW)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    await env.app.state.runner.wait(job_id, 15)
    info = await _job(env, job_id)
    assert info["status"] == "succeeded", info
    assert info["steps"]["capability_resolve"] == "succeeded"
    assert info["steps"]["capability_action"] == "succeeded"
    assert info["steps"]["capability_observe"] == "succeeded"

    led = await env.client.get(f"/api/instances/{iid}/capabilities")
    row = next(x for x in led.json()["items"] if x["capability_id"] == "speckit")
    assert row["desired_state"] == "present"
    assert row["installed"] is False           # 假执行器不会真落文件
    assert row["drift"] is True                # 期望在、探针说没装 = 要重试的漂移
    assert row["unknown_version"] is False     # 「没见过」和「装了但版本读不到」分开
    assert row["last_job_id"] == job_id


async def test_禁用后apply是skipped不是失败(env) -> None:
    iid = await _setup(env)
    for action in ("install", "disable"):
        r = await env.client.post(f"/api/instances/{iid}/capability",
                                  json={"action": action, "capability": "speckit"},
                                  headers=XRW)
        await env.app.state.runner.wait(r.json()["job_id"], 15)
    r = await env.client.post(f"/api/instances/{iid}/capability",
                              json={"action": "apply", "capability": "speckit"},
                              headers=XRW)
    job_id = r.json()["job_id"]
    await env.app.state.runner.wait(job_id, 15)
    info = await _job(env, job_id)
    assert info["status"] == "succeeded"
    assert info["steps"]["capability_action"] == "skipped"    # 三态：skipped ≠ failed

    async with env.db.session() as s:
        led = await s.scalar(select(InstanceCapability).where(
            InstanceCapability.instance_id == iid,
            InstanceCapability.capability_id == "speckit"))
        assert led.desired_state == "present" and led.enabled is False


async def test_动作校验与守卫(env) -> None:
    iid = await _setup(env)
    # 目录里没有的货：422（uninstall 不查目录——卸载不需要货在架）
    r = await env.client.post(f"/api/instances/{iid}/capability",
                              json={"action": "install", "capability": "ghost"},
                              headers=XRW)
    assert r.status_code == 422
    # 写操作缺 X-Requested-With：403（全站 CSRF 惯例，先于会话判）
    r = await env.client.post(f"/api/instances/{iid}/capability",
                              json={"action": "install", "capability": "speckit"})
    assert r.status_code == 403
    # 有 XRW 但没会话：401（env.client 登录过带 cookie，另起一个裸客户端）
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=env.app),
                           base_url="http://test") as raw:
        r = await raw.post(f"/api/instances/{iid}/capability",
                           json={"action": "install", "capability": "speckit"},
                           headers={"X-Requested-With": "1"})
        assert r.status_code == 401, r.text
    # 未知动作：422（pydantic 层就拦）
    r = await env.client.post(f"/api/instances/{iid}/capability",
                              json={"action": "nuke", "capability": "speckit"},
                              headers=XRW)
    assert r.status_code == 422
    # 不存在的实例：404
    r = await env.client.post("/api/instances/9999/capability",
                              json={"action": "uninstall", "capability": "speckit"},
                              headers=XRW)
    assert r.status_code == 404


async def test_兼容闸门拦在安装步骤里(env) -> None:
    """假运行时报 0.9.9——放行；改成目录过不了的区间就该拦。"""
    iid = await _setup(env)
    async with env.db.session() as s:
        row = await s.scalar(select(Capability).where(
            Capability.capability_id == "speckit"))
        row.compatible_dsh = ">=99.0.0"
        await s.commit()
    r = await env.client.post(f"/api/instances/{iid}/capability",
                              json={"action": "install", "capability": "speckit"},
                              headers=XRW)
    job_id = r.json()["job_id"]
    await env.app.state.runner.wait(job_id, 15)
    info = await _job(env, job_id)
    assert info["status"] == "failed" and "兼容闸门" in info["error"]
