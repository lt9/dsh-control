"""noop provider 与 registry：换名即换实现、缺凭据自动落 noop、runtime 绑定。"""

from __future__ import annotations

import asyncio

import pytest

from dsh_control.integrations.aliyun_dns import AliyunDns
from dsh_control.integrations.base import IntegrationError
from dsh_control.integrations.dry_run import DryRunRecorder
from dsh_control.integrations.frp_panel import FrpPanel
from dsh_control.integrations.ng_panel import NgPanel
from dsh_control.integrations.noop import NoopDns, NoopFrp, NoopNg
from dsh_control.integrations.registry import (
    Providers,
    RuntimeBinder,
    build_providers,
    register_dns,
)
from dsh_control.integrations.shell import SshDshRuntime, SshExecutor


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ noop

def test_noop_dns_returns_manual_marker_and_records():
    rec = DryRunRecorder()
    dns = NoopDns(zone="example.com", recorder=rec)
    out = run(dns.create("dsh-x", "203.0.113.10"))
    assert out.record_id == "manual" and out.status == "manual"
    run(dns.delete("whatever"))
    actions = dns.drain_manual_actions()
    assert [a.step for a in actions] == ["dns_create", "dns_delete"]
    assert "人工" in actions[0].summary
    assert dns.drain_manual_actions() == []          # 取走即清空
    assert len(rec.entries) == 2


def test_noop_frp_and_ng_synthesize_clearly_fake_ids():
    frp, ng = NoopFrp(), NoopNg()
    t = run(frp.ensure(name="dsh_a", client_id="c@1", server_id="default",
                       local_ip="127.0.0.1", local_port=1, remote_port=4893))
    assert t.proxy_id == 0 and t.raw["manual"] is True
    site = run(ng.ensure_site(server_name="dsh-a.example.com", upstream_name="DSH_A",
                              upstream_server="127.0.0.1:1"))
    assert site.server_id == 0
    assert run(frp.list()) == [] and run(ng.find_site("x")) is None
    assert run(frp.remote_port_taken(4893)) is False  # 探测不了，宁可放行也不误报占用
    assert any("websocket" in i for i in ng.manual_actions[0].instructions)


def test_noop_dns_zone_visible_in_manual_step():
    dns = NoopDns(zone="example.com")
    run(dns.create("dsh-x", "203.0.113.10"))
    assert "dsh-x.example.com" in dns.manual_actions[0].summary


# ------------------------------------------------------------------ registry

SETTINGS_FULL = {
    "providers": {"dns": "aliyun", "frp": "frp_panel", "ng": "nginxwebui"},
    "dns_zone": "example.com",
    "dry_run": "1",
}
CREDS_FULL = [
    {"kind": "aliyun_ak", "name": "site-dns",
     "secret": {"access_key_id": "id", "access_key_secret": "sec", "zone": "example.com"}},
    {"kind": "frp_panel", "name": "panel", "base_url": "http://203.0.113.10:8500",
     "secret": {"username": "u", "password": "p"}},
    {"kind": "ng_panel", "name": "panel", "base_url": "http://203.0.113.10:8600",
     "secret": {"username": "u", "password": "p"}},
    {"kind": "ssh_key", "name": "console", "secret": {"private_key_pem": "PEM"}},
]


def test_build_all_live_providers():
    p = build_providers(SETTINGS_FULL, CREDS_FULL)
    assert isinstance(p.dns, AliyunDns) and p.dns.zone == "example.com"
    assert isinstance(p.frp, FrpPanel)
    assert isinstance(p.ng, NgPanel)
    assert isinstance(p.exec, SshExecutor) and p.exec.dry_run is True
    assert p.dry_run is True
    assert p.runtime is not None


def test_build_all_noop_without_any_credentials():
    settings = {"providers": {"dns": "noop", "frp": "noop", "ng": "noop"}}
    p = build_providers(settings, [])
    assert isinstance(p.dns, NoopDns) and isinstance(p.frp, NoopFrp) \
        and isinstance(p.ng, NoopNg)
    # 「什么都没配也要能跑起来」
    assert run(p.dns.find("dsh-a")) is None
    assert run(p.frp.list()) == []


def test_missing_credentials_fall_back_to_noop_with_note():
    p = build_providers({"providers": {"dns": "auto", "frp": "auto", "ng": "auto"},
                         "dns_zone": "example.com"}, [])
    assert isinstance(p.dns, NoopDns) and isinstance(p.frp, NoopFrp)
    fallbacks = [e for e in p.recorder.entries if e.action == "provider_fallback"]
    assert len(fallbacks) == 3


def test_unknown_provider_name_fails_loud():
    with pytest.raises(IntegrationError) as ei:
        build_providers({"providers": {"dns": "aws_route53", "frp": "noop", "ng": "noop"}}, [])
    assert ei.value.code == "unknown_provider"
    assert "aws_route53" in str(ei.value)


def test_registry_extensible_by_name():
    register_dns("test_noop_alias", lambda s, c, r: NoopDns(zone="alias.example", recorder=r))
    try:
        p = build_providers({"providers": {"dns": "test_noop_alias", "frp": "noop",
                                           "ng": "noop"}}, [])
        assert p.dns.zone == "alias.example"
    finally:
        from dsh_control.integrations import registry
        registry._DNS_BUILDERS.pop("test_noop_alias", None)


def test_env_fallback_for_credentials(monkeypatch):
    monkeypatch.setenv("DSHC_ALIYUN_AK", "id")
    monkeypatch.setenv("DSHC_ALIYUN_SK", "sec")
    monkeypatch.setenv("DSHC_DNS_ZONE", "example.com")
    p = build_providers({"providers": {"dns": "aliyun", "frp": "noop", "ng": "noop"}}, None)
    assert isinstance(p.dns, AliyunDns) and p.dns.zone == "example.com"


# ------------------------------------------------------------------ runtime 绑定

def test_runtime_binder_requires_bind():
    p = build_providers({"providers": {"dns": "noop", "frp": "noop", "ng": "noop"}}, [])
    with pytest.raises(IntegrationError):
        run(p.runtime.inspect("whatever"))


def test_runtime_bind_from_host_row_and_pem_credential():
    binder = RuntimeBinder(SshExecutor())
    rt = binder.bind_from_host(
        {"name": "nas-1", "ssh_host": "192.0.2.10", "ssh_port": 2333,
         "ssh_user": "ops", "credential_name": "console"},
        CREDS_FULL,
    )
    assert isinstance(rt, SshDshRuntime)
    assert (rt.host, rt.port, rt.user) == ("192.0.2.10", 2333, "ops")
    # 同一主机复用同一绑定
    assert binder.bind_from_host({"ssh_host": "192.0.2.10", "ssh_port": 2333,
                                  "ssh_user": "ops"}, CREDS_FULL) is rt


def test_runtime_bind_missing_key_raises():
    binder = RuntimeBinder(SshExecutor())
    with pytest.raises(IntegrationError):
        binder.bind_from_host({"ssh_host": "192.0.2.10", "ssh_user": "ops"}, [])


def test_providers_shape_stable():
    fields = set(Providers.__dataclass_fields__)
    assert {"dns", "frp", "ng", "exec", "runtime", "probe"} <= fields
