"""probe.py：401=ok / 403=mismatch+提示 / 连不上=unreachable；Origin 归一化。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from dsh_control.integrations.probe import HttpPublicProbe, browser_origin

BASE = "https://dsh-a.example.com"


def run(coro):
    return asyncio.run(coro)


def probe_for(home_status=200, mux_status=401, fail: str | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if fail == "home" and request.url.path == "/":
            raise httpx.ConnectError("connection refused")
        if fail == "mux" and request.url.path.endswith("remote.mux"):
            raise httpx.ConnectError("connection reset")
        if request.url.path == "/":
            return httpx.Response(home_status, text="<html>dsh</html>")
        return httpx.Response(mux_status, text="denied")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpPublicProbe(client=client), seen


def test_ok_when_home_200_and_mux_401():
    probe, seen = probe_for()
    res = run(probe.check(BASE))
    assert (res.ok, res.verdict, res.status_code) == (True, "ok", 401)
    mux = seen[-1]
    assert mux.url.path == "/api/remote.mux"
    assert mux.headers["origin"] == "https://dsh-a.example.com"
    assert "token" not in str(mux.url)  # 探测绝不带 token


def test_origin_normalized_default_ports_stripped():
    assert browser_origin("https://x.example.com:443/foo") == "https://x.example.com"
    assert browser_origin("http://x.example.com:80/") == "http://x.example.com"
    assert browser_origin("https://x.example.com:18443") == "https://x.example.com:18443"


def test_403_is_host_origin_mismatch_with_fix_hint():
    probe, _ = probe_for(mux_status=403)
    res = run(probe.check(BASE))
    assert (res.ok, res.verdict, res.status_code) == (False, "host_origin_mismatch", 403)
    assert "$host" in res.hint and "$proxy_port" in res.hint  # 修正提示要能照着做


def test_unreachable_home():
    probe, _ = probe_for(fail="home")
    res = run(probe.check(BASE))
    assert res.verdict == "unreachable" and not res.ok


def test_unreachable_mux_only():
    probe, _ = probe_for(fail="mux")
    res = run(probe.check(BASE))
    assert res.verdict == "unreachable"


def test_unexpected_statuses():
    probe, _ = probe_for(home_status=502, mux_status=401)
    res = run(probe.check(BASE))
    assert res.verdict == "unexpected" and not res.ok


def test_unprotected_mux_is_dangerous_not_unexpected():
    """没带 token 却能打通受保护接口 = 准入墙没生效，不能只当"回包奇怪"。"""
    probe, _ = probe_for(mux_status=200)
    res = run(probe.check(BASE))
    assert (res.ok, res.verdict) == (False, "auth_open"), res
    assert "准入墙" in res.hint


def test_home_401_counts_as_alive():
    """DSH 开着准入墙时首页本来就 401。现网两次开通的最后一步都被判成
    unexpected（工单写着 status=401 verdict=unexpected，事后公网实测正常），
    标准错了就会天天假警报。"""
    probe, _ = probe_for(home_status=401, mux_status=401)
    res = run(probe.check(BASE))
    assert (res.ok, res.verdict) == (True, "protected"), res
    assert "一键地址" in res.hint


def test_home_403_is_host_mismatch():
    probe, _ = probe_for(home_status=403, mux_status=401)
    res = run(probe.check(BASE))
    assert (res.ok, res.verdict) == (False, "host_origin_mismatch"), res


def test_bad_base_url_rejected():
    probe, _ = probe_for()
    with pytest.raises(ValueError):
        run(probe.check("not-a-url"))
