"""/panel 反代壳单测（api/memory_proxy.py）：上游全走 httpx.MockTransport。

覆盖的是安全面而不是 UI 转发：白名单拒穿越、剥客户端身份头注服务端 admin key、
auth/session 由代理应答（不敲上游）、status-callback 404、上游故障透传成 502、
整前缀的会话闸。注册方式照 docstring 写给 lead 的那两行——include_router +
会话依赖，所以这里也这样挂（生产里位置在 SPA 兜底之前，由 lead 在 main.py 保证）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest_asyncio
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from dsh_control.api import memory_proxy
from dsh_control.db import repo
from dsh_control.main import _require_session
from tests.conftest import XRW

PANEL_BASE = "http://panel.test"
ADMIN_KEY = "sk-mem-admin00000000"


@pytest_asyncio.fixture
async def proxied(env, tmp_path, monkeypatch):
    """env 之上挂好反代壳：假 Panel dist + 假 console dist + MockTransport 上游 + admin key。"""
    # Panel dist（settings.memory_panel_dist 的替身）
    pdist = tmp_path / "panel"
    (pdist / "assets").mkdir(parents=True)
    (pdist / "index.html").write_text("<!doctype html><title>Panel</title>", encoding="utf-8")
    (pdist / "assets" / "main-PANELHASH.js").write_text("panel-main", encoding="utf-8")
    (pdist / "logo.png").write_bytes(b"PNGPANEL")
    # console dist（_console_dist() 的替身：控制台自己的哈希资源必须优先命中）
    cdist = tmp_path / "cdist"
    (cdist / "assets").mkdir(parents=True)
    (cdist / "assets" / "index-CONSOLE.js").write_text("console-main", encoding="utf-8")
    # dist 外面放一个机密文件：穿越防线要是破了，测试能看见它的字
    (tmp_path / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")

    monkeypatch.setattr(memory_proxy, "_DEFAULT_PANEL_DIST", str(pdist))
    monkeypatch.setattr(memory_proxy, "_DEFAULT_PANEL_BASE", PANEL_BASE)
    monkeypatch.setattr(memory_proxy, "_console_dist", lambda _request: cdist)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/meta/echo":
            return httpx.Response(200, json={"code": 0, "echo": request.content.decode()},
                                  headers={"x-up": "1"})
        if request.url.path == "/api/v1/meta/down":
            raise httpx.ConnectError("假上游没在跑")
        return httpx.Response(404, json={"code": 404})

    def fake_client(_request):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url=PANEL_BASE,
                                 timeout=httpx.Timeout(90, connect=5))

    monkeypatch.setattr(memory_proxy, "_panel_client", fake_client)

    async with env.db.session() as s:
        await repo.upsert_credential(s, kind="memory_admin_key", name="memory-admin",
                                     secret={"user_key": ADMIN_KEY}, note="测试 admin")
        await s.commit()

    # 照注册点文档的挂法（lead 在 main.py 的两行，位置在 SPA 兜底之前）
    env.app.include_router(memory_proxy.router,
                           dependencies=[Depends(_require_session)])
    env.app.state._memory_proxy_client = None   # 别拿上一个测试的客户端
    return seen


async def test_panel_index_served_and_not_cacheable(proxied, env):
    for path in ("/panel", "/panel/"):
        r = await env.client.get(path)
        assert r.status_code == 200, path
        assert "Panel" in r.text
        assert r.headers.get("cache-control") == "no-cache", path


async def test_panel_asset_served(proxied, env):
    r = await env.client.get("/panel/assets/main-PANELHASH.js")
    assert r.status_code == 200
    assert r.text == "panel-main"


async def test_traversal_does_not_leak(proxied, env):
    # URL 层怎么归一化由 httpx/Starlette 定，底线是同一条：dist 外的字绝不露面。
    r = await env.client.get("/panel/%2e%2e/secret.txt")
    assert "TOP-SECRET" not in r.text
    # 函数层直接验 resolve + 前缀比对这条防线本身。
    root = Path(memory_proxy._DEFAULT_PANEL_DIST)
    assert memory_proxy._safe_file(root, "../secret.txt") is None
    assert memory_proxy._safe_file(root, "index.html") is not None


async def test_assets_console_first_panel_fallback(proxied, env):
    r = await env.client.get("/assets/index-CONSOLE.js")
    assert r.status_code == 200 and r.text == "console-main"   # 控制台自己的包一个不少
    r = await env.client.get("/assets/main-PANELHASH.js")
    assert r.status_code == 200 and r.text == "panel-main"     # Panel 的落 Panel dist
    r = await env.client.get("/assets/nope.js")
    assert r.status_code == 404


async def test_logo_from_panel_dist(proxied, env):
    r = await env.client.get("/logo.png")
    assert r.status_code == 200
    assert r.content == b"PNGPANEL"


async def test_auth_session_answered_locally(proxied, env):
    r = await env.client.get("/api/v1/auth/session")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["method"] == "idp"
    assert body["instance_id"] == "default"
    assert body["user_id"]
    assert proxied == []          # 免登应答一次都没敲上游


async def test_forward_injects_admin_key_and_strips_client_identity(proxied, env):
    # env.client 本就带着真实会话 cookie；代理既不把它转上游，也不让客户端自报身份头。
    assert env.client.cookies.get("dshc_session") is not None
    r = await env.client.post("/api/v1/meta/echo", json={"q": 1},
                              headers={**XRW, "x-tdai-user-key": "evil-client-key"})
    assert r.status_code == 200
    assert r.json() == {"code": 0, "echo": '{"q":1}'}
    up = proxied[-1].headers
    assert up["x-tdai-user-key"] == ADMIN_KEY          # 服务端注入，不是 evil-client-key
    assert "cookie" not in up                          # 控制台会话不进上游
    assert proxied[-1].url.path == "/api/v1/meta/echo"


async def test_whitelist_rejects_before_upstream(proxied, env):
    n = len(proxied)
    for path in ("/api/v1/meta/a.json", "/api/v1/a//b", "/api/v1/META/user/list"):
        r = await env.client.get(path)
        assert r.status_code == 404, path
    assert len(proxied) == n                            # 全在白名单外，上游零请求


async def test_status_callback_denied(proxied, env):
    n = len(proxied)
    r = await env.client.post("/api/v1/knowledge/status-callback", json={}, headers=XRW)
    assert r.status_code == 404
    assert len(proxied) == n


async def test_upstream_down_is_502_with_machine_code(proxied, env):
    r = await env.client.get("/api/v1/meta/down")
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "memory_panel_unreachable"


async def test_missing_admin_key_503(proxied, env, monkeypatch):
    async def no_key(_request):
        return ""
    monkeypatch.setattr(memory_proxy, "_admin_key", no_key)
    r = await env.client.post("/api/v1/meta/echo", json={}, headers=XRW)
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "memory_admin_key_missing"


async def test_prefix_requires_session(proxied, env):
    # 没登录的第二把客户端：整前缀都在会话闸后面（401 是契约的 unauthenticated 码）。
    async with AsyncClient(transport=ASGITransport(app=env.app),
                           base_url="http://test") as anon:
        assert anon.cookies.get("dshc_session") is None
        for path in ("/panel/", "/api/v1/auth/session", "/api/v1/meta/echo"):
            r = await anon.get(path)
            assert r.status_code == 401, path
            assert r.json()["detail"]["error"] == "unauthenticated"
        assert len(proxied) == 0


async def test_login_endpoint_shape_sanity(proxied, env):
    # 防呆：env 没登录成功的话，上面所有 200 都无意义。
    assert env.client.cookies.get("dshc_session") is not None
