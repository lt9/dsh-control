"""响应序列化：详情响应形状严格按契约 §4 的 JSON 示例。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repo
from ..db.models import Host, Instance
from ..domain import config_docs
from ..domain.slug import token_mask
from ..domain.timeutil import iso

# 完整 token 只允许出现在 reveal 动作 GET /instances/{id}/token（契约 §0.4）；
# 详情只给 mask，列表连 mask 都不给。
_RELEASABLE_STATES = {"deploying", "running", "unhealthy", "restarting", "error"}
_RESTARTABLE_STATES = {"running", "unhealthy", "restarting", "error"}
# 重跑开通：只有从未开通成功过的状态才给这个入口（开通失败后必须有路可走）。
# 页面按钮口径不变——released 恢复与 running 升级走下面的 restore / upgrade
# 新字段（§4 actions），前端照新字段渲染，不靠往 deploy 里塞状态。
_DEPLOYABLE_STATES = {"pending", "error"}
# 升级/重建按钮（§9）：running/unhealthy 且期望不是 released
_UPGRADABLE_STATES = {"running", "unhealthy"}


def purge_after(inst: Instance, purge_after_days: int) -> str | None:
    """自动彻底释放的日期（= released_at + 当前保留期，天数每次用库值现算，§4）。

    仅 state=released 给出，其余状态（含 purged）一律 null——purged 已经删完，
    再给一个未来日期会把「已彻底释放」渲染成「还要再删」。"""
    if inst.state == "released" and inst.released_at is not None:
        released_at: datetime = inst.released_at
        if released_at.tzinfo is None:
            released_at = released_at.replace(tzinfo=UTC)
        return iso(released_at + timedelta(days=purge_after_days))
    return None


def _actions(inst: Instance) -> dict[str, bool]:
    """§4 actions：restore=可恢复（released 未 purge）；purge=可彻底释放；
    upgrade=running/unhealthy 可用「升级/重建」按钮走 deploy。
    purged 全 false——记录只读，任何写操作拒绝（§3）。"""
    return {
        "restart": inst.state in _RESTARTABLE_STATES,
        "release": inst.state in _RELEASABLE_STATES,
        # 开通失败/从未开通才有"重跑开通"；running 不给（那该走 restart/upgrade）
        "deploy": (inst.state in _DEPLOYABLE_STATES
                   and inst.desired_state != "released"),
        "restore": inst.state == "released",
        "purge": inst.state == "released",
        "upgrade": (inst.state in _UPGRADABLE_STATES
                    and inst.desired_state != "released"),
    }


def list_item(inst: Instance, host: Host | None, *,
              purge_after_days: int = 15) -> dict[str, Any]:
    """列表项——§9 交互做减法：保持精简（用途与 slug、状态、DSH 版本、配置版本、
    所在主机、域名、更新时间）；token 与内部细节绝不出现在这里。
    released 行要显示「保留期还剩 N 天」，所以带 purge_after；purged 要标
    「已彻底释放」的时间口径，所以带 purged_at。"""
    return {
        "id": inst.id,
        "slug": inst.slug,
        "owner_label": inst.owner_label,
        "note": inst.note,
        "dns_name": inst.dns_name,
        "state": inst.state,
        "desired_state": inst.desired_state,
        "dsh_version": inst.dsh_version,
        "config_version": inst.config_version,
        "host": {"id": host.id, "name": host.name} if host else None,
        "released_at": iso(inst.released_at),
        "purged_at": iso(inst.purged_at),
        "purge_after": purge_after(inst, purge_after_days),
        "updated_at": iso(inst.updated_at),
    }


async def instance_detail(
    s: AsyncSession, inst: Instance, host: Host | None, *,
    purge_after_days: int = 15,
) -> dict[str, Any]:
    token = await repo.latest_token(s, inst.id)
    cfg = await repo.latest_config(s, inst.id)
    snap = await repo.latest_snapshot(s, inst.id)

    rendered = (cfg.rendered if cfg else {}) or {}
    # 一键地址平时只给打码版：库里存的是含 token 的完整地址，详情页默认不外送，
    # 点"再取一次"（GET /token，带 X-Requested-With）才给明文。缺了这行时
    # 页面只能显示"还没抓到过"，其实密钥就在库里。
    masked_url = ""
    if token and token.url:
        masked_url = token.url.replace(token.token, token_mask(token.token))
    access: dict[str, Any] = {
        "has_token": token is not None,
        "token_masked": token_mask(token.token) if token else "",
        "url_masked": masked_url,
        "captured_at": iso(token.captured_at) if token else None,
    }

    image_id = ""
    if snap and snap.raw:
        image_id = str(snap.raw.get("image_id", ""))[:12]

    return {
        "id": inst.id,
        "slug": inst.slug,
        "owner_label": inst.owner_label,
        "note": inst.note,
        "host": {"id": host.id, "name": host.name} if host else None,
        "state": inst.state,
        "desired_state": inst.desired_state,
        "dsh_version": inst.dsh_version,
        "image_tag": inst.image_tag,
        "image_id": image_id,
        "config_version": inst.config_version or config_docs.CONFIG_VERSION,
        "config": {"items": config_docs.config_items(rendered)},
        # DSH 自己那套（模型/压缩/插件/技能/预设）。老快照没有这一组 → null，
        # 前端据此显示「重新读取」，而不是让她以为"什么都没配"
        "dsh_config": (rendered or {}).get("DSH"),
        "network": {
            "dns_name": inst.dns_name,
            "public_url": inst.public_url,
            # 回源方式来自主机（§6.1.4）：direct 的 frp_proxy_id 为 null 是常态
            # （不建隧道），前端拿这个字段别再当异常提示
            "upstream_mode": host.upstream_mode if host else None,
            "frp_remote_port": inst.frp_remote_port,
            "frp_proxy_id": inst.frp_proxy_id,
            "dns_record_id": inst.dns_record_id,
            "container_ip": (snap.raw or {}).get("ip", "") if snap else "",
        },
        "runtime": {
            "docker_state": snap.docker_state if snap else "",
            "health": snap.health if snap else "none",
            "restart_count": snap.restart_count if snap else 0,
            "cpu_pct": snap.cpu_pct if snap else None,
            "mem_usage_bytes": snap.mem_usage_bytes if snap else None,
            "mem_limit_bytes": snap.mem_limit_bytes if snap else None,
            "captured_at": iso(snap.captured_at) if snap else None,
        },
        "access": access,
        "last_error": inst.last_error,
        "released_at": iso(inst.released_at),
        "purged_at": iso(inst.purged_at),
        "purge_after": purge_after(inst, purge_after_days),
        "actions": _actions(inst),
    }


def job_payload(job, steps, instance=None) -> dict[str, Any]:
    """工单详情。instance 由路由单独查一次传进来（本项目不写 JOIN）——
    没有这三个字段，手敲 URL 打开的工单页就不知道那台实例已经回收，
    还会把死掉的一键地址当成能用的东西摆着。"""
    return {
        "id": job.id,
        "kind": job.kind,
        # 来路（§4）：deploy 工单入队时写进 params；其余 kind 没有。
        "purpose": (job.params or {}).get("purpose") if job.kind == "deploy" else None,
        "instance_id": job.instance_id,
        "instance_slug": instance.slug if instance else None,
        "instance_state": instance.state if instance else None,
        "state": job.state,
        "error": job.error,
        "started_at": iso(job.started_at),
        "finished_at": iso(job.finished_at),
        "steps": [
            {
                "seq": st.seq,
                "key": st.key,
                "title": st.title,
                "state": st.state,
                "detail": st.detail,
            }
            for st in steps
        ],
    }


def log_item(row) -> dict[str, Any]:
    return {"id": row.id, "seq": row.seq, "level": row.level, "line": row.line,
            "ts": iso(row.ts)}


def event_item(ev) -> dict[str, Any]:
    return {
        "id": ev.id,
        "kind": ev.kind,
        "instance_id": ev.instance_id,
        "job_id": ev.job_id,
        "message": ev.message,
        "data": ev.data,
        "created_at": iso(ev.created_at),
    }


def image_artifact_item(art, *, base_url: str) -> dict[str, Any]:
    """制品行（§4 GET /api/images）：url = 解析后的 base_url + /dl/{token}/{filename}。

    token 出现在这里与下载链接里——公开链接本来就是免鉴权的（镜像非机密，
    §6.1.5），所以不算泄密；但列表本身在鉴权面内，只有登录者看得到。
    """
    return {
        "id": art.id,
        "tag": art.tag,
        "size_bytes": art.size_bytes,
        "sha256": art.sha256,
        "token": art.token,
        "url": f"{base_url}/dl/{art.token}/{art.filename}",
        "origin_host": art.origin_host,
        "created_at": iso(art.created_at),
    }
