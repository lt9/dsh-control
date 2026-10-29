"""能力 API（ADR-0001 决策三 b）：目录列表、实例能力台账、一键动作入队。

独立 APIRouter，**不碰 main.py / routes.py**——注册点回报 lead：在 main.py 与
现有 router 一同 `app.include_router(cap_router, prefix="/api")`（本文件已自带
会话校验依赖，挂不挂外层依赖都安全）。

对账在程序里做、两张表不联（§2.0）：先单表取台账，再按 capability_id 单表取
目录现行版，逐行比 desired_* ↔ observed_*。observed_version 空串 = 版本未知，
按「黄标提示」不按漂移算。动作一律入队 kind=capability 的工单——页面点击、
管理 CLI、API 是同一引擎的三张脸。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from ..db import repo
from ..db.models import Capability, Instance, InstanceCapability
from .auth import session_from_request
from .routes import require_xrw

router = APIRouter()

ACTIONS = ("install", "apply", "enable", "disable", "uninstall")


def _require_session(request: Request) -> None:
    """自带会话闸：lead 挂载时无论是否再叠依赖，这层都在（漏挂也不会裸奔）。"""
    if session_from_request(request) is None:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "unauthenticated"})


def _state(request: Request):
    return request.app.state


class CapabilityActionRequest(BaseModel):
    action: str
    capability: str
    version: str = ""
    params: dict[str, Any] = {}

    @field_validator("action")
    @classmethod
    def _act(cls, v: str) -> str:
        if v not in ACTIONS:
            raise ValueError(f"action 只认 {'|'.join(ACTIONS)}")
        return v

    @field_validator("capability")
    @classmethod
    def _cap(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("capability 不能为空")
        return v


def _catalog_item(c: Capability) -> dict[str, Any]:
    return {
        "capability_id": c.capability_id, "version": c.version, "kind": c.kind,
        "title": c.title, "state": c.state, "current": c.current,
        "compatible_dsh": c.compatible_dsh, "on_missing": c.on_missing,
        "kit_version": c.kit_version, "manifest_sha256": c.manifest_sha256,
        "note": c.note,
        "params": {k: v for k, v in (c.manifest or {}).get("params", {}).items()
                   if k != "on_missing"},
        "default_on": bool((c.manifest or {}).get("default_on", False)),
        "restart_required": bool((c.manifest or {}).get("restart_required", True)),
    }


def _ledger_item(led: InstanceCapability, cap: Capability | None) -> dict[str, Any]:
    """desired ↔ observed 的对账结论（程序里比，不写联表）。"""
    want = led.desired_version or (cap.version if cap and cap.current else "")
    drift = False
    unknown_version = False
    if led.desired_state == "present":
        if not led.installed:
            drift = True                      # 期望在、探针说没装
        elif led.observed_version == "":
            unknown_version = True            # 版本未知：黄标提示，不算漂移
        elif want and led.observed_version != want:
            drift = True                      # 版本对不上 = 漂移
    elif led.desired_state == "absent" and led.installed:
        drift = True                          # 期望卸、还装着
    return {
        "capability_id": led.capability_id,
        "title": cap.title if cap else led.capability_id,
        "kind": cap.kind if cap else "",
        "desired_state": led.desired_state,
        "desired_version": led.desired_version,
        "want_version": want,
        "installed": led.installed,
        "enabled": led.enabled,
        "observed_version": led.observed_version,
        "marker_present": led.marker_present,
        "health": led.health,
        "health_detail": led.health_detail,
        "reason": led.reason,
        "last_job_id": led.last_job_id,
        "observed_at": led.observed_at.isoformat() if led.observed_at else None,
        "in_catalog": cap is not None,
        "drift": drift,
        "unknown_version": unknown_version,
        # 详情页四个按钮的依据（vendor §3.3）
        "actions": ["升级" if drift and led.installed else "重试",
                    "禁用" if led.enabled else "启用", "卸载"],
    }


@router.get("/capabilities", dependencies=[Depends(_require_session)])
async def list_capabilities(request: Request, state: str = "") -> dict[str, Any]:
    """能力目录（capabilities 表）：一行一个「能力+版本」，current 打星。"""
    db = _state(request).db
    async with db.session() as s:
        stmt = select(Capability).order_by(Capability.capability_id,
                                           Capability.id.desc())
        if state:
            stmt = stmt.where(Capability.state == state)
        rows = (await s.scalars(stmt)).all()
    return {"items": [_catalog_item(c) for c in rows]}


@router.get("/instances/{instance_id}/capabilities",
            dependencies=[Depends(_require_session)])
async def list_instance_capabilities(request: Request,
                                     instance_id: int) -> dict[str, Any]:
    """某实例的能力台账 + 对账结论（desired ↔ observed，程序里比）。"""
    db = _state(request).db
    async with db.session() as s:
        inst = await s.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(status_code=404,
                                detail={"ok": False, "error": "instance_not_found"})
        ledgers = (await s.scalars(
            select(InstanceCapability)
            .where(InstanceCapability.instance_id == instance_id)
            .order_by(InstanceCapability.capability_id))).all()
        items = []
        for led in ledgers:
            cap = None
            want = led.desired_version
            if want:
                cap = await s.scalar(select(Capability).where(
                    Capability.capability_id == led.capability_id,
                    Capability.version == want))
            if cap is None:
                cap = await s.scalar(select(Capability).where(
                    Capability.capability_id == led.capability_id,
                    Capability.current.is_(True)))
            items.append(_ledger_item(led, cap))
    return {"instance_id": instance_id, "slug": inst.slug, "items": items}


@router.post("/instances/{instance_id}/capability",
             dependencies=[Depends(require_xrw), Depends(_require_session)])
async def capability_action(request: Request, instance_id: int,
                            body: CapabilityActionRequest) -> dict[str, Any]:
    """一键动作入队（决策三 b）：`{"action", "capability", "version?", "params?"}`
    → `{job_id}`。执行只有一条路：kind=capability 工单 → 引擎 → SSH docker exec。
    secret 参数不走这里——引擎按 manifest schema 从 credentials 现取现塞 stdin。
    """
    state = _state(request)
    async with state.db.session() as s:
        inst = await repo.get_instance(s, instance_id)
        if inst is None:
            raise HTTPException(status_code=404,
                                detail={"ok": False, "error": "instance_not_found"})
        if body.action in ("install", "apply", "enable"):
            stmt = select(Capability).where(
                Capability.capability_id == body.capability,
                Capability.version == body.version if body.version
                else Capability.current.is_(True))
            cap = await s.scalar(stmt)
            if cap is None or cap.state != "published":
                raise HTTPException(status_code=422, detail={
                    "ok": False, "error": "capability_not_published",
                    "message": f"目录里没有可铺的 {body.capability}"
                               f"@{body.version or 'current'}"})
        job_params: dict[str, Any] = {
            "action": body.action, "capability": body.capability,
            "version": body.version, "params": body.params or {},
        }
        job = await repo.create_job(s, instance_id, "capability", job_params)
        await repo.add_event(s, "capability_queued",
                             message=f"{body.capability} {body.action} 已入队"
                                     f"（实例 {inst.slug}）",
                             instance_id=instance_id, job_id=job.id,
                             data={"action": body.action,
                                   "capability": body.capability,
                                   "version": body.version})
    # 自注册 flow：lead 在 main.py 正式注册（CAPABILITY_FLOWS）之前，
    # 走本路由的工单也跑得起来；register 是 dict 覆盖，重复注册无害。
    from ..jobs.capabilities import run_capability
    runner = getattr(state, "runner", None)
    if runner is not None:
        runner.register("capability", run_capability)
        runner.spawn(job.id)
    return {"job_id": job.id}
