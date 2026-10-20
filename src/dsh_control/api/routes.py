"""HTTP 路由。契约 §4 的表就是这里的形状；全部返回 JSON，时间带 +08:00。

完整 token 只出现在 GET /instances/{id}（access.token/url）与 GET /instances/{id}/token
两个 reveal 接口；列表接口绝不返回。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..db import repo
from ..db.models import Instance
from ..domain import slug as slug_mod
from ..domain.timeutil import iso
from ..jobs.capture import TokenNotFound, capture_token
from ..jobs.runner import HostEndpoint, make_runtime, redact_text
from ..settings import get_settings
from . import serialize
from .auth import (
    COOKIE_NAME,
    XRW_HEADER,
    XRW_VALUE,
    clear_session_cookie,
    client_ip,
    constant_time_key_match,
    session_from_request,
    set_session_cookie,
)
from .schemas import CreateInstanceRequest, LoginRequest, ReleaseRequest

public_router = APIRouter()
router = APIRouter()


# ---------------------------------------------------------------- 依赖

def _state(request: Request):
    return request.app.state


async def require_auth(request: Request) -> datetime:
    expires = session_from_request(request)
    if expires is None:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "unauthenticated"})
    return expires


async def require_xrw(request: Request) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.headers.get(XRW_HEADER) != XRW_VALUE:
            raise HTTPException(status_code=403,
                                detail={"ok": False, "error": "missing_x_requested_with"})


async def _load_instance(s, instance_id: int) -> Instance:
    inst = await repo.get_instance(s, instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "not_found"})
    return inst


def _runtime_for_host(providers, host, pem: str):
    return make_runtime(providers, HostEndpoint(host.ssh_host, host.ssh_port,
                                                host.ssh_user, pem))


async def _ssh_pem(s, host_id: int) -> str:
    from ..db.models import Credential
    host = await repo.get_host(s, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "host_not_found"})
    if not host.ssh_credential_id:
        return ""
    cred = await s.get(Credential, host.ssh_credential_id)
    return (cred.secret or {}).get("private_key_pem", "") if cred else ""


# ---------------------------------------------------------------- 公开接口

@public_router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True}


@public_router.post("/login")
async def login(request: Request, body: LoginRequest) -> JSONResponse:
    state = _state(request)
    limiter = state.auth.limiter
    ip = client_ip(request)
    if limiter.is_limited(ip):
        resp = JSONResponse(status_code=429,
                            content={"ok": False, "error": "rate_limited"})
        resp.headers["Retry-After"] = str(limiter.retry_after(ip))
        return resp

    settings = get_settings()
    async with state.db.session() as s:
        cred = await repo.get_credential_by_kind(s, "operator_key")
    stored = ((cred.secret or {}).get("key", "") if cred else "")
    if not stored or not constant_time_key_match(body.key, stored):
        limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail={"ok": False, "error": "bad_key"})
    limiter.record_success(ip)

    resp = JSONResponse(content={"ok": True,
                                 "expires_at": None})  # 占位，下面覆盖
    expires_at = set_session_cookie(resp, settings.session_secret, settings.session_days)
    resp.content = json.dumps({"ok": True, "expires_at": iso(expires_at)}).encode()
    return resp


# ---------------------------------------------------------------- 会话

@router.post("/logout", dependencies=[Depends(require_xrw)])
async def logout() -> JSONResponse:
    resp = JSONResponse(content={"ok": True})
    clear_session_cookie(resp)
    return resp


@router.get("/session")
async def session(expires: datetime = Depends(require_auth)) -> dict[str, Any]:
    return {"ok": True, "expires_at": iso(expires)}


# ---------------------------------------------------------------- 概览 / 主机

@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        return await repo.instance_counts(s)


@router.get("/hosts")
async def hosts(request: Request) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        rows = await repo.list_hosts(s)
    return {
        "items": [
            {"id": h.id, "name": h.name, "ssh_host": h.ssh_host, "ssh_port": h.ssh_port,
             "ssh_user": h.ssh_user, "docker_context": h.docker_context,
             "kit_path": h.kit_path, "data_root": h.data_root,
             "frpc_client_id": h.frpc_client_id, "enabled": h.enabled, "note": h.note}
            for h in rows  # 不含任何密钥
        ]
    }


# ---------------------------------------------------------------- 实例

@router.get("/instances")
async def list_instances(request: Request, q: str = "", state: str = "") -> dict[str, Any]:
    async with _state(request).db.session() as s:
        rows = await repo.list_instances(s, q=q, state=state)
        host_cache: dict[int, Any] = {}
        items = []
        for inst in rows:
            host = host_cache.get(inst.host_id)
            if host is None:
                host = await repo.get_host(s, inst.host_id)
                host_cache[inst.host_id] = host
            items.append(serialize.list_item(inst, host))
    return {"items": items}


@router.post("/instances", dependencies=[Depends(require_xrw)])
async def create_instance(request: Request, body: CreateInstanceRequest) -> dict[str, Any]:
    state = _state(request)
    settings = get_settings()
    async with state.db.session() as s:
        host = await repo.get_host(s, body.host_id)
        if host is None or not host.enabled:
            raise HTTPException(status_code=422,
                                detail={"ok": False, "error": "host_not_available"})
        # slug 由服务端生成（契约 §6.1）；DNS 占用复核在 job 的 plan 步骤
        slugs = await repo.all_slugs(s)
        slug = slug_mod.next_slug(slugs, settings.dns_prefix)
        dns_name = f"{slug}.{settings.dns_zone}"
        inst = Instance(slug=slug, owner_label=body.owner_label, host_id=host.id,
                        container_name=slug, dns_name=dns_name,
                        public_url=f"https://{dns_name}", config_version="",
                        state="pending", desired_state="running")
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        job = await repo.create_job(s, inst.id, "deploy")
        await repo.add_event(s, "deploy_queued", f"新建实例 {slug}（{body.owner_label}）",
                             {"slug": slug, "host": host.name, "note": body.note})
    state.runner.spawn(job.id)
    return {"instance_id": inst.id, "dns_name": dns_name, "job_id": job.id}


@router.get("/instances/{instance_id}")
async def instance_detail(request: Request, instance_id: int) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        inst = await _load_instance(s, instance_id)
        host = await repo.get_host(s, inst.host_id)
        return await serialize.instance_detail(s, inst, host)


@router.get("/instances/{instance_id}/token", dependencies=[Depends(require_xrw)])
async def recapture_token(request: Request, instance_id: int) -> dict[str, Any]:
    """现取：SSH 抓容器日志里的最新一键地址，落库后返回（reveal 接口，完整值）。"""
    state = _state(request)
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        pem = await _ssh_pem(s, inst.host_id)
        host = await repo.get_host(s, inst.host_id)
        runtime = _runtime_for_host(state.providers, host, pem)
        try:
            tok = await capture_token(s, runtime, inst, "manual")
        except TokenNotFound as exc:
            raise HTTPException(status_code=502,
                                detail={"ok": False, "error": "token_not_found",
                                        "message": redact_text(str(exc))}) from exc
        await repo.add_event(s, "token_captured", "手动重抓登录密钥",
                             instance_id=inst.id,
                             data={"source": "manual", "url": tok.url, "token": tok.token})
        return {"token": tok.token, "url": tok.url, "source": tok.source,
                "captured_at": iso(tok.captured_at)}


@router.get("/instances/{instance_id}/events")
async def instance_events(request: Request, instance_id: int,
                          limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        await _load_instance(s, instance_id)
        rows = await repo.list_events(s, instance_id=instance_id, limit=limit)
    return {"items": [serialize.event_item(e) for e in rows]}


# ---------------------------------------------------------------- 重启 / 释放

_RESTARTABLE = {"running", "unhealthy", "restarting", "error"}


@router.post("/instances/{instance_id}/restart", dependencies=[Depends(require_xrw)])
async def restart_instance(request: Request, instance_id: int) -> dict[str, Any]:
    state = _state(request)
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        if inst.state not in _RESTARTABLE:
            raise HTTPException(status_code=409,
                                detail={"ok": False, "error": f"cannot_restart_from_{inst.state}"})
        job = await repo.create_job(s, inst.id, "restart")
    state.runner.spawn(job.id)
    return {"job_id": job.id}


@router.post("/instances/{instance_id}/release", dependencies=[Depends(require_xrw)])
async def release_instance(request: Request, instance_id: int, body: ReleaseRequest) -> dict[str, Any]:
    state = _state(request)
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        # 确认词必须逐字等于 slug，否则 422（契约 §0.2 与用户故事）
        if body.confirm != inst.slug:
            raise HTTPException(status_code=422,
                                detail={"ok": False, "error": "confirm_mismatch",
                                        "message": "确认词必须逐字等于实例 slug"})
        if inst.state == "released":
            raise HTTPException(status_code=409,
                                detail={"ok": False, "error": "already_released"})
        job = await repo.create_job(s, inst.id, "release")
        await repo.add_event(s, "release_queued", f"释放 {inst.slug} 已确认",
                             instance_id=inst.id, job_id=job.id, data={"slug": inst.slug})
    state.runner.spawn(job.id)
    return {"job_id": job.id}


# ---------------------------------------------------------------- 任务 / 日志 / SSE

@router.get("/jobs/{job_id}")
async def job_detail(request: Request, job_id: int) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        job = await repo.get_job(s, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "not_found"})
        steps = await repo.list_steps(s, job_id)
        return serialize.job_payload(job, steps)


@router.get("/jobs/{job_id}/logs")
async def job_logs(request: Request, job_id: int, after: int = Query(0, ge=0),
                   limit: int = Query(500, ge=1, le=2000)) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        job = await repo.get_job(s, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "not_found"})
        rows = await repo.logs_after(s, job_id, after, limit)
    items = [serialize.log_item(r) for r in rows]
    nxt = items[-1]["id"] if items else after
    return {"items": items, "next": nxt}


@router.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: int):
    """SSE：step / log / done 三种事件。轮询库实现——跨进程重启也可靠。"""
    state = _state(request)

    async def generator():
        last_log_id = 0
        step_seen: dict[int, str] = {}
        while True:
            if await request.is_disconnected():
                return
            async with state.db.session() as s:
                job = await repo.get_job(s, job_id)
                if job is None:
                    yield {"event": "done", "data": json.dumps({"state": "not_found"})}
                    return
                steps = await repo.list_steps(s, job_id)
                logs = await repo.logs_after(s, job_id, last_log_id)
            for st in steps:
                sig = f"{st.state}|{st.detail}"
                if step_seen.get(st.id) != sig:
                    step_seen[st.id] = sig
                    yield {"event": "step", "data": json.dumps({
                        "seq": st.seq, "key": st.key, "title": st.title,
                        "state": st.state, "detail": st.detail}, ensure_ascii=False)}
            for lg in logs:
                last_log_id = lg.id
                yield {"event": "log", "data": json.dumps(serialize.log_item(lg),
                                                          ensure_ascii=False)}
            if job.state in ("succeeded", "failed"):
                yield {"event": "done", "data": json.dumps(
                    {"state": job.state, "error": redact_text(job.error or "")},
                    ensure_ascii=False)}
                return
            await asyncio.sleep(0.3)

    return EventSourceResponse(generator(), headers={"X-Accel-Buffering": "no"})
