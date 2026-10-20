"""cli expose：主控台自身公网入口的三处外部对象（假 provider 全链路）。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from dsh_control.console import ENDPOINT_CRED_NAME, run_expose
from dsh_control.db.models import Credential
from dsh_control.integrations.base import ProbeResult


async def collector():
    lines: list[tuple[str, str]] = []

    async def log(level: str, msg: str) -> None:
        lines.append((level, msg))
    return lines, log


def _idx(journal, prefix):
    for i, ln in enumerate(journal):
        if ln.startswith(prefix):
            return i
    return -1


async def test_expose_order_and_registration(env):
    env.settings.console_host = "nas-1"
    lines, log = await collector()
    summary = await run_expose(env.db, env.providers, env.settings, log=log)

    j = env.journal
    i_dns, i_frp, i_ng, i_probe = (_idx(j, "dns.create"), _idx(j, "frp.ensure"),
                                   _idx(j, "ng.ensure"), _idx(j, "probe"))
    assert -1 < i_dns < i_frp < i_ng < i_probe, j  # dns→frp→ng→探活
    blob = "\n".join(j)
    # DNS：入口主机名在 zone 下的一级 rr → ingress_ip
    assert "dns.create console 203.0.113.10" in blob
    # frp：固定隧道名、自己主机的 client、本机 ssh_host:listen_port
    assert "frp.ensure dshctl_console remote=4893 local=192.0.2.10" in blob
    # NG：upstream 必须用 frps_upstream_host（198.51.100.9），不是 ingress_ip；
    # headerHost=$host 由 FakeNg 的断言把守；建完必须 reload（要点 1）
    assert "ng.ensure console.example.test DSHCTL_CONSOLE 198.51.100.9:4893" in blob
    assert "ng.reload" in blob
    assert "203.0.113.10:4893" not in blob
    assert "probe https://console.example.test" in blob
    assert summary == {**summary, "dns": "created", "frp": "created",
                       "ng": "created", "probe": "ok", "frp_remote_port": 4893}

    # 幂等登记：id 存进 credentials(kind=site, name=console-endpoint)
    async with env.db.session() as s:
        cred = await s.scalar(select(Credential).where(
            Credential.name == ENDPOINT_CRED_NAME))
        assert cred.secret == {"dns_record_id": "rec-1", "frp_proxy_id": 1,
                               "frp_remote_port": 4893, "ng_server_id": 1,
                               "ng_location_id": 10,
                               "ng_upstream_name": "DSHCTL_CONSOLE"}
    # 收尾必须提醒人工放行安全组（API 管不了）
    assert any("安全组" in m and "4893" in m for _l, m in lines)


async def test_expose_idempotent_second_run_zero_writes(env):
    env.settings.console_host = "nas-1"
    _l, log = await collector()
    await run_expose(env.db, env.providers, env.settings, log=log)
    writes_before = [ln for ln in env.journal
                     if ln.startswith(("dns.create", "frp.ensure", "ng.ensure",
                                       "ng.reload"))]
    lines, log2 = await collector()
    await run_expose(env.db, env.providers, env.settings, log=log2)
    writes_after = [ln for ln in env.journal
                    if ln.startswith(("dns.create", "frp.ensure", "ng.ensure",
                                      "ng.reload"))]
    assert writes_after == writes_before  # 第二次零写请求
    assert any("已存在" in m for _l, m in lines)          # 打印现状
    assert not any(ln.startswith(("dns.delete", "frp.delete", "ng.delete"))
                   for ln in env.journal)                # 绝不删除


async def test_expose_requires_frps_upstream_host(env):
    env.settings.console_host = "nas-1"
    env.settings.frps_upstream_host = ""
    _l, log = await collector()
    with pytest.raises(RuntimeError, match="DSHC_FRPS_UPSTREAM_HOST"):
        await run_expose(env.db, env.providers, env.settings, log=log)
    assert env.journal == []  # 拒绝执行：一个请求都不发


async def test_expose_ambiguous_console_host(env):
    # env fixture 有两台 enabled 主机且没设 console_host：必须拒绝而不是猜
    _l, log = await collector()
    with pytest.raises(RuntimeError, match="DSHC_CONSOLE_HOST"):
        await run_expose(env.db, env.providers, env.settings, log=log)


async def test_expose_probe_mismatch_prints_headerhost_hint(env):
    env.settings.console_host = "nas-1"
    env.providers.probe.result = ProbeResult(
        False, 403, "host_origin_mismatch", "mux 403")
    lines, log = await collector()
    summary = await run_expose(env.db, env.providers, env.settings, log=log)
    assert summary["probe"] == "host_origin_mismatch"
    assert any("headerHost" in m for _l, m in lines)


async def test_expose_dry_run_writes_nothing_to_db(env):
    env.settings.console_host = "nas-1"
    _l, log = await collector()
    summary = await run_expose(env.db, env.providers, env.settings,
                               dry_run=True, log=log)
    assert summary["dns"] == "dry-run"
    # dry-run 绝不把（合成）id 写进自己的库
    async with env.db.session() as s:
        assert await s.scalar(select(Credential).where(
            Credential.name == ENDPOINT_CRED_NAME)) is None
