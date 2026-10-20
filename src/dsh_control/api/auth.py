"""单运维密钥鉴权。契约 §4：

- 库里 credentials(kind=operator_key) 存明文（用户要求可读回）；比对走常数时间。
- 登录签发签名 cookie dshc_session：HttpOnly, SameSite=Lax, 7 天。
- 写操作要求头 X-Requested-With: 1（配合 SameSite=Lax 抑制 CSRF）。
- 登录失败按 IP 限速 5 次/分钟。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

from ..settings import get_settings

COOKIE_NAME = "dshc_session"
XRW_HEADER = "X-Requested-With"
XRW_VALUE = "1"

LOGIN_LIMIT = 5            # 次
LOGIN_WINDOW_SECONDS = 60  # 按 IP 每分钟


def _sign(secret: str, payload_b64: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(secret: str, expires_at: datetime) -> str:
    payload = {"exp": int(expires_at.timestamp()), "n": secrets.token_hex(8)}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"v1.{body}.{_sign(secret, body)}"


def verify_session_cookie(secret: str, value: str) -> datetime | None:
    """返回过期时间；无效返回 None。常数时间比较签名。"""
    try:
        version, body, sig = value.split(".", 2)
    except ValueError:
        return None
    if version != "v1":
        return None
    expect = _sign(secret, body)
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = datetime.fromtimestamp(int(payload["exp"]), UTC)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if datetime.now(UTC) >= exp:
        return None
    return exp


def constant_time_key_match(candidate: str, stored: str) -> bool:
    """比对用常数时间；stored 是库里的明文（用户要求可读回，不哈希）。"""
    a = candidate.encode("utf-8", "replace")
    b = stored.encode("utf-8", "replace")
    # 先等长填充，避免长度差异成为侧信道里更便宜的早退信号
    pad = max(len(a), len(b))
    return hmac.compare_digest(a.ljust(pad, b"\x00"), b.ljust(pad, b"\x00"))


class LoginRateLimiter:
    """进程内按 IP 的滑动窗口，只统计失败次数。单容器单进程，够用。"""

    def __init__(self, limit: int = LOGIN_LIMIT, window: int = LOGIN_WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def is_limited(self, ip: str) -> bool:
        cutoff = time.monotonic() - self.window
        q = self._failures[ip]
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) >= self.limit

    def record_failure(self, ip: str) -> None:
        self._failures[ip].append(time.monotonic())

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def retry_after(self, ip: str) -> int:
        q = self._failures.get(ip)
        if not q:
            return LOGIN_WINDOW_SECONDS
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return max(1, int(self.window - (time.monotonic() - q[0])))


@dataclass
class AuthState:
    limiter: LoginRateLimiter = field(default_factory=LoginRateLimiter)


EXEMPT_PATHS = {"/api/login", "/api/healthz"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def set_session_cookie(response: JSONResponse, secret: str, days: int,
                       *, expires_at: datetime | None = None) -> datetime:
    """给响应挂会话 cookie。

    ``expires_at`` 可由调用方传入：登录响应体里也要回这个时间，两边必须是同一个值，
    不能让调用方算一次、这里再算一次（差一秒就是两个不同的过期时间）。
    """
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(days=days)
    response.set_cookie(
        COOKIE_NAME,
        make_session_cookie(secret, expires_at),
        max_age=days * 86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,  # 内网直连 http 也要能用；公网入口 https 由 frps/NG 终结
    )
    return expires_at


def clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def session_from_request(request, secret: str | None = None) -> datetime | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    if secret is None:
        secret = getattr(getattr(request.app, "state", None), "settings",
                         None) and request.app.state.settings.session_secret
    if not secret:
        secret = get_settings().session_secret
    if not secret:
        return None
    return verify_session_cookie(secret, raw)
