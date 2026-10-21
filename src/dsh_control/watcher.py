"""watcher：周期采状态、自动重启、抓 token、到期自动终态化（契约 §3/§6.4）。

自动重启的三个抑制条件（缺一不可）：
1. 连续健康检查失败 < 3 次不重启（阈值 DSHC_UNHEALTHY_THRESHOLD，默认 3）；
2. 备份窗口内不重启（默认 03:20–04:00，DSHC_BACKUP_WINDOW，Asia/Shanghai）；
3. 距该实例上次自动重启 < 10 分钟不重启（DSHC_RESTART_COOLDOWN）。

备份会让容器健康闪一次 unhealthy——这是已知现象：单次失败远够不到「连续 3 次」，
恢复 healthy 立即清零计数，再叠加窗口与冷却，绝不会因此进重启循环。

宿主机 docker 策略自动拉起容器（restart_count 增长）后，这里负责重新抓
最新 token，保证详情页 access 永远是当前最新密钥；抓到的值与库里最新一条
相同则什么都不写、不记事件（§3 去重——「事件流被获取密码刷满」就是这么修的）。

released 实例不采集、不重启；保留期满（purge_after_days，每轮现读库值，
site.toml 改了立即生效）自动入队 purge 工单做终态化（§6.4）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .db import repo
from .db.models import Credential, Host, Instance
from .domain.timeutil import in_window
from .integrations.base import ContainerStats, redact
from .jobs.capture import TokenNotFound, capture_token
from .jobs.runner import HostEndpoint, make_runtime, redact_text
from .settings import Settings, get_settings

log = logging.getLogger("dsh_control.watcher")

# 这些状态下 watcher 不采集、不判定：pending/deploying 还没容器，
# restarting 归 restart job 管，released 是已停放的停放态（契约 §3：不采集不
# 重启；保留期到期由 _queue_due_purges 入队 purge，purged 没有容器可采）。
SKIP_STATES = {"pending", "deploying", "restarting", "released"}


@dataclass
class WatcherState:
    """进程内的连续失败计数。进程重启即清零——方向是安全的（宁可少重启）。"""

    failures: dict[int, int] = field(default_factory=dict)
    # error → running 的确认计数：连续两轮看到 running+healthy 才恢复。
    # 一轮就改状态会被"重启那一瞬间的假 healthy"糊弄。
    recovered_votes: dict[int, int] = field(default_factory=dict)
    last_restart_count: dict[int, int] = field(default_factory=dict)
    # 已经提示过"容器不存在"的实例：这条只在状态跳变时记一次，别每轮刷屏
    absent_noted: set[int] = field(default_factory=set)


def _is_failure(snap: Any) -> bool:
    if snap is None:
        return True  # 容器不存在
    if snap.state != "running":
        return True
    return snap.health == "unhealthy"


def _is_healthy(snap: Any) -> bool:
    return snap is not None and snap.state == "running" and snap.health in ("healthy", "none")


async def _ssh_key_pem(s: AsyncSession, host: Host) -> str:
    if not host.ssh_credential_id:
        return ""
    cred = await s.get(Credential, host.ssh_credential_id)
    if cred is None:
        return ""
    return (cred.secret or {}).get("private_key_pem", "")


async def _last_auto_restart_at(s: AsyncSession, instance_id: int) -> datetime | None:
    return await repo.last_event_at(s, "auto_restart", instance_id)


async def maybe_auto_restart(
    s: AsyncSession, inst: Instance, wstate: WatcherState, settings: Settings,
    runner: Any, now: datetime,
) -> bool:
    """三个抑制条件逐条核对，全过才触发。返回是否触发。"""
    failures = wstate.failures.get(inst.id, 0)
    if failures < settings.unhealthy_threshold:
        return False
    window = settings.backup_window
    if in_window(window, now):
        log.info("实例 %s 连续失败 %s 次，但处于备份窗口 %s，跳过自动重启",
                 inst.slug, failures, window)
        return False
    cooldown = timedelta(seconds=settings.restart_cooldown)
    last = await _last_auto_restart_at(s, inst.id)
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now - last < cooldown:
            log.info("实例 %s 距上次自动重启不足 %ss，冷却中", inst.slug, settings.restart_cooldown)
            return False
    # 触发
    job = await repo.create_job(s, inst.id, "restart")
    await repo.add_event(
        s, "auto_restart", instance_id=inst.id, job_id=job.id,
        message=f"连续 {failures} 次健康检查失败，自动重启（阈值 {settings.unhealthy_threshold}）",
        data=redact({"failures": failures, "threshold": settings.unhealthy_threshold}),
    )
    await repo.set_instance_state(s, inst, "restarting")
    wstate.failures[inst.id] = 0
    if runner is not None:
        runner.spawn(job.id)
    return True


async def _queue_due_purges(s: AsyncSession, released_rows: list[Instance],
                            settings: Settings, runner: Any, now: datetime) -> None:
    """保留期已过的 released 实例入队 purge 工单（契约 §3/§6.4 自动触发）。

    每轮现读库里的保留天数（site 行改了立即生效，不吃启动缓存——只读这一个键，
    不在循环里重建 settings）；`released_at + 天数 < now` 且该实例没有未完工单
    才入队；runner.spawn 与自动重启同款。released 实例照常不采集、不重启
    （SKIP_STATES 不变，§3）。"""
    if not released_rows:
        return
    days = await repo.current_purge_after_days(s, settings.purge_after_days)
    busy = {j.instance_id for j in await repo.unfinished_jobs(s)}
    for inst in released_rows:
        if inst.id in busy:
            continue
        released_at = inst.released_at
        if released_at is None:
            continue    # 没有释放时间的历史行：无法判断期满，宁可不删
        if released_at.tzinfo is None:
            released_at = released_at.replace(tzinfo=UTC)
        if now - released_at <= timedelta(days=days):
            continue
        job = await repo.create_job(s, inst.id, "purge",
                                    {"source": "auto", "purge_after_days": days})
        await repo.add_event(
            s, "purge_queued", instance_id=inst.id, job_id=job.id,
            message=f"released 保留期已过（现读 {days} 天），自动入队彻底释放",
            data=redact({"source": "auto", "purge_after_days": days,
                         "released_at": released_at.isoformat()}),
        )
        log.info("实例 %s 保留期已过，入队 purge（job %s）", inst.slug, job.id)
        if runner is not None:
            runner.spawn(job.id)


async def scan_once(
    db: Any, providers: Any, settings: Settings | None, runner: Any,
    wstate: WatcherState, now: datetime | None = None,
) -> None:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    async with db.session() as s:
        instances = await repo.list_instances(s)
        due_released: list[Instance] = []
        for inst in instances:
            if inst.state in SKIP_STATES or inst.desired_state == "released":
                if inst.state == "released":
                    due_released.append(inst)   # 保留期判定在循环后统一做
                continue
            host = await repo.get_host(s, inst.host_id)
            if host is None or not host.enabled:
                continue
            pem = await _ssh_key_pem(s, host)
            try:
                runtime = make_runtime(providers, HostEndpoint(
                    host.ssh_host, host.ssh_port, host.ssh_user, pem))
                snap = await runtime.inspect(inst.container_name or inst.slug)
            except Exception as exc:  # noqa: BLE001 —— SSH 不可达等：本轮跳过，不计失败
                log.warning("采集 %s 失败（不计入健康失败）: %s", inst.slug,
                            redact_text(str(exc)))
                continue

            # CPU/内存只能问 `docker stats`（inspect 里没有这两个值）。
            # 单独 try：采样失败绝不能拖累健康判定那条链——那是自动重启的依据。
            stats = ContainerStats()
            if snap and snap.state == "running":
                try:
                    stats = await runtime.stats(inst.container_name or inst.slug)
                except Exception as exc:  # noqa: BLE001
                    log.info("采样 %s 资源占用失败（不影响健康判定）: %s",
                             inst.slug, redact_text(str(exc)))

            prev_snap = await repo.latest_snapshot(s, inst.id)
            await repo.add_snapshot(
                s, inst.id, docker_state=snap.state if snap else "absent",
                health=snap.health if snap else "none",
                restart_count=snap.restart_count if snap else 0,
                cpu_pct=stats.cpu_pct,
                mem_usage_bytes=stats.mem_usage_bytes,
                mem_limit_bytes=stats.mem_limit_bytes,
                raw=redact({"image_id": snap.image_id if snap else "",
                            "ip": snap.ip if snap else ""}),
            )
            if snap and snap.image and snap.image != inst.image_tag:
                inst.image_tag = snap.image
                await s.commit()

            # 容器不存在：要么开通从未走完（第 60 步之前失败并被回滚，实测就是这样），
            # 要么被人 docker rm 了。这两种情况下 `docker restart` 只会回
            # "No such container"，还会把 last_error 刷成和真实故障无关的一句，
            # 把人往错方向带。所以这里不计健康失败、不自动重启，只提示一次。
            if snap is None:
                if inst.id not in wstate.absent_noted:
                    wstate.absent_noted.add(inst.id)
                    log.info("实例 %s 在 %s 上查不到容器 %s：不做自动重启，"
                             "需要重新开通（续建）或释放", inst.slug, host.name,
                             inst.container_name or inst.slug)
                continue
            wstate.absent_noted.discard(inst.id)

            # docker 策略自动拉起：restart_count 比上一次快照增长 → 重新抓 token
            # （契约硬承诺：任何重启后详情里必须是当前最新密钥）
            prev_rc = prev_snap.restart_count if prev_snap else None
            cur_rc = snap.restart_count if snap else 0
            wstate.last_restart_count[inst.id] = cur_rc
            if prev_rc is not None and cur_rc > prev_rc and _is_healthy(snap):
                await _recapture_after_auto_restart(s, runtime, inst)

            # 首次部署完但一条 token 都没有（如部署时 step70 失败后手工救活）
            if _is_healthy(snap) and await repo.latest_token(s, inst.id) is None:
                await _recapture_after_auto_restart(
                    s, runtime, inst, kind="token_backfill", message="补齐登录密钥")

            # 健康判定与连续失败计数
            if _is_healthy(snap):
                if wstate.failures.get(inst.id):
                    wstate.failures[inst.id] = 0
                if inst.state == "unhealthy":
                    await repo.set_instance_state(s, inst, "running")
                elif inst.state == "error":
                    # error 是"上一次动作失败"留下的痕迹，不是容器现在的样子。
                    # 容器明明 running+healthy 却一直挂着 error，页面就在说假话
                    # （现网 dsh-01：容器 healthy，详情却写着"仍未恢复健康"）。
                    # 连续两轮确认才恢复，并把恢复写进事件流，旧错误留在事件里。
                    votes = wstate.recovered_votes.get(inst.id, 0) + 1
                    if votes >= 2:
                        wstate.recovered_votes.pop(inst.id, None)
                        prev_err = (inst.last_error or "")[:400]
                        inst.last_error = ""
                        await repo.set_instance_state(s, inst, "running")
                        await repo.add_event(
                            s, "state_recovered", instance_id=inst.id,
                            message="容器连续两轮 running/healthy，状态从 error 恢复为"
                                    f" running。上次失败记录：{prev_err[:160] or '（无）'}",
                            data={"previous_error": prev_err})
                    else:
                        wstate.recovered_votes[inst.id] = votes
                continue

            if snap is not None and snap.state == "running" and snap.health == "starting":
                continue  # 启动中：既不算失败也不清零

            wstate.failures[inst.id] = wstate.failures.get(inst.id, 0) + 1
            if inst.state == "running":
                await repo.set_instance_state(s, inst, "unhealthy")
            await maybe_auto_restart(s, inst, wstate, settings, runner, now)

        # released 不在采集循环里，但保留期到了要自动终态化（§3/§6.4）
        await _queue_due_purges(s, due_released, settings, runner, now)


async def _recapture_after_auto_restart(
    s: AsyncSession, runtime: Any, inst: Instance,
    kind: str = "docker_auto_restarted", message: str = "检测到容器被自动重启，重抓登录密钥",
) -> None:
    try:
        res = await capture_token(s, runtime, inst, "logs")
    except TokenNotFound as exc:
        await repo.add_event(s, "token_recapture_failed", instance_id=inst.id,
                             message=redact_text(str(exc)), data={})
        return
    if not res.changed:
        # §3 去重：值与库里最新一条相同 → 什么都不写、不记事件（自动路径静默，
        # 这修掉了「事件流被获取密码刷满」）。
        return
    try:
        inst.dsh_version = await runtime.version(inst.container_name or inst.slug)
        await s.commit()
    except Exception:  # noqa: BLE001, S110 —— 版本号取不到不值得惊动
        pass
    await repo.add_event(
        s, kind, instance_id=inst.id, message=redact_text(message),
        data=redact({"source": "logs", "url": res.url, "token": res.token}),
    )


async def run_loop(db: Any, providers: Any, settings: Settings | None,
                   runner: Any, stop_event: asyncio.Event | None = None) -> None:
    settings = settings or get_settings()
    wstate = WatcherState()
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await scan_once(db, providers, settings, runner, wstate)
        except Exception as exc:  # noqa: BLE001 —— 循环不能被任何单轮异常打死
            # 不带 traceback：原始异常参数可能含密钥形态文本，只记打码后的一行
            log.warning("watcher 轮次异常: %s", redact_text(str(exc)))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_interval)
        except TimeoutError:
            pass
