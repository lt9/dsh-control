"""FastAPI 应用装配。契约 §7：单容器单进程，直接 serve frontend/dist。

app.state 上挂：db / providers / runner / settings / auth。
测试通过 create_app(db=…, providers=…) 注入假实现，**不 import registry**；
只有 providers 未注入（生产）时，lifespan 才先从库里的 site 行覆盖 settings、
再 integrations/registry.build_providers(settings) 装配。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .api.auth import AuthState, session_from_request
from .api.routes import public_router, router
from .db import repo
from .db.models import Credential, DeployJob, utcnow
from .db.session import Db, make_db
from .jobs.deploy import run_deploy
from .jobs.purge import run_purge
from .jobs.release import run_release
from .jobs.restart import run_restart
from .jobs.runner import JobRunner
from .settings import Settings, apply_site_overrides, get_settings
from .watcher import run_loop

log = logging.getLogger("dsh_control")


def _default_static_dir() -> Path:
    # 查找顺序：DSHC_STATIC_DIR > 当前工作目录 > 包自身往上。
    # 与 cli 里找 migrations 同一个道理：镜像内包被装进 site-packages，只看
    # __file__ 的父目录永远找不到 /app/frontend/dist，前端会静默不挂，
    # 症状是 "/" 返回 JSON 而不是页面，很容易被误判成"前端没构建"。
    env = os.environ.get("DSHC_STATIC_DIR", "").strip()
    if env:
        return Path(env)
    cwd_cand = Path.cwd() / "frontend" / "dist"
    if cwd_cand.is_dir():
        return cwd_cand
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "frontend" / "dist"
        if cand.is_dir():
            return cand
    return cwd_cand


async def fail_interrupted_jobs(db: Db) -> int:
    """进程重启留下的 pending/running 任务标 failed（可重跑）。"""
    async with db.session() as s:
        rows = (await s.scalars(
            select(DeployJob).where(DeployJob.state.in_(("pending", "running")))
        )).all()
        for j in rows:
            j.state = "failed"
            j.error = "服务重启导致任务中断，可重新发起"
            j.finished_at = utcnow()
        if rows:
            await s.commit()
            await repo.add_event(s, "jobs_interrupted",
                                 message=f"{len(rows)} 个任务因服务重启被中断")
        return len(rows)


async def load_site_into_settings(db: Db, settings: Settings) -> Settings:
    """把 credentials(kind=site) 的 [site]+[providers] 覆盖进 settings（seed --site 灌入）。"""
    async with db.session() as s:
        cred = await repo.get_credential_by_kind(s, "site")
    return apply_site_overrides(settings, cred.secret if cred else None)


def _require_session(request: Request) -> None:
    if session_from_request(request) is None:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "unauthenticated"})


def create_app(db: Db | None = None, providers: Any | None = None,
               settings: Settings | None = None, start_watcher: bool = True,
               static_dir: Path | None = None) -> FastAPI:
    settings = settings or get_settings()
    injected = providers is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = app.state
        if not injected:
            from .integrations import registry  # 只有生产路径 import 注册表
            st.settings = await load_site_into_settings(st.db, st.settings)
            async with st.db.session() as s:
                creds = (await s.scalars(select(Credential))).all()
            st.providers = registry.build_providers(
                st.settings,
                [{"kind": c.kind, "name": c.name, "secret": c.secret or {}}
                 for c in creds])
            st.runner = JobRunner(st.db, st.providers, st.settings)
            st.runner.register_flows({"deploy": run_deploy, "restart": run_restart,
                                      "release": run_release, "purge": run_purge})
        interrupted = await fail_interrupted_jobs(st.db)
        if interrupted:
            log.warning("中断了 %s 个遗留任务", interrupted)
        tasks = []
        stop_event = asyncio.Event()
        st.watcher_stop = stop_event
        if st.watcher_enabled and st.providers is not None:
            tasks.append(asyncio.create_task(
                run_loop(st.db, st.providers, st.settings, st.runner, stop_event),
                name="dshc-watcher"))
        try:
            yield
        finally:
            stop_event.set()
            for t in tasks:
                t.cancel()
            await st.runner.cancel_all()
            aclose = getattr(st.providers, "aclose", None)
            if callable(aclose):
                await aclose()

    app = FastAPI(title="dsh-control", lifespan=lifespan)
    app.state.db = db or make_db(settings.require("database_url"))
    app.state.providers = providers
    app.state.settings = settings
    app.state.auth = AuthState()
    app.state.watcher_enabled = start_watcher
    runner = JobRunner(app.state.db, providers or _LazyProviders(app), settings)
    runner.register_flows({"deploy": run_deploy, "restart": run_restart,
                           "release": run_release, "purge": run_purge})
    app.state.runner = runner

    app.include_router(public_router, prefix="/api")
    app.include_router(router, prefix="/api",
                       dependencies=[Depends(_require_session)])

    sdir = static_dir if static_dir is not None else _default_static_dir()
    if sdir.is_dir():
        assets = sdir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            candidate = (sdir / full_path).resolve()
            root = sdir.resolve()
            if full_path and str(candidate).startswith(str(root)) and candidate.is_file():
                return FileResponse(candidate)
            index = sdir / "index.html"
            if index.is_file():
                # 入口 HTML 一律不许缓存。assets 带内容哈希，长期缓存没问题；
                # index.html 没有哈希，这里不发 Cache-Control 时浏览器按 Last-Modified
                # 启发式缓存，新版上线后老用户会一直跑旧前端。2026-10-21 现网就这么
                # 把"要打字确认"的新页跑成"点一下就部署"的旧页，凭空开出一台公网实例。
                return FileResponse(index, headers={"Cache-Control": "no-cache"})
            return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    else:
        @app.get("/", include_in_schema=False)
        async def root_hint():
            return {"ok": True, "hint": "前端未构建：frontend/dist 不存在，API 在 /api/*"}

    return app


class _LazyProviders:
    """runner 在 lifespan 装配真实 providers 前的占位；任何调用都会炸出清晰错误。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def __getattr__(self, name: str):
        providers = getattr(self._app.state, "providers", None)
        if providers is None:
            raise RuntimeError("providers 尚未装配（应用未启动或初始化失败）")
        return getattr(providers, name)
