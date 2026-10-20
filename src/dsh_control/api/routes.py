"""HTTP 路由。契约 §4 的表就是这里的形状；全部返回 JSON，时间带 +08:00。

完整 token 只出现在 GET /instances/{id}（access.token/url）与 GET /instances/{id}/token
两个 reveal 接口；列表接口绝不返回。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..db import repo
from ..db.models import Credential, Host, Instance
from ..domain import host_pick
from ..domain import slug as slug_mod
from ..domain.timeutil import iso
from ..jobs.capture import TokenNotFound, capture_token
from ..jobs.runner import HostEndpoint, get_executor, make_runtime, redact_text
from . import serialize
from .auth import (
    XRW_HEADER,
    XRW_VALUE,
    clear_session_cookie,
    client_ip,
    constant_time_key_match,
    session_from_request,
    set_session_cookie,
)
from .schemas import (
    CreateInstanceRequest,
    LoginRequest,
    ReleaseRequest,
    UpdateInstanceRequest,
)

public_router = APIRouter()
router = APIRouter()


# ---------------------------------------------------------------- 依赖

def _state(request: Request):
    return request.app.state


async def require_xrw(request: Request) -> None:
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.headers.get(XRW_HEADER) != XRW_VALUE):
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


async def _host_ssh_pem(s, host: Host) -> str:
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

    async with state.db.session() as s:
        cred = await repo.get_credential_by_kind(s, "operator_key")
    stored = ((cred.secret or {}).get("key", "") if cred else "")
    if not stored:
        raise HTTPException(status_code=500,
                            detail={"ok": False, "error": "not_initialized",
                                    "message": "运维密钥未初始化：先跑 cli init"})
    if not constant_time_key_match(body.key, stored):
        limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail={"ok": False, "error": "bad_key"})
    limiter.record_success(ip)

    # 响应体必须**一次成型**：JSONResponse 在构造时就把 content-length 定死了，
    # 之后再改 resp.body 会让实际字节数超过声明长度，uvicorn 直接掐断连接
    # （"Response content longer than Content-Length"）。TestClient 不校验这个，
    # 所以只有真机上会炸——登录成功分支曾因此对所有真实客户端不可用。
    expires_at = datetime.now(UTC) + timedelta(days=state.settings.session_days)
    resp = JSONResponse(status_code=200, content={"ok": True, "expires_at": iso(expires_at)})
    set_session_cookie(resp, state.settings.session_secret,
                       state.settings.session_days, expires_at=expires_at)
    return resp


# ---------------------------------------------------------------- 会话

@router.post("/logout", dependencies=[Depends(require_xrw)])
async def logout() -> JSONResponse:
    resp = JSONResponse(content={"ok": True})
    clear_session_cookie(resp)
    return resp


@router.get("/session")
async def session(request: Request) -> dict[str, Any]:
    expires = session_from_request(request)
    return {"ok": True, "expires_at": iso(expires) if expires else None}


# ---------------------------------------------------------------- 概览 / 主机

@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    st = _state(request)
    async with st.db.session() as s:
        counts = await repo.instance_counts(s)
    # 前端确认卡要把 dsh-01 显示成 dsh-01.<zone> 完整域名（空系统也显示得出来）
    return {**counts, "dns_zone": st.settings.dns_zone}


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
        # §2.0 规矩二：不联表。单表取主机后 Python 组装（主机数量级很小）。
        host_cache: dict[int, Any] = {}
        items = []
        for inst in rows:
            host = host_cache.get(inst.host_id)
            if host is None:
                host = await repo.get_host(s, inst.host_id)
                host_cache[inst.host_id] = host
            items.append(serialize.list_item(inst, host))
    return {"items": items}


async def _pick_host(request: Request, s) -> Host:
    """§6.1.1：enabled 主机里 data_root 剩余空间最大（机械盘优先）的一台。
    逐台 SSH 探测（`df -P` + rotational），全部探不到时退回第一台 enabled 主机。"""
    state = _state(request)
    settings = state.settings
    hosts = [h for h in await repo.list_hosts(s) if h.enabled]
    if not hosts:
        raise HTTPException(status_code=422, detail={"ok": False,
                                                     "error": "no_enabled_host"})
    probes: list[host_pick.HostProbe] = []
    for h in hosts:
        pem = await _host_ssh_pem(s, h)
        try:
            r = await get_executor(state.providers).run(
                host=h.ssh_host, port=h.ssh_port, user=h.ssh_user,
                private_key_pem=pem, command=host_pick.probe_command(h.data_root),
                timeout=15,
            )
            avail, rota = host_pick.parse_probe_output(r.stdout if r.ok else "")
        except Exception:  # noqa: BLE001 —— 探不到不致命
            avail, rota = None, None
        probes.append(host_pick.HostProbe(h.id, avail, rota))
    best = host_pick.rank(probes, prefer_hdd=settings.prefer_hdd)
    chosen_id = best.host_id if best else hosts[0].id
    host = await repo.get_host(s, chosen_id)
    assert host is not None
    return host


@router.post("/instances", dependencies=[Depends(require_xrw)])
async def create_instance(
    request: Request, body: CreateInstanceRequest | None = None
) -> dict[str, Any]:
    state = _state(request)
    settings = state.settings
    if not settings.dns_zone:
        raise HTTPException(status_code=422, detail={"ok": False, "error": "site_not_seeded",
                                                     "message": "缺少站点配置：先 cli seed --site"})
    async with state.db.session() as s:
        if body and body.host_id:
            host = await repo.get_host(s, body.host_id)
            if host is None or not host.enabled:
                raise HTTPException(status_code=422,
                                    detail={"ok": False, "error": "host_not_available"})
        else:
            host = await _pick_host(request, s)
        # slug 服务端顺序生成（§6.1.1）；DNS 占用复核在 job 的 plan 步骤
        slugs = await repo.all_slugs(s)
        slug = slug_mod.next_slug(slugs, settings.dns_prefix)
        dns_name = f"{slug}.{settings.dns_zone}"
        inst = Instance(slug=slug, host_id=host.id, container_name=slug,
                        dns_name=dns_name, public_url=f"https://{dns_name}",
                        state="pending", desired_state="running")
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        job = await repo.create_job(s, inst.id, "deploy")
        await repo.add_event(s, "deploy_queued",
                             message=f"新建实例 {slug}（主机 {host.name}）",
                             instance_id=inst.id, job_id=job.id,
                             data={"slug": slug, "host": host.name})
    state.runner.spawn(job.id)
    return {"instance_id": inst.id, "dns_name": dns_name, "job_id": job.id}


@router.get("/instances/{instance_id}")
async def instance_detail(request: Request, instance_id: int) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        inst = await _load_instance(s, instance_id)
        host = await repo.get_host(s, inst.host_id)
        return await serialize.instance_detail(s, inst, host)


@router.patch("/instances/{instance_id}", dependencies=[Depends(require_xrw)])
async def update_instance(request: Request, instance_id: int,
                          body: UpdateInstanceRequest) -> dict[str, Any]:
    """§6.1.1 铅笔就地编辑：只改备注名与备注，不碰域名/容器名/任何外部对象。"""
    async with _state(request).db.session() as s:
        inst = await _load_instance(s, instance_id)
        await repo.update_instance_notes(s, inst, owner_label=body.owner_label,
                                         note=body.note)
        host = await repo.get_host(s, inst.host_id)
        return await serialize.instance_detail(s, inst, host)


@router.get("/instances/{instance_id}/token")
async def recapture_token(request: Request, instance_id: int) -> dict[str, Any]:
    """现取：SSH 抓容器日志里的最新一键地址，落库后返回（reveal 接口，完整值）。

    虽是 GET 但属「取回密钥」的写式操作：必须带 X-Requested-With（契约 §0 鉴权）。
    """
    if request.headers.get(XRW_HEADER) != XRW_VALUE:
        raise HTTPException(status_code=403,
                            detail={"ok": False, "error": "missing_x_requested_with"})
    state = _state(request)
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        host = await repo.get_host(s, inst.host_id)
        if host is None:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "host_not_found"})
        pem = await _host_ssh_pem(s, host)
        runtime = _runtime_for_host(state.providers, host, pem)
        try:
            tok = await capture_token(s, runtime, inst, "manual")
        except TokenNotFound as exc:
            raise HTTPException(status_code=502,
                                detail={"ok": False, "error": "token_not_found",
                                        "message": redact_text(str(exc))}) from exc
        await repo.add_event(s, "token_captured", message="手动重抓登录密钥",
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
# 开通失败后的续建入口（§6.1「重试」）：只有从未走完（pending）和已判死（error）可重跑。
# deploying 说明有 job 在跑，running 该走 restart，released 是终态，都拒绝。
_DEPLOYABLE = {"pending", "error"}


@router.post("/instances/{instance_id}/deploy", dependencies=[Depends(require_xrw)])
async def retry_deploy(request: Request, instance_id: int) -> dict[str, Any]:
    """开通失败后重跑（§6.1「重试」）。

    deploy 的每一步都是「已存在且一致就跳过」，所以重跑是**续建**：DNS/frp/NG
    建到哪一步就接着往下走，不重建已经对的东西。只在从未走完（pending）或已判死
    （error）时允许；deploying 有 job 在跑、released 是终态、running 该走 restart。
    """
    state = _state(request)
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        if inst.desired_state == "released" or inst.state not in _DEPLOYABLE:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "error": f"cannot_deploy_from_{inst.state}",
                        "message": "只有开通失败或尚未开通的实例可以重跑；"
                                   "运行中的实例请走重启"})
        for j in await repo.unfinished_jobs(s):
            if j.instance_id == inst.id:
                raise HTTPException(
                    status_code=409,
                    detail={"ok": False, "error": "job_in_progress",
                            "message": f"该实例已有 {j.kind} 任务在跑（job {j.id}），"
                                       "等它结束再重跑"})
        prev_state = inst.state
        job = await repo.create_job(s, inst.id, "deploy")
        await repo.add_event(s, "deploy_requeued",
                             message=f"重新开通 {inst.slug}（从 {prev_state} 续建）",
                             instance_id=inst.id, job_id=job.id,
                             data={"from": prev_state})
    state.runner.spawn(job.id)
    return {"job_id": job.id}


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
async def release_instance(request: Request, instance_id: int,
                           body: ReleaseRequest) -> dict[str, Any]:
    state = _state(request)
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        # 确认词必须逐字等于 slug，否则 422（契约 §0.2）。
        # 值由前端自己填（§9 按住确认），服务端校验不因体验放松。
        if body.confirm != inst.slug:
            raise HTTPException(status_code=422,
                                detail={"ok": False, "error": "confirm_mismatch",
                                        "message": "确认词必须逐字等于实例 slug"})
        if inst.state == "released":
            raise HTTPException(status_code=409,
                                detail={"ok": False, "error": "already_released"})
        job = await repo.create_job(s, inst.id, "release")
        await repo.add_event(s, "release_queued", message=f"释放 {inst.slug} 已确认",
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
