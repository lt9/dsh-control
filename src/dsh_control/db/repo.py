"""仓储层：所有查询集中在这里，api / jobs / watcher 不手写 select 细节。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Credential,
    DeployJob,
    DeployLog,
    DeployStep,
    Event,
    Host,
    Instance,
    InstanceConfig,
    InstanceSnapshot,
    InstanceToken,
    utcnow,
)

# ---------------------------------------------------------------- credentials

async def get_credential_by_name(s: AsyncSession, name: str) -> Credential | None:
    return await s.scalar(select(Credential).where(Credential.name == name))


async def get_credential_by_kind(s: AsyncSession, kind: str) -> Credential | None:
    return await s.scalar(
        select(Credential).where(Credential.kind == kind).order_by(Credential.id)
    )


async def upsert_credential(
    s: AsyncSession, *, kind: str, name: str, secret: dict[str, Any], note: str = ""
) -> Credential:
    # 按 (kind, name) 找——只按 name 找会跨 kind 撞车、静默覆盖别人的密钥
    cred = await s.scalar(
        select(Credential).where(Credential.kind == kind, Credential.name == name)
    )
    if cred is None:
        by_name = await get_credential_by_name(s, name)
        if by_name is not None:
            raise ValueError(f"credentials.name 已被 kind={by_name.kind} 占用: {name}")
        cred = Credential(kind=kind, name=name, secret=secret, note=note)
        s.add(cred)
    else:
        cred.secret = secret
        cred.note = note
    await s.commit()
    return cred


# ---------------------------------------------------------------- hosts

async def list_hosts(s: AsyncSession) -> Sequence[Host]:
    return (await s.scalars(select(Host).order_by(Host.id))).all()


async def get_host(s: AsyncSession, host_id: int) -> Host | None:
    return await s.get(Host, host_id)


# ---------------------------------------------------------------- instances

async def list_instances(
    s: AsyncSession, q: str = "", state: str = ""
) -> Sequence[Instance]:
    stmt = select(Instance).order_by(Instance.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Instance.slug.ilike(like),
                Instance.owner_label.ilike(like),
                Instance.dns_name.ilike(like),
            )
        )
    if state:
        stmt = stmt.where(Instance.state == state)
    return (await s.scalars(stmt)).all()


async def get_instance(s: AsyncSession, instance_id: int) -> Instance | None:
    return await s.get(Instance, instance_id)


async def get_instance_by_slug(s: AsyncSession, slug: str) -> Instance | None:
    return await s.scalar(select(Instance).where(Instance.slug == slug))


async def update_instance_notes(
    s: AsyncSession, inst: Instance, *, owner_label: str | None = None,
    note: str | None = None,
) -> Instance:
    """PATCH 专用：只允许改备注名与备注，不碰域名 / 容器名 / 任何外部对象。"""
    if owner_label is not None:
        inst.owner_label = owner_label
    if note is not None:
        inst.note = note
    await s.commit()
    return inst


async def max_frp_remote_port(s: AsyncSession) -> int:
    """公网端口分配起点用：包含 released 实例——已释放的端口不复用。"""
    return (await s.scalar(select(func.max(Instance.frp_remote_port)))) or 0


async def all_slugs(s: AsyncSession) -> list[str]:
    return list((await s.scalars(select(Instance.slug))).all())


async def set_instance_state(
    s: AsyncSession, inst: Instance, state: str, *, last_error: str | None = None,
    commit: bool = True,
) -> None:
    inst.state = state
    if last_error is not None:
        inst.last_error = last_error
    elif state in ("running", "restarting", "deploying"):
        inst.last_error = None
    await s.flush()
    if commit:
        await s.commit()


# ---------------------------------------------------------------- tokens

async def add_token(
    s: AsyncSession, instance_id: int, token: str, url: str, source: str
) -> InstanceToken:
    row = InstanceToken(instance_id=instance_id, token=token, url=url,
                        source=source, captured_at=utcnow())
    s.add(row)
    await s.commit()
    return row


async def latest_token(s: AsyncSession, instance_id: int) -> InstanceToken | None:
    return await s.scalar(
        select(InstanceToken)
        .where(InstanceToken.instance_id == instance_id)
        .order_by(InstanceToken.captured_at.desc(), InstanceToken.id.desc())
        .limit(1)
    )


async def token_exists(s: AsyncSession, instance_id: int, token: str) -> bool:
    found = await s.scalar(
        select(InstanceToken.id)
        .where(InstanceToken.instance_id == instance_id, InstanceToken.token == token)
        .limit(1)
    )
    return found is not None


# ---------------------------------------------------------------- configs

async def add_config_snapshot(
    s: AsyncSession, instance_id: int, version: str, rendered: dict[str, Any]
) -> InstanceConfig:
    row = InstanceConfig(instance_id=instance_id, version=version, rendered=rendered)
    s.add(row)
    await s.commit()
    return row


async def latest_config(s: AsyncSession, instance_id: int) -> InstanceConfig | None:
    return await s.scalar(
        select(InstanceConfig)
        .where(InstanceConfig.instance_id == instance_id)
        .order_by(InstanceConfig.id.desc())
        .limit(1)
    )


# ---------------------------------------------------------------- snapshots

async def add_snapshot(s: AsyncSession, instance_id: int, **kw: Any) -> InstanceSnapshot:
    row = InstanceSnapshot(instance_id=instance_id, captured_at=utcnow(), **kw)
    s.add(row)
    await s.commit()
    return row


async def latest_snapshot(s: AsyncSession, instance_id: int) -> InstanceSnapshot | None:
    return await s.scalar(
        select(InstanceSnapshot)
        .where(InstanceSnapshot.instance_id == instance_id)
        .order_by(InstanceSnapshot.captured_at.desc(), InstanceSnapshot.id.desc())
        .limit(1)
    )


# ---------------------------------------------------------------- jobs / steps / logs

async def create_job(s: AsyncSession, instance_id: int, kind: str) -> DeployJob:
    job = DeployJob(instance_id=instance_id, kind=kind, state="pending")
    s.add(job)
    await s.commit()
    return job


async def get_job(s: AsyncSession, job_id: int) -> DeployJob | None:
    return await s.get(DeployJob, job_id)


async def list_steps(s: AsyncSession, job_id: int) -> Sequence[DeployStep]:
    return (await s.scalars(
        select(DeployStep).where(DeployStep.job_id == job_id).order_by(DeployStep.seq)
    )).all()


async def add_step(
    s: AsyncSession, job_id: int, seq: int, key: str, title: str
) -> DeployStep:
    step = DeployStep(job_id=job_id, seq=seq, key=key, title=title, state="pending")
    s.add(step)
    await s.commit()
    return step


async def logs_after(
    s: AsyncSession, job_id: int, after: int, limit: int = 500
) -> Sequence[DeployLog]:
    return (await s.scalars(
        select(DeployLog)
        .where(DeployLog.job_id == job_id, DeployLog.id > after)
        .order_by(DeployLog.id)
        .limit(limit)
    )).all()


async def add_log(
    s: AsyncSession, job_id: int, seq: int, level: str, line: str
) -> DeployLog:
    row = DeployLog(job_id=job_id, seq=seq, level=level, line=line, ts=utcnow())
    s.add(row)
    await s.commit()
    return row


async def unfinished_jobs(s: AsyncSession) -> Sequence[DeployJob]:
    return (await s.scalars(
        select(DeployJob).where(DeployJob.state.in_(("pending", "running")))
    )).all()


# ---------------------------------------------------------------- events

async def add_event(
    s: AsyncSession, kind: str, *, instance_id: int | None = None,
    job_id: int | None = None, message: str = "", data: dict[str, Any] | None = None,
) -> Event:
    row = Event(kind=kind, instance_id=instance_id, job_id=job_id, message=message,
                data=data or {})
    s.add(row)
    await s.commit()
    return row


async def list_events(
    s: AsyncSession, instance_id: int | None = None, limit: int = 100
) -> Sequence[Event]:
    stmt = select(Event).order_by(Event.id.desc()).limit(limit)
    if instance_id is not None:
        stmt = stmt.where(Event.instance_id == instance_id)
    return (await s.scalars(stmt)).all()


async def last_event_at(s: AsyncSession, kind: str, instance_id: int) -> datetime | None:
    return await s.scalar(
        select(func.max(Event.created_at)).where(
            Event.kind == kind, Event.instance_id == instance_id
        )
    )


# ---------------------------------------------------------------- overview 聚合
# §2.0 规矩二：不写联表。两张表各查各的，Python 里组装。

async def instance_counts(s: AsyncSession) -> dict[str, Any]:
    rows = await s.execute(
        select(Instance.state, func.count()).group_by(Instance.state)
    )
    by_state = {state: n for state, n in rows.all()}

    hosts = await list_hosts(s)
    host_rows = await s.execute(
        select(Instance.host_id, Instance.state, func.count(Instance.id))
        .group_by(Instance.host_id, Instance.state)
    )
    by_host: dict[int, dict[str, Any]] = {
        h.id: {"host_id": h.id, "host_name": h.name, "total": 0, "running": 0,
               "unhealthy": 0, "restarting": 0, "error": 0, "released": 0, "other": 0}
        for h in hosts
    }
    for host_id, state, n in host_rows.all():
        entry = by_host.setdefault(
            host_id, {"host_id": host_id, "host_name": "", "total": 0, "running": 0,
                      "unhealthy": 0, "restarting": 0, "error": 0, "released": 0,
                      "other": 0}
        )
        entry["total"] += n
        if state in ("running", "unhealthy", "restarting", "error", "released"):
            entry[state] += n
        elif state is not None:
            entry["other"] += n
    total_hosts = await s.scalar(select(func.count()).select_from(Host))
    return {
        "running": by_state.get("running", 0),
        "unhealthy": by_state.get("unhealthy", 0) + by_state.get("restarting", 0),
        "released": by_state.get("released", 0),
        "hosts": total_hosts or 0,
        "by_host": sorted(by_host.values(), key=lambda x: x["host_id"]),
    }
