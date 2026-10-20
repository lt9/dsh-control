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
import ipaddress
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


# 只信这些来源送来的转发头。我们的拓扑里请求要么从内网直连进来，要么经
# frps + nginxWebUI 转发进来，两条路落的对端地址都在这些段里。
TRUSTED_PROXY_NETS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12",
                      "192.168.0.0/16", "fc00::/7")


def peer_ip(request: Request) -> str:
    """TCP 层看到的对端地址（不受任何请求头影响）。"""
    return request.client.host if request.client else ""


def _from_trusted_proxy(request: Request) -> bool:
    ip = peer_ip(request)
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in TRUSTED_PROXY_NETS:
        if addr in ipaddress.ip_network(cidr):
            return True
    return False


def forwarded_for(request: Request) -> str:
    """真实来源 IP，用来做限速与审计。

    这里踩过两个方向的坑，都记着：

    1. 取 ``X-Forwarded-For`` 最左段 = 取客户端自己写上去的那一段。公网入口
       挂上之后，登录限速（5 次/分钟）想绕就绕——每发一次换一个假 XFF 就行，
       审计日志里的 IP 也全是编的。
    2. 反过来完全不看转发头也不对：穿两层反代后所有人都是代理那一台 IP，
       限速就变成"一个人撞库，全站一起被关在门外"。

    口径：只对可信反代送来的头认真；优先 ``X-Real-IP``（nginxWebUI 用
    ``$remote_addr`` 覆写，客户端伪造不了），否则取 XFF **最右段**
    （``$proxy_add_x_forwarded_for`` 追加的那一段才是代理亲眼看到的对端）。
    """
    if not _from_trusted_proxy(request):
        return peer_ip(request) or "unknown"
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return [x for x in fwd.split(",") if x.strip()][-1].strip()
    return peer_ip(request) or "unknown"


# 兼容旧调用点
client_ip = forwarded_for


def is_https(request: Request) -> bool:
    """这次请求是不是走 TLS 进来的。只有可信反代报的 ``X-Forwarded-Proto`` 才算数。"""
    if _from_trusted_proxy(request):
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: JSONResponse, secret: str, days: int,
                       *, expires_at: datetime | None = None,
                       secure: bool = False) -> datetime:
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
        # 内网直连 http 也要能用，所以不能一律 Secure；但走 https 进来的那次
        # 必须带上——否则这枚能开全部运维权限的 cookie 有机会跟着明文请求跑掉。
        secure=secure,
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
