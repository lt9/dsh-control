"""来源 IP 判定：公网入口挂上之后，这一层就是限速与审计的地基。

两个方向都错过：取 XFF 最左段等于让客户端自己填身份；完全不看转发头又让
所有真人挤在代理那一台 IP 上，一个人撞库全站被关。
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from dsh_control.api.auth import client_ip, is_https


def req(client: str | None, headers: dict[str, str] | None = None,
        scheme: str = "http") -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (client, 51234) if client else None,
        "scheme": scheme,
        "method": "POST",
        "path": "/api/login",
        "query_string": b"",
        "root_path": "",
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_leftmost_xff_is_not_the_identity():
    """最左段是客户端自己写的，不能当身份。"""
    r = req("203.0.113.9", {"x-forwarded-for": "192.0.2.8, 203.0.113.9"})
    assert client_ip(r) == "203.0.113.9"


def test_forged_xff_from_outside_cannot_choose_bucket():
    """外部来源送来的转发头一概不信，只认 TCP 对端。"""
    a = req("198.51.100.7", {"x-forwarded-for": "192.0.2.8", "x-real-ip": "198.51.100.66"})
    b = req("198.51.100.7", {"x-forwarded-for": "192.0.2.99"})
    assert client_ip(a) == client_ip(b) == "198.51.100.7"


def test_x_real_ip_wins_behind_trusted_proxy():
    r = req("192.168.3.109", {"x-real-ip": "203.0.113.5",
                              "x-forwarded-for": "192.0.2.9, 203.0.113.5"})
    assert client_ip(r) == "203.0.113.5"


def test_rightmost_xff_when_no_real_ip():
    r = req("172.19.4.1", {"x-forwarded-for": "192.0.2.8, 203.0.113.6"})
    assert client_ip(r) == "203.0.113.6"


def test_no_peer_is_unknown():
    assert client_ip(req(None, {})) == "unknown"


def test_https_only_from_trusted_proxy_report():
    assert is_https(req("192.168.3.109", {"x-forwarded-proto": "https"})) is True
    assert is_https(req("192.168.3.109", {"x-forwarded-proto": "http"})) is False
    # 外部来源自称 https 不算数
    assert is_https(req("198.51.100.7", {"x-forwarded-proto": "https"})) is False
    assert is_https(req("198.51.100.7", {}, scheme="https")) is True


def test_non_ip_peer_is_handled():
    """TestClient 的对端叫 'testclient'，不是 IP——不能把解析异常抛到线上去。"""
    r = req("testclient", {"x-forwarded-for": "1.2.3.4"})
    assert client_ip(r) == "testclient"
    assert is_https(r) is False


@pytest.mark.parametrize("bad", ["", "abc", "999.1.1.1"])
def test_garbage_peer_never_crashes(bad: str):
    assert client_ip(req(bad or None, {"x-real-ip": "192.0.2.44"})) in (bad, "unknown")
