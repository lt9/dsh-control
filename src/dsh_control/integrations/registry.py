"""按名字装配 provider（docs/SITE-CONFIG.md）。

``build_providers(settings)`` 是唯一入口：业务层（jobs / watcher / api）只拿
``Providers`` 里的 ``DnsProvider / FrpProvider / NgProvider / Executor / DshRuntime /
PublicProbe`` Protocol，不 import 具体厂商。

``[providers]`` 的取值决定用哪个实现（``dns=aliyun|noop``、``frp=frp_panel|noop``、
``ng=nginxwebui|noop``；名字未注册直接报错，静默降级才危险）。凭据按 kind 从
``credentials`` 里取（``aliyun_ak`` / ``frp_panel`` / ``ng_panel``），取不到且没选
noop 时**自动落 noop 并留一条 ManualAction**——「没有阿里云也要能跑」。
新增厂商 = ``register_*`` 注册一行，不改调用方。

settings 的形状不钦定：属性对象或 dict 都行，按候选名逐个试，再退到环境变量
（DSHC_* 名字沿用 ARCHITECTURE §8）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .aliyun_dns import DEFAULT_RR_PREFIXES as DNS_RR_PREFIXES
from .aliyun_dns import AliyunDns
from .base import (
    DnsProvider,
    DshRuntime,
    Executor,
    FrpProvider,
    IntegrationError,
    NgProvider,
    PublicProbe,
)
from .dry_run import DryRunRecorder
from .frp_panel import FrpPanel
from .ng_panel import NgPanel
from .noop import ManualAction, NoopDns, NoopFrp, NoopNg
from .probe import HttpPublicProbe
from .shell import SshDshRuntime, SshExecutor

ProviderFactory = Callable[[Any, Mapping[str, Any], DryRunRecorder], Any]

_DNS_BUILDERS: dict[str, ProviderFactory] = {}
_FRP_BUILDERS: dict[str, ProviderFactory] = {}
_NG_BUILDERS: dict[str, ProviderFactory] = {}

DEFAULT_DNS_IMPL = "aliyun"
DEFAULT_FRP_IMPL = "frp_panel"
DEFAULT_NG_IMPL = "nginxwebui"


def register_dns(name: str, factory: ProviderFactory) -> None:
    _DNS_BUILDERS[name] = factory


def register_frp(name: str, factory: ProviderFactory) -> None:
    _FRP_BUILDERS[name] = factory


def register_ng(name: str, factory: ProviderFactory) -> None:
    _NG_BUILDERS[name] = factory


# ------------------------------------------------------------------ settings 取值

def _get(obj: Any, names: str | tuple[str, ...] | list[str], default: Any = None) -> Any:
    """在对象/dict 上按多个候选名找值；全落空再查同名环境变量。"""
    if isinstance(names, str):
        names = (names,)
    for name in names:
        if obj is not None:
            if isinstance(obj, Mapping):
                if name in obj and obj[name] not in (None, ""):
                    return obj[name]
                lowered = {str(k).lower(): v for k, v in obj.items()}
                if name.lower() in lowered and lowered[name.lower()] not in (None, ""):
                    return lowered[name.lower()]
                if "." in name:  # "providers.dns" → obj["providers"]["dns"]
                    head, rest = name.split(".", 1)
                    sub = obj.get(head) if head in obj else lowered.get(head.lower())
                    if isinstance(sub, Mapping):
                        hit = _get(sub, rest)
                        if hit is None and "." in rest:
                            hit = _get(sub, rest.rsplit(".", 1)[-1])
                        if hit is not None:
                            return hit
            else:
                for attr in (name, name.lower()):
                    value = getattr(obj, attr, None)
                    if value not in (None, ""):
                        return value
                providers = getattr(obj, "providers", None)
                if isinstance(providers, Mapping):
                    hit = _get(providers, name)
                    if hit is None and "." in name:
                        hit = _get(providers, name.rsplit(".", 1)[-1])
                    if hit is not None:
                        return hit
        env = os.environ.get(f"DSHC_{name.upper()}") or os.environ.get(name.upper())
        if env:
            return env
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (list, tuple)):
        out: dict[str, Any] = {}
        for item in value:
            row = _as_mapping(item)
            name = str(row.pop("name", "") or "")
            if name:
                out[name] = row
        return out
    obj = {k: v for k, v in vars(value).items() if not k.startswith("_")} \
        if hasattr(value, "__dict__") else {}
    return obj


class _CredStore:
    """凭据仓库视图：按 kind（可选 name）找 secret + base_url/zone。"""

    def __init__(self, credentials: Any) -> None:
        self._rows: list[dict[str, Any]] = []
        if credentials is None:
            return
        if isinstance(credentials, Mapping):
            for name, val in credentials.items():
                row = _as_mapping(val)
                row.setdefault("name", str(name))
                secret = _as_mapping(row.get("secret"))
                row = {**row, **{k: v for k, v in secret.items() if k not in row}}
                self._rows.append(row)
        elif isinstance(credentials, (list, tuple)):
            for item in credentials:
                row = _as_mapping(item)
                secret = _as_mapping(row.get("secret"))
                row = {**row, **{k: v for k, v in secret.items() if k not in row}}
                self._rows.append(row)
        else:  # 对象：按属性当 mapping 再走一遍
            self._rows = [self._unwrap(credentials)]

    @staticmethod
    def _unwrap(obj: Any) -> dict[str, Any]:
        row = {k: v for k, v in vars(obj).items() if not k.startswith("_")} \
            if hasattr(obj, "__dict__") else {}
        secret = _as_mapping(row.get("secret"))
        return {**row, **{k: v for k, v in secret.items() if k not in row}}

    def find(self, kind: str, name: str | None = None) -> dict[str, Any] | None:
        for row in self._rows:
            if str(row.get("kind", "")).lower() != kind:
                continue
            if name and str(row.get("name", "")) != name:
                continue
            return row
        return None


# ------------------------------------------------------------------ 各 provider 工厂

def _noop_note(recorder: DryRunRecorder, provider: str, why: str) -> ManualAction:
    action = ManualAction(provider, "provider_fallback",
                          f"{provider} 层落为 noop：{why}",
                          [("在 deploy/site.toml 的 [providers] 或凭据里补齐后自动恢复；"
                            "否则这一步按人工处理")])
    recorder.record(f"noop:{provider}", "provider_fallback", method="-",
                    target="(人工)", payload={"why": why},
                    note="缺实现或缺凭据，自动落 noop")
    return action


def _build_aliyun_dns(settings: Any, creds: _CredStore, recorder: DryRunRecorder):
    row = creds.find("aliyun_ak") or {}
    ak = str(row.get("access_key_id") or row.get("ak")
             or _get(settings, ("aliyun_ak", "access_key_id")) or "")
    sk = str(row.get("access_key_secret") or row.get("sk")
             or _get(settings, ("aliyun_sk", "access_key_secret")) or "")
    if not ak or not sk:
        _noop_note(recorder, "dns", "没有 kind=aliyun_ak 凭据（也没有 DSHC_ALIYUN_AK/_SK）")
        return NoopDns(zone=_get(settings, ("dns_zone", "zone"), "") or "", recorder=recorder)
    zone = (row.get("zone") or _get(settings, ("dns_zone", "zone"), "")) or ""
    if not zone:
        raise IntegrationError("DNS zone 未配置（site.dns_zone / DSHC_DNS_ZONE）",
                               code="no_config")
    prefixes = _get(settings, ("dns_rr_prefixes", "domain_prefix", "dns_prefix"))
    prefixes = tuple(prefixes) if isinstance(prefixes, (list, tuple)) else \
        ((prefixes,) + (DNS_RR_PREFIXES[1],)) if prefixes else DNS_RR_PREFIXES
    return AliyunDns(
        access_key_id=ak,
        access_key_secret=sk,
        zone=str(zone),
        dry_run=_as_bool(_get(settings, "dry_run")),
        recorder=recorder,
        rr_prefixes=prefixes,
    )


def _build_frp_panel(settings: Any, creds: _CredStore, recorder: DryRunRecorder):
    row = creds.find("frp_panel") or {}
    base_url = str(row.get("base_url")
                   or _get(settings, ("frp_panel_url", "frp_url")) or "")
    user = str(row.get("username") or _get(settings, "frp_panel_user") or "")
    pwd = str(row.get("password") or _get(settings, "frp_panel_pass") or "")
    if not base_url or not user:
        _noop_note(recorder, "frp", "缺 kind=frp_panel 凭据（base_url/username）")
        return NoopFrp(recorder=recorder)
    return FrpPanel(
        base_url=base_url,
        username=user,
        password=pwd,
        dry_run=_as_bool(_get(settings, "dry_run")),
        recorder=recorder,
    )


def _build_nginxwebui(settings: Any, creds: _CredStore, recorder: DryRunRecorder):
    row = creds.find("ng_panel") or {}
    base_url = str(row.get("base_url") or _get(settings, ("ng_url",)) or "")
    user = str(row.get("username") or _get(settings, "ng_user") or "")
    pwd = str(row.get("password") or _get(settings, "ng_pass") or "")
    if not base_url or not user:
        _noop_note(recorder, "ng", "缺 kind=ng_panel 凭据（base_url/username）")
        return NoopNg(recorder=recorder)
    return NgPanel(
        base_url=base_url,
        username=user,
        password=pwd,
        dry_run=_as_bool(_get(settings, "dry_run")),
        recorder=recorder,
    )


def _noop_dns(settings: Any, creds: _CredStore, recorder: DryRunRecorder):
    return NoopDns(zone=_get(settings, ("dns_zone", "zone"), "") or "", recorder=recorder)


def _noop_frp(settings: Any, creds: _CredStore, recorder: DryRunRecorder):
    return NoopFrp(recorder=recorder)


def _noop_ng(settings: Any, creds: _CredStore, recorder: DryRunRecorder):
    return NoopNg(recorder=recorder)


register_dns(DEFAULT_DNS_IMPL, _build_aliyun_dns)
register_dns("noop", _noop_dns)
register_frp(DEFAULT_FRP_IMPL, _build_frp_panel)
register_frp("noop", _noop_frp)
register_ng(DEFAULT_NG_IMPL, _build_nginxwebui)
register_ng("noop", _noop_ng)


# ------------------------------------------------------------------ Providers

class RuntimeBinder(DshRuntime):
    """DshRuntime 是「绑定一台主机」的视图；这里做工厂 + 分发。

    编排层每台主机调一次 ``bind()``（``bind_from_host`` 能吃 hosts 表行 +
    credentials 里的私钥）。直接调本对象的 inspect/stop 会报错，防止拿错主机。
    """

    manual_required = False

    def __init__(self, executor: Executor, *, dry_run: bool = False) -> None:
        self.executor = executor
        self.dry_run = dry_run
        self._bound: dict[tuple[str, int, str], SshDshRuntime] = {}

    def bind(self, *, host: str, user: str, port: int = 22,
             private_key_pem: str) -> SshDshRuntime:
        key = (host, port, user)
        if key not in self._bound:
            self._bound[key] = SshDshRuntime(
                self.executor, host=host, user=user, port=port,
                private_key_pem=private_key_pem, dry_run=self.dry_run,
            )
        return self._bound[key]

    def bind_from_host(self, host_row: Any, credentials: Any = None) -> SshDshRuntime:
        creds = credentials if isinstance(credentials, _CredStore) else _CredStore(credentials)
        pem = _get(host_row, ("private_key_pem", "ssh_private_key_pem"))
        if not pem:
            cred_name = _get(host_row, ("credential_name", "ssh_credential_name"))
            row = creds.find("ssh_key", cred_name) if cred_name else creds.find("ssh_key")
            pem = str((row or {}).get("private_key_pem") or "")
        if not pem:
            raise IntegrationError(
                f"主机 {_get(host_row, 'name', '?')} 没有可用的 ssh_key 凭据",
                code="no_ssh_key",
            )
        return self.bind(
            host=str(_get(host_row, ("ssh_host", "host"))),
            port=int(_get(host_row, ("ssh_port", "port"), 22) or 22),
            user=str(_get(host_row, ("ssh_user", "user"))),
            private_key_pem=str(pem),
        )

    # -- DshRuntime Protocol 透传：未绑定即拒绝 --------------------------------
    def _refuse(self) -> None:
        raise IntegrationError(
            "runtime 需要先 bind 一台被管主机（用 bind()/bind_from_host()）",
            code="runtime_unbound",
        )

    async def inspect(self, container: str):
        self._refuse()

    async def restart(self, container: str, line_sink=None):
        self._refuse()

    async def stop(self, container: str, line_sink=None):
        self._refuse()

    async def latest_token(self, container: str):
        self._refuse()

    async def version(self, container: str) -> str:
        self._refuse()
        return ""  # pragma: no cover


@dataclass(slots=True)
class Providers:
    dns: DnsProvider
    frp: FrpProvider
    ng: NgProvider
    exec: Executor
    runtime: RuntimeBinder
    probe: PublicProbe
    recorder: DryRunRecorder = field(default_factory=DryRunRecorder, repr=False)
    dry_run: bool = False

    async def aclose(self) -> None:
        for p in (self.dns, self.frp, self.ng, self.probe):
            close = getattr(p, "aclose", None)
            if close is not None:
                await close()


def _pick(
    settings: Any, key: str, table: dict[str, ProviderFactory], default_name: str,
    settings_key: str,
) -> str:
    name = _get(settings, (f"provider_{key}", f"providers.{key}", settings_key))
    if isinstance(name, str) and name.strip():
        return name.strip().lower()
    return default_name


def build_providers(
    settings: Any,
    credentials: Any = None,
    *,
    recorder: DryRunRecorder | None = None,
) -> Providers:
    """装配一套 provider。settings 兼容 pydantic 对象 / dict / 环境变量。"""
    recorder = recorder or DryRunRecorder()
    creds = _CredStore(credentials)
    dry_run = _as_bool(_get(settings, "dry_run"))

    def build(key: str, table: dict[str, ProviderFactory], default_name: str,
              settings_key: str):
        name = _pick(settings, key, table, default_name, settings_key)
        if name == "auto":
            kind = {"dns": "aliyun_ak", "frp": "frp_panel", "ng": "ng_panel"}[key]
            if creds.find(kind):
                name = default_name
            else:
                name = "noop"
                _noop_note(recorder, key, "providers=auto 但缺对应凭据")
        factory = table.get(name)
        if factory is None:
            raise IntegrationError(
                f"[providers] {key}={name!r} 未注册；可用：{sorted(table)}",
                code="unknown_provider",
            )
        return factory(settings, creds, recorder)

    executor = SshExecutor(dry_run=dry_run, recorder=recorder)
    return Providers(
        dns=build("dns", _DNS_BUILDERS, DEFAULT_DNS_IMPL, "dns_provider"),
        frp=build("frp", _FRP_BUILDERS, DEFAULT_FRP_IMPL, "frp_provider"),
        ng=build("ng", _NG_BUILDERS, DEFAULT_NG_IMPL, "ng_provider"),
        exec=executor,
        runtime=RuntimeBinder(executor, dry_run=dry_run),
        probe=HttpPublicProbe(),
        recorder=recorder,
        dry_run=dry_run,
    )
