"""一键登录 URL 永远最新（用户故事 4）：手动重启、docker 自拉起、/token 现取。"""

from __future__ import annotations

import json

from tests.conftest import XRW, wait_job


async def deploy(env) -> dict:
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    return body


async def detail(env, iid: int) -> dict:
    return (await env.client.get(f"/api/instances/{iid}")).json()


async def test_detail_shows_newest_token_after_manual_restart(env):
    body = await deploy(env)
    iid = body["instance_id"]
    first = (await detail(env, iid))["access"]["token"]

    r = await env.client.post(f"/api/instances/{iid}/restart", headers=XRW)
    assert r.status_code == 200
    await wait_job(env.app, r.json()["job_id"])

    d = await detail(env, iid)
    new = d["access"]["token"]
    # SEED.md §3 第 4 步：重启后 token 变了，且详情拿到的是新的完整值
    assert new and new != first
    assert env.runtime.token == new
    assert new in d["access"]["url"]
    assert d["access"]["captured_at"] is not None
    assert d["state"] == "running"


async def test_restart_steps_order_and_token_capture_failure(env):
    body = await deploy(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/restart", headers=XRW)
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert [st["key"] for st in job["steps"]] == ["container_restart", "wait_healthy",
                                                  "token_capture", "probe_public"]
    assert job["state"] == "succeeded"


async def test_token_endpoint_recaptures_live_value(env):
    body = await deploy(env)
    iid = body["instance_id"]
    # 容器侧又换了一次密钥（比如容器内部重启），/token 必须现取到最新
    live = env.runtime.advance_token()
    r = await env.client.get(f"/api/instances/{iid}/token", headers=XRW)
    assert r.status_code == 200
    got = r.json()
    assert got["token"] == live and live in got["url"]
    d = await detail(env, iid)
    assert d["access"]["token"] == live
    # 不带 XRW 的写式取用被拒
    r = await env.client.get(f"/api/instances/{iid}/token")
    assert r.status_code == 403


async def test_events_never_contain_plaintext_token(env):
    body = await deploy(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/restart", headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    blob = json.dumps(evs, ensure_ascii=False)
    token = env.runtime.token
    assert len(token) > 6
    assert token not in blob  # 事件里只有打码形态
    kinds = [e["kind"] for e in evs]
    assert "token_captured" in kinds


async def test_token_not_found_returns_502(env):
    body = await deploy(env)
    iid = body["instance_id"]

    async def empty_token(container):  # 容器日志里还没有 ?token= 地址
        return None

    env.runtime.latest_token = empty_token
    r = await env.client.get(f"/api/instances/{iid}/token", headers=XRW)
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "token_not_found"
