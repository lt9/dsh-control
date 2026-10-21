"""与主线 Settings（pydantic-settings）的对接：provider 选择、凭据 env 兜底、dry_run。"""

from __future__ import annotations

from dsh_control.integrations.aliyun_dns import AliyunDns
from dsh_control.integrations.noop import NoopDns, NoopFrp, NoopNg
from dsh_control.integrations.registry import build_providers
from dsh_control.settings import Settings


def settings(**kw):
    return Settings(_env_file=None, **kw)


def test_site_provider_names_select_real_impls():
    s = settings(provider_dns="aliyun", provider_frp="noop", provider_ng="noop",
                 dns_zone="example.com", aliyun_ak="id", aliyun_sk="sec")
    p = build_providers(s, [])
    assert isinstance(p.dns, AliyunDns) and p.dns.zone == "example.com"
    assert isinstance(p.frp, NoopFrp) and isinstance(p.ng, NoopNg)
    assert p.dns.manual_required is not True
    assert p.frp.manual_required is True   # jobs 层按这个把步骤标 skipped


def test_settings_defaults_are_all_noop_and_runnable():
    p = build_providers(settings(), [])
    assert isinstance(p.dns, NoopDns) and isinstance(p.frp, NoopFrp) \
        and isinstance(p.frp, NoopFrp) and isinstance(p.ng, NoopNg)


def test_dry_run_bool_and_dns_prefix_flow_through():
    s = settings(provider_dns="aliyun", dns_zone="example.com",
                 aliyun_ak="id", aliyun_sk="sec", dns_prefix="dsh-", dry_run=True)
    p = build_providers(s, [])
    assert p.dry_run is True and p.exec.dry_run is True
    assert p.dns.rr_prefixes == ("dsh-", "dshctl")
