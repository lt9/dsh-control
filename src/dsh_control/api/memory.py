"""记忆中枢的实例级 API（P1；main.py 的注册点见交付回报，本文件不动 main.py/routes.py）。

路由变量名 `router`，挂载方式与 routes.router 同款（会话依赖 + 写请求 X-Requested-With
由挂载方注入）：`app.include_router(memory_router, prefix="/api",
dependencies=[Depends(_require_session)])`，POST 的 CSRF 依赖挂在路由本身
（照 routes 里 `dependencies=[Depends(require_xrw)]` 的先例）。

机器码走 §4.1 现有表（not_found / unauthenticated / missing_x_requested_with），
新增两个：`invalid_action`（422，动作只认 enable/disable）、
`cannot_memory_from_<state>`（409，只有 running 可启停）。

出网打码是硬要求：业务 key 只以 `key_masked`（前 12 位 + ****）露面，
全文件没有一处把 secret 原样塞进响应。
"""

from __future__ import annotations

import shlex
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from ..db import repo
from ..db.models import Credential, DeployJob
from ..integrations import memory_hub as memhub
from ..jobs.runner import HostEndpoint, get_executor, redact_text
from .routes import require_xrw
from .schemas import MemberActionRequest, MemoryActionRequest

router = APIRouter()

# 容器侧只读探针的 home 口径：kit 的 compose 把 DSH_HOME 固定成 /data
# （jobs/capabilities.py 同源）；状态接口用默认值，不额外多跳一次 env 读取。
_PROBE_HOME = "/data"


def _err(status: int, code: str, message: str = "") -> HTTPException:
    detail: dict[str, Any] = {"ok": False, "error": code}
    if message:
        detail["message"] = message
    return HTTPException(status_code=status, detail=detail)


async def _container_patch_state(state: Any, host: Any, pem: str,
                                 container: str) -> dict[str, bool] | None:
    """容器里两份文件的接线事实（best-effort：SSH 层不可用返回 None，不失败接口）。"""
    if host is None or not pem:
        return None
    try:
        ex = get_executor(state.providers)
        ep = HostEndpoint(host.ssh_host, host.ssh_port, host.ssh_user, pem)
        cmd = (f"docker exec {shlex.quote(container)} sh -c "
               + shlex.quote(memhub.script_patch_state(_PROBE_HOME)))
        r = await ex.run(host=ep.host, port=ep.port, user=ep.user,
                         private_key_pem=ep.private_key_pem, command=cmd,
                         timeout=30)
    except Exception:  # noqa: BLE001 —— 状态接口不被宿主机的可用性绑架
        return None
    if not r.ok:
        return None
    return memhub.parse_patch_state(r.stdout)


@router.get("/instances/{slug}/memory")
async def memory_status(request: Request, slug: str) -> dict[str, Any]:
    """这台实例的记忆状态（详情页「记忆」小块的数据源）。

    三层事实分开给，各说各的话：
    - `enabled/missing`：站点配没配（[memory] 段的缺料原因，人话）；
    - `container`：容器里 marker 与 refs 的在位事实——**P0 兼容判据在这里**：
      `p0=true` 即旧记号 `# dsh-control-p0`，`wired` 同样算已接线，
      `legacy_p0=true` 表示接线是 P0 手工版、控制台尚未接管段本身；
    - `hub`：core /health 与 Proxy TCP（控制台视角一次现测，够不着就 null），
      业务 key 在 Hub 侧的有效性用 auth/verify 现核。
    """
    state = request.app.state
    settings = state.settings
    async with state.db.session() as s:
        inst = await repo.get_instance_by_slug(s, slug)
        if inst is None:
            raise _err(404, "not_found")
        host = await repo.get_host(s, inst.host_id)
        pem = ""
        if host is not None and host.ssh_credential_id:
            cred = await s.get(Credential, host.ssh_credential_id)
            pem = (cred.secret or {}).get("private_key_pem", "") if cred else ""

        cred_name = memhub.credential_name_for(slug)
        key_row = await repo.get_credential_by_name(s, cred_name)
        stored = ""
        if key_row is not None and key_row.kind == "memory_user_key":
            stored = str((key_row.secret or {}).get("user_key") or "")

        hub, missing = await memhub.build_memory_hub(s, settings)
        container = await _container_patch_state(state, host, pem,
                                                 inst.container_name or slug)
        wired = None
        legacy_p0 = None
        if container is not None:
            wired = container["memory"] or container["p0"]
            legacy_p0 = bool(container["p0"] and not container["memory"])

        user: dict[str, Any] | None = None
        hub_view: dict[str, Any] | None = None
        if hub is not None and not getattr(hub, "manual_required", False):
            try:
                core_ok = await hub.core_health()
                proxy_ok = await memhub.probe_tcp(
                    str(getattr(settings, "memory_proxy_url", "") or ""))
                hub_view = {"core_ok": core_ok, "proxy_tcp": proxy_ok}
                key_status: str | None = None
                if stored:
                    v = await hub.verify_key(stored)
                    key_status = "valid" if v["valid"] else "invalid"
                    if v.get("user_id"):
                        user = {"user_id": v["user_id"]}
                user = {**(user or {"user_id": memhub.user_id_for(slug)}),
                        "key_masked": memhub.mask_key(stored) if stored else "",
                        "key_status": key_status or "absent"}
            except Exception as exc:  # noqa: BLE001 —— Hub 不可达也要出状态
                hub_view = {"core_ok": False, "proxy_tcp": None,
                            "error": redact_text(str(exc))[:200]}
            finally:
                await hub.aclose()

        last_job = (await s.scalars(
            select(DeployJob).where(DeployJob.instance_id == inst.id,
                                    DeployJob.kind == "memory")
            .order_by(DeployJob.id.desc()).limit(1))).first()

        return {
            "ok": True,
            "slug": slug,
            "enabled": bool(getattr(settings, "memory_enabled", False)),
            "missing": missing if getattr(settings, "memory_enabled", False) else "",
            "wired": wired,               # null = 宿主机够不着，容器侧事实未知
            "legacy_p0": legacy_p0,       # true = P0 手工段，控制台未接管段本身
            "container": container,
            "user": user,
            "hub": hub_view,
            "egress": {
                "space_id": str(getattr(settings, "memory_space_id", "") or "default"),
                "base_url": memhub.memory_base_url(
                    settings, host.name if host else "",
                    host.ssh_host if host else ""),
            },
            "policy": {"purge_default": "keep",
                       "note": "记忆数据永远在 Hub 卷里，本控制台从不删除"},
            "last_job": ({"id": last_job.id, "state": last_job.state,
                          "params": last_job.params or {}} if last_job else None),
        }


@router.post("/instances/{slug}/memory", dependencies=[Depends(require_xrw)])
async def memory_action(request: Request, slug: str,
                        body: MemoryActionRequest) -> dict[str, Any]:
    """启用/停用记忆：只入队 kind="memory" 工单并交给 runner（重跑=再点一次）。

    只有 running 可发起：deploying 有自己的工单在跑，released/purged 的实例
    没有容器可接线（Hub 侧用户原地留着，恢复后直接点启用即可）。
    """
    state = request.app.state
    action = (body.action or "").strip()
    if action not in ("enable", "disable"):
        raise _err(422, "invalid_action",
                   '动作只认 {"action": "enable"} 或 {"action": "disable"}')
    async with state.db.session() as s:
        inst = await repo.get_instance_by_slug(s, slug)
        if inst is None:
            raise _err(404, "not_found")
        if inst.state != "running":
            raise _err(409, f"cannot_memory_from_{inst.state}",
                       "只有运行中的实例能启用/停用记忆；已释放的实例记忆原样"
                       "留在 Hub，恢复实例后再启用即可接上")
        for j in await repo.unfinished_jobs(s):
            if j.instance_id == inst.id:
                raise _err(409, "job_in_progress",
                           f"该实例已有 {j.kind} 任务在跑（job {j.id}），"
                           "等它结束再操作记忆")
        job = await repo.create_job(s, inst.id, "memory", {"action": action})
        await repo.add_event(
            s, "memory_job_queued", instance_id=inst.id, job_id=job.id,
            message=f"记忆{('启用' if action == 'enable' else '停用')}已入队",
            data={"slug": slug, "action": action})
    state.runner.spawn(job.id)
    return {"ok": True, "job_id": job.id, "action": action}


# ---------------------------------------------------------------- 成员治理（P4 最小版）


@router.get("/memory/members")
async def memory_members(request: Request) -> dict[str, Any]:
    """业务成员列表：credentials(kind=memory_user_key) 逐条 + Hub 现核。

    口径说明（前端只照字段渲染，不猜语义）：
    - key_status：valid=auth/verify 现核通过；invalid=key 在但 Hub 不认；
      absent=台账没存 key；hub 够不着时全为 null 并给 hub_note；
    - state：key 现核有效即 active，其余一律 disabled（停用的实现就是吊销 key，
      U-7 口径；记忆数据永远留在 Hub，本接口从不触碰）。
    """
    state = request.app.state
    async with state.db.session() as s:
        rows = (await s.scalars(
            select(Credential).where(Credential.kind == "memory_user_key")
            .order_by(Credential.name))).all()
        hub, missing = await memhub.build_memory_hub(s, state.settings)
        members: list[dict[str, Any]] = []
        try:
            for cred in rows:
                secret = cred.secret or {}
                slug = cred.name.removeprefix("dshctl-")
                stored = str(secret.get("user_key") or "")
                key_status: str | None = None
                if hub is not None:
                    if stored:
                        v = await hub.verify_key(stored)
                        key_status = "valid" if v["valid"] else "invalid"
                    else:
                        key_status = "absent"
                members.append({
                    "slug": slug,
                    "user_id": str(secret.get("user_id")
                                   or memhub.user_id_for(slug)),
                    "key_masked": memhub.mask_key(stored) if stored else "",
                    "key_status": key_status,
                    "state": ("active" if key_status == "valid" else "disabled")
                             if key_status is not None else "unknown",
                })
        finally:
            if hub is not None:
                await hub.aclose()
        return {"ok": True, "members": members,
                "hub_note": "" if hub is not None else missing}


@router.post("/memory/members/{user_id}/action",
             dependencies=[Depends(require_xrw)])
async def memory_member_action(request: Request, user_id: str,
                               body: MemberActionRequest) -> dict[str, Any]:
    """停用＝吊销全部 key（记忆保留）；回收＝吊销后删 Hub 用户并销本地台账。

    两个动作都不删记忆数据（§vendor P1 第 5 条：记忆在 Hub 卷，控制台从不动它）。
    """
    action = (body.action or "").strip()
    if action not in ("disable", "reclaim"):
        raise _err(422, "invalid_action",
                   '动作只认 {"action": "disable"} 或 {"action": "reclaim"}')
    state = request.app.state
    async with state.db.session() as s:
        hub, missing = await memhub.build_memory_hub(s, state.settings)
        if hub is None:
            raise _err(409, "memory_unavailable", missing)
        try:
            rev = await hub.revoke_user_keys(user_id)
            if action == "reclaim":
                await hub.delete_user(user_id)
                rows = (await s.scalars(select(Credential).where(
                    Credential.kind == "memory_user_key"))).all()
                for cred in rows:
                    if str((cred.secret or {}).get("user_id") or "") == user_id:
                        await s.delete(cred)
        finally:
            await hub.aclose()
        await repo.add_event(
            s, "memory_member_action",
            message=("已停用成员 " + user_id if action == "disable"
                     else "已回收成员 " + user_id + "（记忆数据保留）"),
            data={"user_id": user_id, "action": action,
                  "revoked": len(rev.get("revoked") or [])})
    return {"ok": True, "user_id": user_id, "action": action, "revoke": rev}
