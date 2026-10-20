"""一键登录 URL 永远最新（用户故事 4）：手动重启、docker 自拉起、/token 现取。"""

from __future__ import annotations

import json

from tests.conftest import XRW, create_instance, wait_job


async def deploy(env) -> dict:
    r = await create_instance(env.client)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    return body


async def detail(env, iid: int) -> dict:
    return (await env.client.get(f"/api/instances/{iid}")).json()


async def test_detail_shows_newest_token_after_manual_restart(env):
    from dsh_control.domain.slug import token_mask

    body = await deploy(env)
    iid = body["instance_id"]
    first = (await env.client.get(f"/api/instances/{iid}/token",
                                  headers=XRW)).json()["token"]

    r = await env.client.post(f"/api/instances/{iid}/restart", headers=XRW)
    assert r.status_code == 200
    await wait_job(env.app, r.json()["job_id"])

    # SEED.md §3 第 4 步：重启后一键地址变了，reveal 接口现取到新完整值
    new = (await env.client.get(f"/api/instances/{iid}/token",
                                headers=XRW)).json()["token"]
    assert new and new != first
    assert env.runtime.token == new
    assert new in (await env.client.get(f"/api/instances/{iid}/token",
                                        headers=XRW)).json()["url"]
    # 详情退回报备：mask 跟着换成新密钥的 mask，且不给完整值
    d = await detail(env, iid)
    assert d["access"]["token_masked"] == token_mask(new)
    assert "token" not in d["access"] and "url" not in d["access"]
    assert d["access"]["captured_at"] is not None
    assert d["state"] == "running"


async def test_restart_steps_order_and_token_capture_failure(env):
    body = await deploy(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/restart", headers=XRW)
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    # 第 5 步 capabilities 在重启之前：智能体团队开在 home 层补丁里，
    # DSH 只在启动时组合一次配置，铺完必须紧跟这次重启才带上。
    assert [st["key"] for st in job["steps"]] == ["capabilities", "container_restart",
                                                  "wait_healthy", "token_capture",
                                                  "probe_public"]
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
    from dsh_control.domain.slug import token_mask
    assert d["access"]["token_masked"] == token_mask(live)
    assert "token" not in d["access"]
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


async def test_list_and_detail_leak_no_full_token(env):
    """列表连 mask 都不给；详情只给 mask。全值只在 /token reveal 里。"""
    r = await create_instance(env.client)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    blob_list = (await env.client.get("/api/instances")).text
    blob_detail = (await env.client.get(f"/api/instances/{body['instance_id']}")).text
    assert env.runtime.token not in blob_list
    assert env.runtime.token not in blob_detail
    item = (await env.client.get("/api/instances")).json()["items"][0]
    assert "token" not in str(item.keys())
    assert "access" not in item  # 列表没有 access 块
