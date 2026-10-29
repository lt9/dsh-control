"""MemoryPanel 的同源反代壳（MEMORY-PANEL-EMBED.md ③-A 的落地，新文件）。

注册点（lead 在 main.py create_app 执行，位置有硬要求——必须在
`app.mount("/assets", ...)` 与 SPA 兜底 `@app.get("/{full_path:path}")` **之前**）：

    from .api.memory_proxy import router as memory_proxy_router
    app.include_router(memory_proxy_router, dependencies=[Depends(_require_session)])

三类路由：

1. `ANY /api/v1/{path}`：反代 Panel 的业务面。
   - 上游基址只取 settings.memory_panel_base（缺省 _DEFAULT_PANEL_BASE，
     不接受请求里的 host/scheme——无 SSRF 面）；路径白名单
     `^/api/v1/[a-z0-9/_-]+$`，`..` 与 `//` 一律 404（③-A.1）。
   - 剥掉客户端传入的 `x-tdai-user-key`，由服务端注入 credentials 里的
     admin key（kind=memory_admin_key，口径与 integrations/memory_hub 一致）——
     客户端不能指定身份，业务 key 从头到尾不进浏览器（方案 1 的立身之本）。
   - 免登：`GET /api/v1/auth/session` 由代理自己应答 authenticated:true
     （形状照 ③-A.1.4，src/panel/http/routes/auth.ts:116-126）。
   - `POST /api/v1/knowledge/status-callback` 直接 404：那是 KS 走内网直连
     Panel 的回调面，不该从浏览器这一侧进（②-2 无鉴权那条）。
   - 其余原样透传状态码与信封，不重试（②-4：上游扇出故障时代理重试会把
     放大成更糟的事）。读超时 90s、连接 5s（②-4：清空记忆 60s 是合法的慢）。
2. `GET /panel/{path}`：Panel dist 静态（settings.memory_panel_dist，缺省
   `/opt/dsh-panel/dist`，lead 部署时挂卷进容器）。resolve + 前缀比对防穿越
   （照 main.py SPA 兜底的既有写法）；index.html 一律 `Cache-Control: no-cache`
   ——main.py 里 2026-10-21 的现网教训，入口 HTML 不回源核对就会跑旧前端。
3. `GET /assets/{path}` 与 `GET /logo.png`：Panel 的 dist 资源是根绝对路径
   （③-A.2，不改 Panel 源文件的既定绕法）。本 router 注册在控制台自己的
   `app.mount("/assets")` 之前，所以 `/assets/*` 先到这里：**先查控制台自己的
   dist**（vite 哈希文件名，查得到就原样serve，控制台自己的包一个不少），
   查不到再落 Panel dist；都查不到才 404。`/logo.png` 同理，本地没有才转发上游。

CSRF 口径（与契约文档 ③-A 的差异，回报里已列给 lead）：整前缀挂会话依赖照办；
`/api/v1` 的 POST **不再叠 require_xrw**——嵌进来的 Panel 前端自己不发这个头，
叠上会把 Panel 所有写动作全灭；挡跨站 POST 的是既有的 SameSite=Lax 会话 cookie，
身份锁由「剥客户端 key + 服务端注入」保证。控制台的 `/api/*` 照旧 XRW，不受影响。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.responses import Response as StarletteResponse

from ..api.auth import session_from_request
from ..db import repo

log = logging.getLogger("dshc.memory_proxy")
router = APIRouter()


def _require_session(request: Request) -> None:
    """自带会话闸（照 capabilities.py 同款）：/panel 与 /api/v1 各自挂死，
    不依赖 lead 挂载时叠不叠依赖。/assets 与 /logo.png 不闸——它们走的是
    控制台 StaticFiles 一贯的公开口径（登录页自己要用），闸了会白屏。"""
    if session_from_request(request) is None:
        raise HTTPException(status_code=401, detail={"ok": False,
                                                     "error": "unauthenticated"})

# 上游基址与静态目录的缺省值：settings 里有 memory_panel_base / memory_panel_dist
# 两个字段后（settings.py 归 lead），getattr 自然生效；没有就用站点现场值。
_DEFAULT_PANEL_BASE = "http://192.168.3.109:8125"
_DEFAULT_PANEL_DIST = "/opt/dsh-panel/dist"

# ③-A.1 的路径白名单：小写、数字、斜杠、下划线、连字符；.. 与 // 单独拒。
_V1_RE = re.compile(r"^/api/v1/[a-z0-9/_-]+$")

# 会话应答里这张「我是谁」：与 frontend/src/lib/panelSession.ts 的引导会话一致。
_ADMIN_USER_ID = "dshctl-console"
_ADMIN_USER_NAME = "控制台运维"

# 透传的请求头（Panel 前端会带的都放行）；x-tdai-user-key 一律剥掉再注入，
# cookie 一律不转——dshc_session 是控制台的凭据，不进上游的嘴（§0.4 口径）。
_FORWARD_REQ_HEADERS = ("content-type", "accept", "accept-language",
                        "x-tdai-service-id")
_STRIP_RES_HEADERS = {"content-encoding", "content-length", "transfer-encoding",
                      "connection", "keep-alive", "set-cookie", "date", "server"}


def _err(status: int, code: str, message: str = "") -> HTTPException:
    detail: dict[str, Any] = {"ok": False, "error": code}
    if message:
        detail["message"] = message
    return HTTPException(status_code=status, detail=detail)


def _panel_base(request: Request) -> str:
    return str(getattr(request.app.state.settings, "memory_panel_base", "")
               or _DEFAULT_PANEL_BASE).rstrip("/")


def _panel_dist(request: Request) -> Path:
    return Path(str(getattr(request.app.state.settings, "memory_panel_dist", "")
                    or _DEFAULT_PANEL_DIST))


def _console_dist(request: Request) -> Path:
    """控制台自己的 dist 根。

    优先读 app 上已挂载的 /assets StaticFiles 的 directory（那是权威位置，且与
    create_app 传入的 static_dir 天然一致）；没挂 StaticFiles 时退回 main.py 的
    同一条查找链（惰性 import 防循环依赖）。这样即便 lead 传了自定义 static_dir，
    控制台自己的哈希资源也绝不会被我的 /assets 兜底误判成「找不到」。
    """
    for route in getattr(request.app.router, "routes", []):
        if getattr(route, "path", None) != "/assets":
            continue
        # directory 挂在 Mount 包着的 StaticFiles 实例上，不在 Mount 本身
        directory = (getattr(getattr(route, "app", None), "directory", None)
                     or getattr(route, "directory", None))
        if directory:
            return Path(str(directory)).parent
    from ..main import _default_static_dir
    return _default_static_dir()


def _safe_file(root: Path, rel: str) -> Path | None:
    """resolve 后前缀比对（照 main.py:172-175 的既有写法），越界与非文件给 None。"""
    if not rel or rel.endswith("/"):
        return None
    try:
        candidate = (root / rel).resolve()
    except OSError:
        return None
    root_r = root.resolve()
    if not str(candidate).startswith(str(root_r)) or not candidate.is_file():
        return None
    return candidate


def _panel_client(request: Request) -> httpx.AsyncClient:
    """上游客户端挂在 app.state 上复用连接；测试替身 monkeypatch 这个函数。"""
    client = getattr(request.app.state, "_memory_proxy_client", None)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            base_url=_panel_base(request),
            timeout=httpx.Timeout(90, connect=5),
            follow_redirects=False,
        )
        request.app.state._memory_proxy_client = client
    return client


async def _admin_key(request: Request) -> str:
    """记忆管理 key：与 memory_hub.build_memory_hub 同一找法（先名字后 kind）。"""
    async with request.app.state.db.session() as s:
        cred = await repo.get_credential_by_name(s, "memory-admin")
        if cred is None or cred.kind != "memory_admin_key":
            cred = await repo.get_credential_by_kind(s, "memory_admin_key")
        return str(((cred.secret or {}) if cred else {}).get("user_key") or "").strip()


async def _file_or_index(root: Path, rel: str) -> StarletteResponse:
    """Panel 静态的兜底：hash 路由深链由 index.html 自己消化，未知路径回入口页。"""
    f = _safe_file(root, rel)
    if f is not None:
        return FileResponse(f)
    index = root / "index.html"
    if not index.is_file():
        raise _err(404, "not_found", "Panel 静态没挂上：容器里找不到 "
                                     f"{index}（lead 部署时挂卷 memory_panel_dist）")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- 静态面

@router.get("/panel", include_in_schema=False,
            dependencies=[Depends(_require_session)])
@router.get("/panel/{path:path}", include_in_schema=False,
            dependencies=[Depends(_require_session)])
async def panel_static(request: Request, path: str = "") -> StarletteResponse:
    return await _file_or_index(_panel_dist(request), path)


@router.get("/assets/{path:path}", include_in_schema=False)
async def assets_fallback(request: Request, path: str) -> StarletteResponse:
    """控制台 dist 优先、Panel dist 兜底（③-A.2：不改 Panel 源文件的 /assets 绕法）。

    本路由注册在控制台自己的 /assets 挂载之前，接管了这一前缀：
    控制台的 vite 哈希文件先在这里查到自己的一份，一个不少；Panel 的哈希文件
    在控制台里查不到，落到 Panel dist——两边的文件名都带内容哈希，不会互相顶。
    """
    for root in (_console_dist(request) / "assets", _panel_dist(request) / "assets"):
        f = _safe_file(root, path)
        if f is not None:
            return FileResponse(f)
    raise _err(404, "not_found")


@router.get("/logo.png", include_in_schema=False)
async def logo_png(request: Request) -> StarletteResponse:
    """Panel 前端硬编码 /logo.png 两处（②-2）：本地 dist 有就直接给，没有转上游。"""
    for root in (_console_dist(request), _panel_dist(request)):
        f = _safe_file(root, "logo.png")
        if f is not None:
            return FileResponse(f)
    try:
        r = await _panel_client(request).get("/logo.png")
    except httpx.HTTPError:
        raise _err(404, "not_found")
    if r.status_code != 200 or not r.content:
        raise _err(404, "not_found")
    return Response(content=r.content, status_code=200,
                    media_type=r.headers.get("content-type", "image/png"))


# ---------------------------------------------------------------- 反代面

@router.api_route("/api/v1/{path:path}", methods=["GET", "POST"],
                  dependencies=[Depends(_require_session)])
async def panel_api(request: Request, path: str) -> StarletteResponse:
    full = f"/api/v1/{path}"
    # 白名单 + 显式拒 .. 与 //（正则的字符集容得下空段，③-A.1 点名要拒）。
    if not _V1_RE.fullmatch(full) or ".." in full or "//" in full:
        raise _err(404, "not_found")
    # KS 的内网回调面不从浏览器进（②-2：这条完全无鉴权）。
    if full == "/api/v1/knowledge/status-callback":
        raise _err(404, "not_found")
    # 免登应答：形状照 ③-A.1.4（auth.ts:116-126），不敲上游。
    if request.method == "GET" and full == "/api/v1/auth/session":
        return JSONResponse({
            "ok": True, "authenticated": True, "method": "idp",
            "instance_id": "default", "user_id": _ADMIN_USER_ID,
            "user": {"user_id": _ADMIN_USER_ID, "name": _ADMIN_USER_NAME,
                     "display_name": _ADMIN_USER_NAME},
        })

    admin_key = await _admin_key(request)
    if not admin_key:
        raise _err(503, "memory_admin_key_missing",
                   "记忆管理密钥还没登记进凭据（credentials kind=memory_admin_key）")

    headers = {k: v for k in _FORWARD_REQ_HEADERS
               if (v := request.headers.get(k)) is not None}
    headers["x-tdai-user-key"] = admin_key   # 客户端同名头已剥，这里只认服务端的
    body = await request.body()              # 原样转发，不做 JSON 重序列化（②-4）
    try:
        upstream = await _panel_client(request).request(
            request.method, full, content=body, headers=headers,
            params=request.url.query or None)
    except httpx.HTTPError as exc:
        # 502 的 message 不带异常原文里的 URL 之外内容；这里连上游地址都不回显。
        log.warning("记忆面板反代失败 %s %s: %s", request.method, full, exc)
        raise _err(502, "memory_panel_unreachable",
                   "记忆面板没在跑或够不着——先确认 Hub 容器健康，再重试这一动作")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={k: v for k, v in upstream.headers.items()
                 if k.lower() not in _STRIP_RES_HEADERS},
    )
