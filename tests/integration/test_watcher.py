"""watcher：三个抑制条件、备份闪断不误伤、docker 自拉起后重抓 token（§3）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from dsh_control.db import repo
from dsh_control.db.models import DeployJob, Instance
from dsh_control.watcher import WatcherState, maybe_auto_restart, scan_once
from tests.conftest import make_settings

CN = ZoneInfo("Asia/Shanghai")
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=CN)
IN_WINDOW = datetime(2026, 10, 20, 3, 30, tzinfo=CN)


async def make_running_instance(env) -> int:
    async with env.db.session() as s:
        inst = Instance(slug="dsh-09", host_id=env.host_nas, container_name="dsh-09",
                        dns_name="dsh-09.example.test",
                        public_url="https://dsh-09.example.test",
                        state="running", desired_state="running")
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        return inst.id


async def job_count(env) -> int:
    async with env.db.session() as s:
        return await s.scalar(select(func.count()).select_from(DeployJob)) or 0


async def scan(env, wstate, now=NOON, runner=None):
    await scan_once(env.db, env.providers, env.settings, runner, wstate, now=now)


async def test_single_unhealthy_blip_never_restarts(env):
    """备份让健康闪一次 unhealthy：够不到连续 3 次，恢复即清零。"""
    iid = await make_running_instance(env)
    wstate = WatcherState()
    env.runtime.health = "unhealthy"
    await scan(env, wstate)
    assert wstate.failures[iid] == 1
    assert await job_count(env) == 0
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.state == "unhealthy"
    # 恢复 healthy：计数清零、状态回 running
    env.runtime.health = "healthy"
    await scan(env, wstate)
    assert wstate.failures[iid] == 0
    async with env.db.session() as s:
        assert (await repo.get_instance(s, iid)).state == "running"
    assert await job_count(env) == 0


async def test_three_consecutive_failures_trigger_job(env):
    iid = await make_running_instance(env)
    wstate = WatcherState()
    env.runtime.health = "unhealthy"
    await scan(env, wstate)
    await scan(env, wstate)
    assert await job_count(env) == 0  # 2 次还不够
    # 第 3 次：跨过阈值
    await scan(env, wstate)
    assert await job_count(env) == 1
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.state == "restarting"
        job = (await s.scalars(select(DeployJob))).one()
        assert job.kind == "restart"
        from dsh_control.db.models import Event
        ev = (await s.scalars(select(Event).where(Event.kind == "auto_restart"))).one()
        assert ev.instance_id == iid


async def test_suppressed_inside_backup_window(env):
    iid = await make_running_instance(env)
    wstate = WatcherState(failures={iid: 5})
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        triggered = await maybe_auto_restart(s, inst, wstate, env.settings, None,
                                             IN_WINDOW)
    assert triggered is False
    assert await job_count(env) == 0


async def test_suppressed_within_cooldown(env):
    iid = await make_running_instance(env)
    wstate = WatcherState(failures={iid: 5})
    settings = make_settings(restart_cooldown=600)
    async with env.db.session() as s:
        await repo.add_event(s, "auto_restart", instance_id=iid, message="上次自动重启")
        inst = await repo.get_instance(s, iid)
        # 刚重启过（now 与事件时间几乎相同）：冷却中。库内读回的是 naive UTC，
        # watcher 会补 UTC tz——所以 now 也用 UTC，与线上一致。
        now = datetime.now(UTC)
        assert await maybe_auto_restart(s, inst, wstate, settings, None, now) is False
        # 超过 10 分钟后：放行
        assert await maybe_auto_restart(s, inst, wstate, settings, None,
                                        now + timedelta(seconds=700)) is True
    assert await job_count(env) == 1


async def test_ssh_error_does_not_count_as_failure(env):
    iid = await make_running_instance(env)
    wstate = WatcherState()

    async def boom(container):
        raise TimeoutError("ssh 连不上")

    env.runtime.inspect = boom
    for _ in range(5):
        await scan(env, wstate)
    assert wstate.failures.get(iid, 0) == 0
    assert await job_count(env) == 0


async def test_docker_auto_restart_recaptures_token(env):
    """宿主机 docker 策略把容器自拉起（restart_count 增长）后，
    watcher 必须重抓 token 落库——详情页 access 永远是当前最新密钥。"""
    iid = await make_running_instance(env)
    wstate = WatcherState()
    await scan(env, wstate)  # 先建基线快照 restart_count=0
    old_token = env.runtime.token
    # docker 自己重启了容器：restart_count+1，token 已换新
    env.runtime.restart_count = 1
    env.runtime.advance_token()
    await scan(env, wstate)
    async with env.db.session() as s:
        tok = await repo.latest_token(s, iid)
        assert tok.token == env.runtime.token != old_token
        assert tok.source == "logs"
        from dsh_control.db.models import Event
        evs = (await s.scalars(select(Event).where(
            Event.kind == "docker_auto_restarted"))).all()
        assert len(evs) == 1


async def test_released_and_deploying_are_skipped(env):
    iid = await make_running_instance(env)
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        await repo.set_instance_state(s, inst, "released")
    wstate = WatcherState()
    env.runtime.health = "unhealthy"
    await scan(env, wstate)
    assert wstate.failures == {}
    assert await job_count(env) == 0


async def test_absent_container_never_auto_restarts(env):
    """开通失败被回滚后容器根本不存在（真实开通就是这么翻车的）：watcher 不许
    去 docker restart——它只会回 "No such container"，还把 last_error 刷成和真实
    故障无关的一句，把人往错方向带。"""
    iid = await make_running_instance(env)
    env.runtime.exists = False
    before = await job_count(env)
    wstate = WatcherState()
    await scan(env, wstate)
    await scan(env, wstate)

    assert await job_count(env) == before, "容器不存在却排了任务"
    assert wstate.absent_noted == {iid}, "只该提示一次"
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        snap = await repo.latest_snapshot(s, iid)
    assert snap is not None and snap.docker_state == "absent"
    # 状态不瞎改：容器不存在不等于实例坏了，详情里 docker_state=absent 已说清
    assert inst.state == "running"

    # 容器回来了就忘，不影响后续正常收敛
    env.runtime.exists = True
    await scan(env, wstate)
    assert wstate.absent_noted == set()


async def test_error_recovers_when_container_is_actually_healthy(env):
    """上次动作失败留下的 error 不许永远挂在 healthy 的容器上：页面就在说假话。
    （现网 dsh-01：容器 running/healthy、重启 0 次，详情却写着"仍未恢复健康"。）"""
    iid = await make_running_instance(env)
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        inst.state = "error"
        inst.last_error = "[20:wait_healthy] 180s 内容器仍未恢复健康"
        await s.commit()

    wstate = WatcherState()
    await scan(env, wstate)                      # 第一轮：只记账，不改状态
    async with env.db.session() as s:
        assert (await repo.get_instance(s, iid)).state == "error"
    await scan(env, wstate)                      # 第二轮：确认恢复
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.state == "running"
        assert not inst.last_error, "恢复后别把旧错误留在实例行上，事件里才有"
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    rec = next(e for e in evs if e["kind"] == "state_recovered")
    assert "wait_healthy" in str(rec["data"].get("previous_error")), "旧错误要留在事件里"


async def test_error_stays_when_container_really_is_gone(env):
    """容器真不在：error 不许被"恢复"掉——那是还没救活的现场，不是恢复。"""
    iid = await make_running_instance(env)
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        inst.state = "error"
        await s.commit()
    env.runtime.exists = False
    wstate = WatcherState()
    for _ in range(3):
        await scan(env, wstate, now=IN_WINDOW)
    async with env.db.session() as s:
        assert (await repo.get_instance(s, iid)).state == "error"
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    assert not any(e["kind"] == "state_recovered" for e in evs)
