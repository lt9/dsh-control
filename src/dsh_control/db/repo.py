"""仓储层：所有查询集中在这里，api / jobs / watcher 不手写 select 细节。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Credential,
    DeployJob,
    DeployLog,
    DeployStep,
    Event,
    FontlibRelease,
    Host,
    ImageArtifact,
    ImageChannel,
    Instance,
    InstanceConfig,
    InstanceSnapshot,
    InstanceToken,
    KitRelease,
    utcnow,
)

# ---------------------------------------------------------------- credentials

async def get_credential_by_name(s: AsyncSession, name: str) -> Credential | None:
    return await s.scalar(select(Credential).where(Credential.name == name))


async def get_credential_by_kind(s: AsyncSession, kind: str) -> Credential | None:
    return await s.scalar(
        select(Credential).where(Credential.kind == kind).order_by(Credential.id)
    )


async def get_site_value(s: AsyncSession, key: str) -> Any:
    """现读 credentials(kind=site) 里的单个键（seed --site 灌的 [site] 段镜像）。

    只给 watcher / 序列化这类「每轮/每次现读最新库值」的口径用（§3：改了 DB 的
    purge_after_days 不需重启就生效）；整表覆盖仍走 main.load_site_into_settings，
    不在循环里重建 settings。"""
    cred = await get_credential_by_kind(s, "site")
    return (cred.secret or {}).get(key) if cred else None


async def current_purge_after_days(s: AsyncSession, fallback: int) -> int:
    """released 保留期的当前值：库里的 site 行优先，读不到或读坏回落 env 口径。"""
    raw = await get_site_value(s, "purge_after_days")
    try:
        value = int(raw)   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


SHIP_MODES = ("ssh", "download")
MIRROR_ORDER_DEFAULT = ["gitee", "github", "console"]   # 国内优先；探测不通自然滑向下一条（§6.1.6）
MIRROR_CHANNELS = ("github", "gitee")                   # console 不算渠道，恒兜底


def _clean_mirror_order(raw: object) -> list[str]:
    """mirror_order 的容错读法：只认 gitee/github/console，去重保序；读坏回落默认。"""
    if not isinstance(raw, (list, tuple)):
        return list(MIRROR_ORDER_DEFAULT)
    seen: list[str] = []
    for x in raw:
        if (isinstance(x, str) and x in ("gitee", "github", "console")
                and x not in seen):
            seen.append(x)
    return seen or list(MIRROR_ORDER_DEFAULT)


async def current_image_ship(s: AsyncSession, *, default_base: str = "") -> dict[str, Any]:
    """镜像分发设置的当前值（§6.1.5/§6.1.6），与 purge 保留期同一个活读模式。

    返回生效值：{"mode", "base_url", "mirror_order", "auto_mirror"}。site 凭据
    secret 的 image_ship 优先；读不到或读坏逐键回落默认（ssh / 站点 public_base /
    gitee→github→console / True）。base_url 空串=吃 site 的 public_base。
    """
    raw = await get_site_value(s, "image_ship")
    d = raw if isinstance(raw, dict) else {}
    mode = d.get("mode") if d.get("mode") in SHIP_MODES else "ssh"
    base = str(d.get("base_url") or "").strip().rstrip("/")
    if not base:
        base = str(await get_site_value(s, "public_base") or default_base or "").strip()
        base = base.rstrip("/")
    auto_mirror = d.get("auto_mirror")
    auto_mirror = True if not isinstance(auto_mirror, bool) else auto_mirror
    return {"mode": mode, "base_url": base,
            "mirror_order": _clean_mirror_order(d.get("mirror_order")),
            "auto_mirror": auto_mirror}


async def mirror_repos(s: AsyncSession) -> dict[str, str]:
    """各渠道配置的仓库名（site secret 的 image_ship.mirrors.{ch}.repo）。"""
    raw = await get_site_value(s, "image_ship")
    mirrors = raw.get("mirrors") if isinstance(raw, dict) else None
    out: dict[str, str] = {}
    for ch in MIRROR_CHANNELS:
        cfg = mirrors.get(ch) if isinstance(mirrors, dict) else None
        out[ch] = str(cfg.get("repo") or "") if isinstance(cfg, dict) else ""
    return out


async def mirror_token(s: AsyncSession, channel: str) -> str:
    """渠道令牌明文（kind=mirror_token, name=channel）；没配返回空串。

    只给服务端内部用（publish 推送）；任何 API 出口只回 token_set，明文永不出网
    （§6.1.6）。
    """
    cred = await s.scalar(select(Credential).where(
        Credential.kind == "mirror_token", Credential.name == channel))
    return str((cred.secret or {}).get("token") or "") if cred else ""


# ---------------------------------------------------------------- image_channels

async def get_channel(s: AsyncSession, artifact_id: int,
                      channel: str) -> ImageChannel | None:
    return await s.scalar(select(ImageChannel).where(
        ImageChannel.artifact_id == artifact_id, ImageChannel.channel == channel))


async def upsert_channel(s: AsyncSession, *, artifact_id: int, channel: str,
                         state: str, url: str = "", release_tag: str = "",
                         error: str = "") -> ImageChannel:
    row = await get_channel(s, artifact_id, channel)
    if row is None:
        row = ImageChannel(artifact_id=artifact_id, channel=channel, state=state,
                           url=url, release_tag=release_tag, error=error)
        s.add(row)
    else:
        row.state = state
        row.url = url
        row.release_tag = release_tag
        row.error = error
        row.updated_at = utcnow()
    await s.commit()
    return row


async def demote_channels(s: AsyncSession, artifact_id: int, note: str) -> int:
    """制品被同 tag 重发顶掉后，把它的渠道行全打回 pending：外部那份副本已是旧货，
    不许再进第 20 步候选，等 30/40 重推才恢复 ok。返回打回的行数。"""
    rows = (await s.scalars(select(ImageChannel).where(
        ImageChannel.artifact_id == artifact_id, ImageChannel.state == "ok"))).all()
    for row in rows:
        row.state = "pending"
        row.url = ""
        row.error = note
        row.updated_at = utcnow()
    await s.commit()
    return len(rows)


async def delete_channels(s: AsyncSession, artifact_id: int) -> None:
    """制品下架时连坐删渠道登记行（外部 release 留着无妨，公开仓库，§6.1.6）。"""
    for row in (await s.scalars(select(ImageChannel).where(
            ImageChannel.artifact_id == artifact_id))).all():
        await s.delete(row)
    await s.commit()


async def other_ok_rows(s: AsyncSession, channel: str,
                        exclude_row_id: int) -> Sequence[ImageChannel]:
    """该渠道其它制品还挂着的 ok 行（gitee 仓库只留最新一版，发布成功后删旧用）。"""
    return (await s.scalars(
        select(ImageChannel)
        .where(ImageChannel.channel == channel, ImageChannel.state == "ok",
               ImageChannel.id != exclude_row_id)
        .order_by(ImageChannel.updated_at)
    )).all()


# ---------------------------------------------------------------- image_artifacts

async def get_artifact(s: AsyncSession, artifact_id: int) -> ImageArtifact | None:
    return await s.get(ImageArtifact, artifact_id)


async def get_artifact_by_tag(s: AsyncSession, tag: str) -> ImageArtifact | None:
    return await s.scalar(select(ImageArtifact).where(ImageArtifact.tag == tag))


async def get_artifact_by_token(s: AsyncSession, token: str) -> ImageArtifact | None:
    if not token:
        return None
    return await s.scalar(select(ImageArtifact).where(ImageArtifact.token == token))


async def list_artifacts(s: AsyncSession) -> Sequence[ImageArtifact]:
    return (await s.scalars(
        select(ImageArtifact).order_by(ImageArtifact.created_at.desc(),
                                       ImageArtifact.id.desc())
    )).all()


async def add_artifact(s: AsyncSession, *, tag: str, filename: str, size_bytes: int,
                       sha256: str, token: str, origin_host: str) -> ImageArtifact:
    art = ImageArtifact(tag=tag, filename=filename, size_bytes=size_bytes,
                        sha256=sha256, token=token, origin_host=origin_host)
    s.add(art)
    await s.commit()
    return art


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


# ---------------------------------------------------------------- kit / 字体库制品（资产分发）

async def current_kit_release(s: AsyncSession) -> KitRelease | None:
    return await s.scalar(select(KitRelease).where(KitRelease.current.is_(True))
                          .order_by(KitRelease.id.desc()).limit(1))


async def list_kit_releases(s: AsyncSession) -> Sequence[KitRelease]:
    return (await s.scalars(select(KitRelease).order_by(KitRelease.id.desc()))).all()


async def add_kit_release(s: AsyncSession, *, version: str, files: dict[str, Any],
                          size_bytes: int, file_count: int,
                          note: str = "") -> KitRelease:
    if await s.scalar(select(KitRelease).where(KitRelease.version == version)):
        raise ValueError(f"kit 版本已存在: {version}")
    row = KitRelease(version=version, files=files, size_bytes=size_bytes,
                     file_count=file_count, note=note, current=True)
    s.add(row)
    await s.flush()
    # 换行同事务：新行落定才把旧的 demote，中途崩了两边都有 current，重启幂等
    await s.execute(update(KitRelease)
                    .where(KitRelease.id != row.id)
                    .values(current=False))
    await s.commit()
    return row


async def current_fontlib_release(s: AsyncSession) -> FontlibRelease | None:
    return await s.scalar(select(FontlibRelease).where(FontlibRelease.current.is_(True))
                          .order_by(FontlibRelease.id.desc()).limit(1))


async def list_fontlib_releases(s: AsyncSession) -> Sequence[FontlibRelease]:
    return (await s.scalars(select(FontlibRelease).order_by(
        FontlibRelease.id.desc()))).all()


async def add_fontlib_release(s: AsyncSession, *, version: str, filename: str,
                              size_bytes: int, sha256: str, file_count: int,
                              origin: str = "", note: str = "") -> FontlibRelease:
    if await s.scalar(select(FontlibRelease).where(
            FontlibRelease.version == version)):
        raise ValueError(f"字体库版本已存在: {version}")
    row = FontlibRelease(version=version, filename=filename, size_bytes=size_bytes,
                         sha256=sha256, file_count=file_count, origin=origin,
                         note=note, current=True)
    s.add(row)
    await s.flush()
    await s.execute(update(FontlibRelease)
                    .where(FontlibRelease.id != row.id)
                    .values(current=False))
    await s.commit()
    return row


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


async def host_in_use_ports(s: AsyncSession, host_id: int, *,
                            exclude_instance_id: int | None = None) -> set[int]:
    """该机已登记在用的公网端口（frp_remote_port 单表查询，含 released——端口不复用）。

    direct 主机挑端口用（§6.1.4）：占用探测在本机做，库里同机实例占着的端口是
    另一半事实；purged 实例的登记已清空，天然不在内。"""
    q = select(Instance.frp_remote_port).where(
        Instance.host_id == host_id, Instance.frp_remote_port.is_not(None))
    if exclude_instance_id is not None:
        q = q.where(Instance.id != exclude_instance_id)
    return {int(p) for p in (await s.scalars(q)).all() if p}


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
    """同 (instance_id, version) 只留一行：已存在就地更新 ``rendered`` 并返回。

    重试开通会从第 10 步重跑，裸 INSERT 会撞唯一键
    ``instance_configs_instance_id_version_key``（2026-10-21 dsh-01 续建实测崩在这）。
    配置文档按版本号定稿，同版本重算出来的内容就是要替换旧内容。
    """
    existing = await s.scalar(
        select(InstanceConfig)
        .where(InstanceConfig.instance_id == instance_id,
               InstanceConfig.version == version)
        .order_by(InstanceConfig.id)
    )
    if existing is not None:
        existing.rendered = rendered
        await s.commit()
        return existing
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

async def create_job(s: AsyncSession, instance_id: int, kind: str,
                     params: dict[str, Any] | None = None) -> DeployJob:
    job = DeployJob(instance_id=instance_id, kind=kind, state="pending",
                    params=params or {})
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


async def delete_artifact(s: AsyncSession, art: ImageArtifact) -> None:
    """下架：删行。文件删除由调用方在提交后尽力收尾——行没了链接就死了。"""
    await s.delete(art)
    await s.commit()
