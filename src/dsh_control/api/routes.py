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
from ..domain.config_docs import CONFIG_VERSION
from ..domain.dsh_config import annotate_model_drift
from ..domain.timeutil import iso
from ..jobs.capabilities import read_dsh_config
from ..jobs.capture import TokenNotFound, capture_token
from ..jobs.runner import HostEndpoint, get_executor, make_runtime, redact_text
from . import serialize
from .auth import (
    XRW_HEADER,
    XRW_VALUE,
    clear_session_cookie,
    client_ip,
    constant_time_key_match,
    is_https,
    session_from_request,
    set_session_cookie,
)
from .schemas import (
    CreateInstanceRequest,
    DeployRequest,
    LoginRequest,
    PurgeRequest,
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
                       state.settings.session_days, expires_at=expires_at,
                       secure=is_https(request))
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
        days = await repo.current_purge_after_days(
            s, _state(request).settings.purge_after_days)
        items = []
        for inst in rows:
            host = host_cache.get(inst.host_id)
            if host is None:
                host = await repo.get_host(s, inst.host_id)
                host_cache[inst.host_id] = host
            items.append(serialize.list_item(inst, host, purge_after_days=days))
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
        # 防误建：调用方必须把它要建的名字回显上来（确认卡上显示的那个）。
          # 只在前端设卡等于没设——2026-10-21 现网"连点两次"被真双击穿透，
          # 凭空开出一台公网实例（DNS/隧道/反代/容器全建了）。顺带挡住另一种
          # 尴尬：确认卡算出的名字已被别人抢先占用，那就拒绝，而不是"建成别的"。
        if (body.confirm if body else None) is None or (body.confirm or "").strip() != slug:
            raise HTTPException(status_code=422, detail={
                "ok": False, "error": "confirm_mismatch",
                "message": f"没有创建任何东西：确认的名字与服务端现算的不一致"
                           f"（现在要建的是 {slug}）——重新取号后按确认卡回显再提交。"})
        inst = Instance(slug=slug, host_id=host.id,
                        # 容器名由 kit 决定（dsh-<slug>），创建时就按同一条规则写，
                        # 别留一个"要等第 10 步来纠正"的错值（2026-10-21 现网
                        # 第 70 步抓不到 token 就是这么来的）。
                        container_name=slug_mod.derive(slug, settings.dns_zone).container,
                        dns_name=dns_name, public_url=f"https://{dns_name}",
                        state="pending", desired_state="running")
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        job = await repo.create_job(s, inst.id, "deploy", {"purpose": "create"})
        await repo.add_event(s, "deploy_queued",
                             message=f"新建实例 {slug}（主机 {host.name}）",
                             instance_id=inst.id, job_id=job.id,
                             data={"slug": slug, "host": host.name})
    state.runner.spawn(job.id)
    return {"instance_id": inst.id, "dns_name": dns_name, "job_id": job.id}


@router.get("/instances/next-slug")
async def next_slug(request: Request) -> dict[str, Any]:
    """确认卡用：先问清"下一台叫什么"。创建时要把这个名字回显（见 confirm）。

    必须声明在 /instances/{instance_id} 之前，否则被路径参数路由吃掉。"""
    st = _state(request)
    zone = st.settings.dns_zone or ""
    async with st.db.session() as s:
        slug = slug_mod.next_slug(await repo.all_slugs(s), st.settings.dns_prefix)
    return {"slug": slug, "dns_name": f"{slug}.{zone}" if zone else slug}


@router.get("/instances/{instance_id}")
async def instance_detail(request: Request, instance_id: int) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        inst = await _load_instance(s, instance_id)
        host = await repo.get_host(s, inst.host_id)
        days = await repo.current_purge_after_days(
            s, _state(request).settings.purge_after_days)
        return await serialize.instance_detail(s, inst, host, purge_after_days=days)


@router.patch("/instances/{instance_id}", dependencies=[Depends(require_xrw)])
async def update_instance(request: Request, instance_id: int,
                          body: UpdateInstanceRequest) -> dict[str, Any]:
    """§6.1.1 铅笔就地编辑：只改备注名与备注，不碰域名/容器名/任何外部对象。"""
    async with _state(request).db.session() as s:
        inst = await _load_instance(s, instance_id)
        await repo.update_instance_notes(s, inst, owner_label=body.owner_label,
                                         note=body.note)
        host = await repo.get_host(s, inst.host_id)
        days = await repo.current_purge_after_days(
            s, _state(request).settings.purge_after_days)
        return await serialize.instance_detail(s, inst, host, purge_after_days=days)


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
            res = await capture_token(s, runtime, inst, "manual")
        except TokenNotFound as exc:
            raise HTTPException(status_code=502,
                                detail={"ok": False, "error": "token_not_found",
                                        "message": redact_text(str(exc))}) from exc
        # §3 去重：与库里最新一条相同就不落库、不记事件，但照常 200 返回现取值——
        # 「详情页永远是最新登录密钥」靠现取实现，不靠往库里堆重复行。
        if res.changed:
            await repo.add_event(s, "token_captured", message="手动重抓登录密钥",
                                 instance_id=inst.id,
                                 data={"source": "manual", "url": res.url,
                                       "token": res.token})
        return {"token": res.token, "url": res.url, "source": res.source,
                "captured_at": iso(res.captured_at)}


@router.get("/instances/{instance_id}/dsh-config")
async def refresh_dsh_config(request: Request, instance_id: int) -> dict[str, Any]:
    """现取：SSH 进容器读 DSH 实际生效的那套配置，刷新快照后返回。

    与 /token 同一套规矩：形式是 GET，实质是写式操作，必须带 X-Requested-With。
    读失败一律 502 并且**不动旧快照**——详情页宁可显示"上次读取于 03:12"，
    也不能因为一次 ssh 超时就把她已经配好的东西显示成没配。
    这条接口刻意不放进详情接口本身：台账页一次 ssh 都不做才能秒开，
    而 dump-config 在 NAS 上要 0.6 秒以上。
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
        container = inst.container_name or inst.slug
        try:
            dsh = await read_dsh_config(runtime, container,
                                        dsh_version=inst.dsh_version or "")
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail={"ok": False, "error": "dsh_config_unavailable",
                                        "message": redact_text(str(exc))[:300]}) from exc
        if not dsh:
            raise HTTPException(status_code=502, detail={
                "ok": False, "error": "dsh_config_unavailable",
                "message": "容器里 dsh web --dump-config 没吐出内容"
                           "（容器没起来、镜像里没有 dsh，或 DSH_HOME 不是 /data）"})
        cfg = await repo.latest_config(s, inst.id)
        merged = dict((cfg.rendered if cfg else None) or {})
        dsh = annotate_model_drift(dsh, merged.get("LLM_MODEL_ID"))
        merged["DSH"] = dsh
        inst.config_version = CONFIG_VERSION   # 与快照版本同步，卡头才不会对不上
        await repo.add_config_snapshot(s, inst.id, CONFIG_VERSION, merged)
        await repo.add_event(s, "config_refreshed", message="手动刷新 DSH 配置",
                             instance_id=inst.id,
                             data={"model": dsh.get("model", ""),
                                   "agent_team": bool(dsh.get("agent_team")),
                                   "skills": len(dsh.get("skills") or [])})
        return dsh


@router.get("/instances/{instance_id}/events")
async def instance_events(request: Request, instance_id: int,
                          limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    async with _state(request).db.session() as s:
        await _load_instance(s, instance_id)
        rows = await repo.list_events(s, instance_id=instance_id, limit=limit)
    return {"items": [serialize.event_item(e) for e in rows]}


# ---------------------------------------------------------------- 重启 / 释放 / 彻底释放

_RESTARTABLE = {"running", "unhealthy", "restarting", "error"}
# 续建入口（§4 deploy / §6.1 重试 / §6.1.3 恢复 / 切镜像）：从未走完（pending）、
# 已判死（error）、running（切镜像的正规路径）与 **released（恢复，§6.1.3）**
# 都可重跑。deploy 每一步都是「已存在且一致就跳过」的幂等续建；从 running 走是
# 切镜像的正规路径（实例 .env 的 DSH_IMAGE 与站点目标 tag 不一致时 compose 重建
# 容器——docker restart 不重读 .env、不重建容器，做不到）。代价：会话短暂打断；
# 登录密钥不轮换（cookie secret 在 DATA_DIR/.credentials.yaml，重建不动它）。
# deploying 有 job 在跑；purged 是终态——不在集合里，自然 409（released 的判定
# 只看 state=="released"，purged 是另一个值，绝不与 released 混同）。
# 注意：serialize.py 的 _DEPLOYABLE_STATES 是**页面按钮**口径（pending/error），
# 恢复/升级走 restore/upgrade 新字段，两条独立——这里的宽开口留给 API/脚本。
_DEPLOYABLE = {"pending", "error", "running", "released"}


@router.post("/instances/{instance_id}/deploy", dependencies=[Depends(require_xrw)])
async def retry_deploy(request: Request, instance_id: int,
                       body: DeployRequest | None = None) -> dict[str, Any]:
    """续建统一入口（§4）：开通失败重试（pending/error）、running 升级/切镜像、
    released **恢复**（§6.1.3）都从这里走；purged 拒绝。

    deploy 的每一步都是「已存在且一致就跳过」，所以重跑是**续建**：DNS/frp/NG
    建到哪一步就接着往下走，不重建已经对的东西。恢复即完整跑一遍 deploy：外部
    三处按登记 id 重建/复用，容器 compose up，原 slug/容器名/公网端口回来
    （frp 步先探占用，被占才换端口并 event 说破）。
    body 可选 `{"image": "目标tag"}`：留空 = 站点当前目标 tag。
    详情页的「升级/重建」（running）与「恢复实例」（released）按钮都走这个入口（§9）。
    """
    state = _state(request)
    image_param = (body.image or "").strip() if body else ""
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        if inst.state not in _DEPLOYABLE:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "error": f"cannot_deploy_from_{inst.state}",
                        "message": "该状态不可重跑开通：有任务在跑就等它结束，"
                                   "purged 是终态不可重开；"
                                   "released 恢复与 running 切镜像都直接重跑 deploy"
                                   "（幂等续建，重建容器会短暂打断会话）"})
        for j in await repo.unfinished_jobs(s):
            if j.instance_id == inst.id:
                raise HTTPException(
                    status_code=409,
                    detail={"ok": False, "error": "job_in_progress",
                            "message": f"该实例已有 {j.kind} 任务在跑（job {j.id}），"
                                       "等它结束再重跑"})
        prev_state = inst.state
        # 来路（§4）：入队时按当时状态定死，工单页据此出标题与完成文案，不靠 URL 猜。
        purpose = ("restore" if prev_state == "released"
                   else "upgrade" if prev_state in ("running", "unhealthy")
                   else "create")
        job = await repo.create_job(
            s, inst.id, "deploy",
            {"purpose": purpose, **({"image": image_param} if image_param else {})})
        await repo.add_event(s, "deploy_requeued",
                             message=f"重新开通 {inst.slug}（从 {prev_state} 续建）",
                             instance_id=inst.id, job_id=job.id,
                             data={"from": prev_state, "purpose": purpose,
                                   **({"image": image_param} if image_param else {})})
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


@router.post("/instances/{instance_id}/purge", dependencies=[Depends(require_xrw)])
async def purge_instance(request: Request, instance_id: int,
                         body: PurgeRequest) -> dict[str, Any]:
    """彻底释放（§6.4）：released 的终态化——删容器、删数据目录，不可恢复。

    确认词逐字等于 slug（同 release：值由前端填，长按确认，服务端不因体验放松）。
    仅 released 可发起：running/error 等一律 409 cannot_purge_from_<state>——
    对还在跑的实例删数据是灾难；purged 也是 409（已是终态）。
    """
    state = _state(request)
    async with state.db.session() as s:
        inst = await _load_instance(s, instance_id)
        if body.confirm != inst.slug:
            raise HTTPException(status_code=422,
                                detail={"ok": False, "error": "confirm_mismatch",
                                        "message": "确认词必须逐字等于实例 slug"})
        if inst.state != "released":
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "error": f"cannot_purge_from_{inst.state}",
                        "message": "只有已释放（released）的实例可以彻底释放"})
        for j in await repo.unfinished_jobs(s):
            if j.instance_id == inst.id:
                raise HTTPException(
                    status_code=409,
                    detail={"ok": False, "error": "job_in_progress",
                            "message": f"该实例已有 {j.kind} 任务在跑（job {j.id}），"
                                       "等它结束再彻底释放"})
        job = await repo.create_job(s, inst.id, "purge",
                                    {"source": "manual", "confirm": inst.slug})
        await repo.add_event(s, "purge_queued", message=f"彻底释放 {inst.slug} 已确认",
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
        inst = await repo.get_instance(s, job.instance_id) if job.instance_id else None
        return serialize.job_payload(job, steps, inst)


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
