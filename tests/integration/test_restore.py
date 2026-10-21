"""恢复（§6.3.1）与升级：released 走 deploy 续建、原公网端口赎回、body.image、
详情/列表的 purge_after 与 actions 新字段（§4）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from dsh_control.db import repo
from dsh_control.db.models import Instance
from dsh_control.domain.timeutil import iso
from tests.conftest import XRW, create_instance, wait_job


async def deploy(env) -> dict:
    r = await create_instance(env.client)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    return body


async def release(env, iid: int) -> None:
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    assert r.status_code == 200
    await wait_job(env.app, r.json()["job_id"])


async def events(env, iid: int) -> list[dict]:
    return (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]


async def test_restore_reuses_names_and_original_public_port(env):
    body = await deploy(env)
    iid = body["instance_id"]
    await release(env, iid)
    before = (await env.client.get(f"/api/instances/{iid}")).json()
    assert before["network"]["frp_remote_port"] == 20001   # 释放特意保留的端口

    r = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    assert r.status_code == 200, r.text      # released → deploy = 恢复
    j = (await env.client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    assert j["purpose"] == "restore", "恢复工单必须自带来路（§4），前端不靠 URL 猜"
    await wait_job(env.app, r.json()["job_id"])
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "running", d["last_error"]
    # 原 slug/容器名/公网端口原样回来（§9「原域名原端口回来」）
    assert d["slug"] == "dsh-01"
    assert d["network"]["frp_remote_port"] == 20001
    ensures = [ln for ln in env.journal if ln.startswith("frp.ensure")]
    assert ensures and "dsh_01" in ensures[-1] and "remote=20001" in ensures[-1], env.journal
    # 三处外部对象重建
    assert await env.providers.dns.find("dsh-01") is not None
    assert await env.providers.ng.find_site("dsh-01.example.test") is not None
    # desired_state 翻回 running：恢复后 watcher 必须重新开始守护
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.desired_state == "running"
    # 事件：从 released 起头记 deploy_requeued(from=released)，不新增 kind
    assert any(e["kind"] == "deploy_requeued" and (e.get("data") or {}).get("from") == "released"
               for e in await events(env, iid))
    # 恢复后不再显示保留期：purge_after 归 null，upgrade 按钮亮起
    assert d["purge_after"] is None
    assert d["actions"]["upgrade"] is True and d["actions"]["restore"] is False


async def test_restore_falls_back_when_original_port_taken(env):
    body = await deploy(env)
    iid = body["instance_id"]
    await release(env, iid)
    # 原端口被别人的隧道占了：先探占用，被占才回落原分配逻辑，并 event 说破
    from dsh_control.integrations.base import Tunnel

    env.providers.frp.tunnels[999] = Tunnel(proxy_id=999, name="other_99",
                                            client_id="office-frpc@1",
                                            server_id="s", remote_port=20001)
    env.providers.frp.taken.add(20001)
    r = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "running", d["last_error"]
    assert d["network"]["frp_remote_port"] == 20002, "被占就换下一个，不硬抢别人的门"
    fb = next(e for e in await events(env, iid) if e["kind"] == "frp_port_fallback")
    assert "被占" in fb["message"] and "20002" in fb["message"], fb


async def test_deploy_body_image_overrides_site_default(env):
    """POST deploy body {"image": "tag"}：容器步与第 20 步镜像在位检查都优先用它，
    留空回站点当前目标 tag。"""
    body = await deploy(env)
    iid = body["instance_id"]
    cmds = env.providers.executor.commands
    n_before = len(cmds)
    r = await env.client.post(f"/api/instances/{iid}/deploy",
                              json={"image": "dsh-test:v11"}, headers=XRW)
    assert r.status_code == 200, r.text
    await wait_job(env.app, r.json()["job_id"])
    jid = r.json()["job_id"]
    job = (await env.client.get(f"/api/jobs/{jid}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert job["purpose"] == "upgrade", "running 续建 = 升级来路（§4）"
    this_job = cmds[n_before:]
    assert any("DSH_IMAGE=dsh-test:v11" in c for c in this_job), this_job
    assert not any("docker image inspect 'dsh-test:v9'" in c for c in this_job), \
        "同一张工单里站点默认 tag 不许混进来（第 20 步在位检查也要认工单 tag）"
    assert any("docker image inspect 'dsh-test:v11'" in c for c in this_job), this_job
    logs = (await env.client.get(f"/api/jobs/{jid}/logs")).json()["items"]
    assert any("本工单指定 tag" in ln["line"] and "dsh-test:v11" in ln["line"]
               for ln in logs), "计划日志要说破镜像 tag 是从工单来的"

    # 留空 body → 回站点当前默认 tag（假站点默认 dsh-test:v9）
    n2 = len(cmds)
    r2 = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    await wait_job(env.app, r2.json()["job_id"])
    assert any("DSH_IMAGE=dsh-test:v9" in c for c in cmds[n2:])


async def test_purge_after_and_actions_fields(env):
    """purge_after：仅 released 给，= released_at + 当前库值；改库值立刻跟着变。"""
    body = await deploy(env)
    iid = body["instance_id"]
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["purge_after"] is None, "running 不给保留期字段"
    assert d["actions"] == {"restart": True, "release": True, "deploy": False,
                            "restore": False, "purge": False, "upgrade": True}

    await release(env, iid)
    async with env.db.session() as s:
        released_at = (await repo.get_instance(s, iid)).released_at
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["purge_after"] == iso(released_at + timedelta(days=15)), d["purge_after"]
    assert d["purged_at"] is None
    assert d["actions"] == {"restart": False, "release": False, "deploy": False,
                            "restore": True, "purge": True, "upgrade": False}
    row = next(i for i in (await env.client.get("/api/instances")).json()["items"]
               if i["id"] == iid)
    assert row["purge_after"] == d["purge_after"], "列表行也要能显示还剩几天"
    assert row["purged_at"] is None

    # 改库里的保留期（site 行）→ 详情立刻用新值，不用重启
    async with env.db.session() as s:
        await repo.upsert_credential(s, kind="site", name="site",
                                     secret={"purge_after_days": 3}, note="")
    d2 = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d2["purge_after"] == iso(released_at + timedelta(days=3)), d2["purge_after"]

    # 手工摆一个 purged：purge_after 必为 null（终态不显示未来日期）
    async with env.db.session() as s:
        inst = (await s.scalars(select(Instance).where(Instance.id == iid))).one()
        inst.state = "purged"
        inst.purged_at = datetime.now(UTC)
        await s.commit()
    d3 = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d3["purge_after"] is None and d3["purged_at"] is not None
    assert not any(d3["actions"].values())


async def test_job_purpose_marks_origin_for_every_kind(env):
    """§4 purpose：deploy 工单自带三用来路（create/upgrade/restore）；
    其余 kind 一律 null。工单页据此出标题，不靠 URL 上的 ?op= 猜。"""
    body = await deploy(env)
    j = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert j["kind"] == "deploy" and j["purpose"] == "create", j
    await release(env, body["instance_id"])
    rel = next(e for e in await events(env, body["instance_id"])
               if e["kind"] == "release_queued")
    j2 = (await env.client.get(f"/api/jobs/{rel['job_id']}")).json()
    assert j2["kind"] == "release" and j2["purpose"] is None, j2
