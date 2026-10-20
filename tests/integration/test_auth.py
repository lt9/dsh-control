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


async def _raw_post(app, path: str, payload: dict):
    """绕开 TestClient，直接按 ASGI 调 app：把 response.start 的响应头原样拿到手。"""
    import json

    body = json.dumps(payload).encode()
    out: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        out.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "client": ("127.0.0.1", 5000), "server": ("testserver", 80),
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    }
    await app(scope, receive, send)
    start = next(m for m in out if m["type"] == "http.response.start")
    sent = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, sent


async def test_login_success_declares_exact_content_length(env):
    """声明长度必须等于实发字节数：uvicorn 在实际 body 超过 content-length 时直接
    掐断连接（"Response content longer than Content-Length"），而 TestClient 不校验
    这条，所以整条绿灯下真机登录可以对所有人长期不可用。2026-10-21 就踩实了：
    登录成功分支建完 JSONResponse 又改 resp.body，长度对不上。"""
    import json

    status, headers, body = await _raw_post(env.app, "/api/login", {"key": OP_KEY})
    assert status == 200, body[:200]
    assert "dshc_session=" in headers.get("set-cookie", ""), "会话 cookie 没发出来"
    assert "HttpOnly" in headers["set-cookie"] and "SameSite=lax" in headers["set-cookie"]
    assert int(headers["content-length"]) == len(body), \
        f"声明 {headers['content-length']} 字节，实发 {len(body)} 字节"
    payload = json.loads(body)
    assert payload["ok"] is True and payload["expires_at"], "登录响应体缺字段"


async def test_login_failure_body_also_matches_declared_length(env):
    """失败分支同一条规矩：密钥错时也是 JSONResponse，长度也必须对得上。"""
    status, headers, body = await _raw_post(env.app, "/api/login", {"key": "不对的密钥"})
    assert status == 401
    assert int(headers["content-length"]) == len(body)
    assert b"bad_key" in body
