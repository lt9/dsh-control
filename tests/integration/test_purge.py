"""purge（§6.4）：确认词、仅 released、全步走通、数据目录护栏、watcher 自动入队。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from dsh_control.db import repo
from dsh_control.db.models import (
    DeployJob,
    Event,
    InstanceSnapshot,
    InstanceToken,
)
from dsh_control.integrations.base import ExecResult
from dsh_control.watcher import WatcherState, scan_once
from tests.conftest import XRW, create_instance, wait_job


async def deploy(env) -> dict:
    r = await create_instance(env.client)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    return body


async def release(env, iid: int, slug: str = "dsh-01") -> None:
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": slug}, headers=XRW)
    assert r.status_code == 200
    await wait_job(env.app, r.json()["job_id"])


async def jobs_for(env, kind: str) -> list[DeployJob]:
    async with env.db.session() as s:
        return list((await s.scalars(
            select(DeployJob).where(DeployJob.kind == kind).order_by(DeployJob.id))).all())


def allow_data(env, slug: str) -> None:
    """让假主机的实例 .env 返回一个合规的生效 DATA_DIR（purge 只认这个生效值）。"""
    env.providers.executor.handlers.insert(
        0, (f"instances/{slug}/.env",
            ExecResult(0, f"DATA_DIR=/hdd1/dsh-data/{slug}\n", "")))


async def test_purge_confirm_mismatch_422_and_no_side_effects(env):
    body = await deploy(env)
    iid = body["instance_id"]
    await release(env, iid)
    for wrong in ("dsh-1", "DSH-01", "dsh-01 ", "confirm", ""):
        r = await env.client.post(f"/api/instances/{iid}/purge",
                                  json={"confirm": wrong}, headers=XRW)
        assert r.status_code == 422, wrong
        assert r.json()["detail"]["error"] == "confirm_mismatch"
    # 422 不许产生任何副作用：没有工单、没有 rm
    assert await jobs_for(env, "purge") == []
    assert not any("rm -rf" in c for c in env.providers.executor.commands)
    assert not any("docker rm" in c for c in env.providers.executor.commands)


async def test_purge_only_from_released(env):
    body = await deploy(env)
    iid = body["instance_id"]
    # running：数据还在用，绝不许 purge
    r = await env.client.post(f"/api/instances/{iid}/purge",
                              json={"confirm": "dsh-01"}, headers=XRW)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "cannot_purge_from_running"
    # pending 的机器同样拒（错误码带上真实状态）
    r2 = await create_instance(env.client)
    iid2 = r2.json()["instance_id"]
    r = await env.client.post(f"/api/instances/{iid2}/purge",
                              json={"confirm": "dsh-02"}, headers=XRW)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "cannot_purge_from_pending"
    # released → 允许；purged → 终态再拒
    await release(env, iid)
    allow_data(env, "dsh-01")
    r = await env.client.post(f"/api/instances/{iid}/purge",
                              json={"confirm": "dsh-01"}, headers=XRW)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    assert (await env.client.get(f"/api/jobs/{job_id}")).json()["state"] == "succeeded"
    r = await env.client.post(f"/api/instances/{iid}/purge",
                              json={"confirm": "dsh-01"}, headers=XRW)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "cannot_purge_from_purged"
    # 收尾等 dsh-02 的开通工单跑完，别留活任务撞 fixture 拆除
    await wait_job(env.app, r2.json()["job_id"])


async def test_purge_full_flow_marks_purged(env):
    body = await deploy(env)
    iid = body["instance_id"]
    await release(env, iid)
    allow_data(env, "dsh-01")
    # 释放已经删过外部对象——purge 的补删步要对「库里已清」的形态无话可说地通过
    r = await env.client.post(f"/api/instances/{iid}/purge",
                              json={"confirm": "dsh-01"}, headers=XRW)
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert job["kind"] == "purge"
    # 步骤 seq/key 逐字照契约 §6.4 的表
    assert [(st["seq"], st["key"]) for st in job["steps"]] == [
        (10, "confirm_guard"), (20, "external_cleanup"), (30, "container_remove"),
        (40, "data_delete"), (50, "mark_purged")]

    # 容器：先 stop（释放时已停，这里不该再 stop）再 rm；数据目录 rm -rf 过护栏的路径
    cmds = env.providers.executor.commands
    assert any("docker rm dsh-dsh-01" in c for c in cmds), cmds
    assert any("rm -rf" in c and "/hdd1/dsh-data/dsh-01" in c for c in cmds), cmds
    assert not any("docker rmi" in c or "image rm" in c for c in cmds), "镜像不许动"

    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "purged"
    assert d["purged_at"] is not None
    # purged 只读：全部动作关（含 restore/purge/upgrade/deploy）
    assert d["actions"] == {"restart": False, "release": False, "deploy": False,
                            "restore": False, "purge": False, "upgrade": False}
    # purged 不给 purge_after（已经删完了，再显示「还剩 N 天」是说假话）
    assert d["purge_after"] is None
    # 外部登记清空（含端口放回池子）
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.dns_record_id in ("", None)
        assert inst.frp_proxy_id is None
        assert inst.frp_remote_port is None
        assert inst.ng_server_id is None
        assert inst.ng_upstream_id is None
        assert inst.purged_at is not None
    # 事件：instance_purged 带 auto/manual 与删了什么
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    purged_ev = next(e for e in evs if e["kind"] == "instance_purged")
    assert purged_ev["data"]["source"] == "manual"
    assert purged_ev["data"]["deleted"].get("container") == "dsh-dsh-01"
    assert "/hdd1/dsh-data/dsh-01" in str(purged_ev["data"]["deleted"])
    # 列表行带 purged_at，purge_after 为 null
    row = next(i for i in (await env.client.get("/api/instances")).json()["items"]
               if i["id"] == iid)
    assert row["state"] == "purged" and row["purged_at"] is not None
    assert row["purge_after"] is None


async def test_purge_deletes_leftover_external_ids(env):
    """释放删了一半（库里还挂着 id）→ purge 补删；面板 404 视为已删。"""
    body = await deploy(env)
    iid = body["instance_id"]
    # 不走 release：手工把实例摆成 released，外部对象与 id 原样留着
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        inst.state = "released"
        inst.desired_state = "released"
        inst.released_at = datetime.now(UTC)
        await s.commit()
    allow_data(env, "dsh-01")
    r = await env.client.post(f"/api/instances/{iid}/purge",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    assert await env.providers.dns.find("dsh-01") is None
    assert env.providers.frp.tunnels == {}
    assert await env.providers.ng.find_site("dsh-01.example.test") is None
    # 补删的 id 逐个记进事件
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    kinds = {e["kind"] for e in evs}
    assert {"purge_dns_deleted", "purge_frp_deleted", "purge_ng_deleted"} <= kinds


async def test_purge_data_guardrail_refuses_outside_paths(env):
    """护栏逐条：越界/等于数据根/兄弟前缀/相对路径/读不到生效值——
    任何一条不满足宁可不删。"""
    cases = [
        ("DATA_DIR=/tmp/evil\n", "完全不在主机数据根目录下"),
        ("DATA_DIR=/hdd1/dsh-data\n", "就是数据根目录本身"),
        ("DATA_DIR=/hdd1/dsh-data-extra/dsh-01\n", "兄弟前缀目录不算在根之下"),
        ("DATA_DIR=relative/dsh-01\n", "相对路径"),
        ("HOST_PORT=8080\n", ".env 里压根没有 DATA_DIR——路径只认生效值，读不到不猜"),
    ]
    for env_txt, why in cases:
        body = await deploy(env)
        iid = body["instance_id"]
        slug = body["dns_name"].split(".")[0]
        await release(env, iid, slug)
        env.providers.executor.handlers.insert(
            0, (f"instances/{slug}/.env", ExecResult(0, env_txt, "")))
        r = await env.client.post(f"/api/instances/{iid}/purge",
                                  json={"confirm": slug}, headers=XRW)
        job_id = r.json()["job_id"]
        await wait_job(env.app, job_id)
        job = (await env.client.get(f"/api/jobs/{job_id}")).json()
        assert job["state"] == "failed", f"{why}：护栏必须拦下"
        assert "护栏" in (job["error"] or "")
        # 一个 rm -rf 都不许发出去
        assert not any("rm -rf" in c for c in env.providers.executor.commands), why
        # 实例停在 released：工单失败不改判、还能重跑或改走恢复
        d = (await env.client.get(f"/api/instances/{iid}")).json()
        assert d["state"] == "released", why
        assert d["actions"]["purge"] is True
        # 还原 handler，免得影响下一轮
        env.providers.executor.handlers.pop(0)


async def test_purge_aborts_if_instance_restored_midflight(env):
    """入队后、执行前实例被恢复（deploy 续建）：confirm_guard 看到 state≠released
    必须中止——对跑着的实例删容器删数据是灾难。"""
    body = await deploy(env)
    iid = body["instance_id"]
    await release(env, iid)
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        job = await repo.create_job(s, inst.id, "purge",
                                    {"source": "auto", "purge_after_days": 0})
        inst.state = "running"   # 模拟中途恢复成功
        await s.commit()
    env.app.state.runner.spawn(job.id)
    await wait_job(env.app, job.id)
    j = (await env.client.get(f"/api/jobs/{job.id}")).json()
    assert j["state"] == "failed"
    assert "不是 released" in (j["error"] or "")
    assert not any("docker rm" in c for c in env.providers.executor.commands)
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "running"


async def test_watcher_auto_purge_queue_and_live_site_value(env):
    """released 到期自动入队 purge；保留期每轮现读库值（改了立即生效）；
    有未完工单不重复入队。"""
    body = await deploy(env)
    iid = body["instance_id"]
    await release(env, iid)
    now = datetime.now(UTC)
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        # 手工把 released_at 摆到 10 天前（照 sqlite 的 naive-UTC 回读形态）
        inst.released_at = (now - timedelta(days=10)).replace(tzinfo=None)
        await s.commit()
    # 默认保留期 15 天：还没到期
    await scan_once(env.db, env.providers, env.settings, None, WatcherState(), now=now)
    assert await jobs_for(env, "purge") == []
    # 把保留期改到库里（5 天）：下一轮立即生效，不需要重启、不吃启动缓存
    async with env.db.session() as s:
        await repo.upsert_credential(s, kind="site", name="site",
                                     secret={"purge_after_days": 5}, note="")
    await scan_once(env.db, env.providers, env.settings, None, WatcherState(), now=now)
    (job,) = await jobs_for(env, "purge")
    assert job.state == "pending"
    assert job.params["source"] == "auto"
    async with env.db.session() as s:
        ev = (await s.scalars(select(Event).where(Event.kind == "purge_queued"))).one()
        assert ev.instance_id == iid and ev.job_id == job.id
    # 再扫一轮：已有未完工单，不许入第二个 purge
    await scan_once(env.db, env.providers, env.settings, None, WatcherState(), now=now)
    assert len(await jobs_for(env, "purge")) == 1
    # released 依旧不采集不重启：扫描前后它的快照数量纹丝不动，状态也没变
    async def snap_count() -> int:
        async with env.db.session() as s:
            return int((await s.scalar(
                select(func.count()).select_from(InstanceSnapshot)
                .where(InstanceSnapshot.instance_id == iid))) or 0)

    before = await snap_count()
    await scan_once(env.db, env.providers, env.settings, None, WatcherState(), now=now)
    assert await snap_count() == before
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.state == "released"


async def test_watcher_token_unchanged_is_silent(env):
    """§3 去重：自动重抓路径保留，但值没变 → 零落库零事件；变了 → 恰好一条。"""
    await deploy(env)
    wstate = WatcherState()
    await scan_once(env.db, env.providers, env.settings, None, wstate)

    async def token_rows() -> list[InstanceToken]:
        async with env.db.session() as s:
            return list((await s.scalars(select(InstanceToken))).all())

    async def ev_kinds() -> list[str]:
        async with env.db.session() as s:
            return [e.kind for e in (await s.scalars(select(Event))).all()]

    before = len(await token_rows())
    # docker 策略自拉起（restart_count 增长）但密钥没轮换：静默，什么都不写
    env.runtime.restart_count = 1
    await scan_once(env.db, env.providers, env.settings, None, wstate)
    assert len(await token_rows()) == before
    assert "docker_auto_restarted" not in await ev_kinds()
    # 又自拉起一次，这回密钥真换了：恰好一行 + 一条事件
    env.runtime.restart_count = 2
    env.runtime.advance_token()
    await scan_once(env.db, env.providers, env.settings, None, wstate)
    assert len(await token_rows()) == before + 1
    kinds = await ev_kinds()
    assert kinds.count("docker_auto_restarted") == 1
