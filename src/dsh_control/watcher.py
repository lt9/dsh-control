"""watcher：周期采状态、自动重启、抓 token（契约 §3）。

自动重启的三个抑制条件（缺一不可）：
1. 连续健康检查失败 < 3 次不重启（阈值 DSHC_UNHEALTHY_THRESHOLD，默认 3）；
2. 备份窗口内不重启（默认 03:20–04:00，DSHC_BACKUP_WINDOW，Asia/Shanghai）；
3. 距该实例上次自动重启 < 10 分钟不重启（DSHC_RESTART_COOLDOWN）。

备份会让容器健康闪一次 unhealthy——这是已知现象：单次失败远够不到「连续 3 次」，
恢复 healthy 立即清零计数，再叠加窗口与冷却，绝不会因此进重启循环。

宿主机 docker 策略自动拉起容器（restart_count 增长）后，这里负责重新抓
最新 token 落库，保证详情页 access 永远是当前最新密钥。
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
from .integrations.base import redact
from .jobs.capture import TokenNotFound, capture_token
from .jobs.runner import HostEndpoint, make_runtime, redact_text
from .settings import Settings, get_settings

log = logging.getLogger("dsh_control.watcher")

# 这些状态下 watcher 不采集、不判定：pending/deploying 还没容器，
# restarting 归 restart job 管，released 是终态（契约 §3）。
SKIP_STATES = {"pending", "deploying", "restarting", "released"}


@dataclass
class WatcherState:
    """进程内的连续失败计数。进程重启即清零——方向是安全的（宁可少重启）。"""

    failures: dict[int, int] = field(default_factory=dict)
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


async def scan_once(
    db: Any, providers: Any, settings: Settings | None, runner: Any,
    wstate: WatcherState, now: datetime | None = None,
) -> None:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    async with db.session() as s:
        instances = await repo.list_instances(s)
        for inst in instances:
            if inst.state in SKIP_STATES or inst.desired_state == "released":
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

            prev_snap = await repo.latest_snapshot(s, inst.id)
            await repo.add_snapshot(
                s, inst.id, docker_state=snap.state if snap else "absent",
                health=snap.health if snap else "none",
                restart_count=snap.restart_count if snap else 0,
                cpu_pct=snap.cpu_pct if snap else None,
                mem_usage_bytes=snap.mem_usage_bytes if snap else None,
                mem_limit_bytes=snap.mem_limit_bytes if snap else None,
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
                continue

            if snap is not None and snap.state == "running" and snap.health == "starting":
                continue  # 启动中：既不算失败也不清零

            wstate.failures[inst.id] = wstate.failures.get(inst.id, 0) + 1
            if inst.state == "running":
                await repo.set_instance_state(s, inst, "unhealthy")
            await maybe_auto_restart(s, inst, wstate, settings, runner, now)


async def _recapture_after_auto_restart(
    s: AsyncSession, runtime: Any, inst: Instance,
    kind: str = "docker_auto_restarted", message: str = "检测到容器被自动重启，重抓登录密钥",
) -> None:
    try:
        tok = await capture_token(s, runtime, inst, "logs")
    except TokenNotFound as exc:
        await repo.add_event(s, "token_recapture_failed", instance_id=inst.id,
                             message=redact_text(str(exc)), data={})
        return
    try:
        inst.dsh_version = await runtime.version(inst.container_name or inst.slug)
        await s.commit()
    except Exception:  # noqa: BLE001, S110 —— 版本号取不到不值得惊动
        pass
    await repo.add_event(
        s, kind, instance_id=inst.id, message=redact_text(message),
        data=redact({"source": "logs", "url": tok.url, "token": tok.token}),
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
