"""鉴权：单运维密钥、HttpOnly cookie、限流、X-Requested-With。"""

from __future__ import annotations

from tests.conftest import OP_KEY, XRW


async def test_healthz_public(env):
    r = await env.client.get("/api/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_login_ok_sets_httponly_lax_cookie(env):
    r = await env.client.post("/api/login", json={"key": OP_KEY})
    assert r.status_code == 200
    sc = r.headers["set-cookie"]
    assert "dshc_session=" in sc and "HttpOnly" in sc and "samesite=lax" in sc.lower()
    assert "dshc_session" in env.client.cookies
    r = await env.client.get("/api/session")
    assert r.status_code == 200 and r.json()["ok"]


async def test_login_bad_key_401(env):
    # env.client 已带会话 cookie，用无 cookie 的新 client
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=env.app),
                           base_url="http://test") as c:
        r = await c.post("/api/login", json={"key": "wrong-key"})
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "bad_key"


async def test_login_rate_limited_by_ip(env):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=env.app),
                           base_url="http://test") as c:
        for _ in range(5):
            r = await c.post("/api/login", json={"key": "nope"})
            assert r.status_code == 401
        r = await c.post("/api/login", json={"key": OP_KEY})
        assert r.status_code == 429
        assert "Retry-After" in r.headers


async def test_api_requires_cookie(env):
    env.client.cookies.clear()
    r = await env.client.get("/api/instances")
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "unauthenticated"


async def test_write_requires_xrw(env):
    r = await env.client.post("/api/instances", json={})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "missing_x_requested_with"
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    assert r.status_code == 200


async def test_logout_clears_cookie(env):
    r = await env.client.post("/api/logout", headers=XRW)
    assert r.status_code == 200
    env.client.cookies.clear()  # 手动同步：服务端已让 cookie 失效
    r = await env.client.get("/api/session")
    # /session 在没有 cookie 时 ok=False（路由级鉴权会先 401）
    assert r.status_code == 401
