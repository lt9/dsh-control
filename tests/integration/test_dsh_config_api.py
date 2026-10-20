"""详情页那格「DSH 配置」：随部署落一次快照，页面上可以手动重新读取。

这条接口存在的理由是"她问的是模型/技能/团队开没开，不是容器端口"，而它的取数
要 ssh 进容器（约 0.6 秒），所以**不能**塞进详情接口本身——台账页一次 ssh 都不做。
"""

from __future__ import annotations

from tests.conftest import XRW, create_instance, wait_job


async def deploy(env) -> dict:
    body = (await create_instance(env.client)).json()
    await wait_job(env.app, body["job_id"])
    return body


async def test_部署后详情里就带着_dsh_那一组(env):
    iid = (await deploy(env))["instance_id"]
    detail = (await env.client.get(f"/api/instances/{iid}")).json()
    dsh = detail["dsh_config"]
    assert dsh is not None, "第 66 步没把 DSH 配置写进快照"
    assert dsh["model"] == "Qwen3.8-Flash-Next"
    assert dsh["base_url"] == "http://192.0.2.10:8080/v1"
    assert dsh["agent_team"] is True
    assert dsh["skills"] == ["comfy-image"]
    titles = [g["title"] for g in dsh["groups"]]
    for want in ("模型", "生图", "插件与智能体团队", "技能（Skills）"):
        assert want in titles
    # 密钥只放环境变量名，值一个都不许出现在这里
    assert "LLAMA_API_KEY" in str(dsh)


async def test_重新读取要运维密钥否则_403(env):
    iid = (await deploy(env))["instance_id"]
    r = await env.client.get(f"/api/instances/{iid}/dsh-config")
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "missing_x_requested_with"


async def test_重新读取刷新快照并落一条事件(env):
    iid = (await deploy(env))["instance_id"]
    before = (await env.client.get(f"/api/instances/{iid}")).json()["dsh_config"]
    r = await env.client.get(f"/api/instances/{iid}/dsh-config", headers=XRW)
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["captured_at"]
    assert fresh["model"] == before["model"]

    after = (await env.client.get(f"/api/instances/{iid}")).json()["dsh_config"]
    assert after["captured_at"] == fresh["captured_at"], "刷新没落进快照"
    events = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    assert any(e["kind"] == "config_refreshed" for e in events)


async def test_容器读不到时降级并且不动旧快照(env):
    """一次 ssh 超时不能让详情页变成"她什么都没配"。"""
    iid = (await deploy(env))["instance_id"]
    old = (await env.client.get(f"/api/instances/{iid}")).json()["dsh_config"]
    assert old is not None

    env.runtime.exists = False          # dump_config 与 container_facts 都给空
    r = await env.client.get(f"/api/instances/{iid}/dsh-config", headers=XRW)
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "dsh_config_unavailable"

    still = (await env.client.get(f"/api/instances/{iid}")).json()["dsh_config"]
    assert still["model"] == old["model"], "读失败却把旧快照清了"
