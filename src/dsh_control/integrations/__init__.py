"""外部系统适配器包（ARCHITECTURE §5）。

业务层只 import 本包的 base Protocol 与 registry.build_providers；具体厂商实现
通过 site.toml 的 ``[providers]`` 名字选择，可整体换掉或落 noop。

任何站点专属事实（域名、入口 IP、面板地址、id）都不许出现在这个包里——
全部走构造参数（来源：deploy/site.toml 与环境变量）。
"""

from .aliyun_dns import AliyunDns
from .base import (
    ContainerState,
    DnsProvider,
    DnsRecord,
    DshRuntime,
    ExecResult,
    Executor,
    FrpProvider,
    IntegrationError,
    LineSink,
    NgProvider,
    NgSite,
    NotManaged,
    ProbeResult,
    ProviderMode,
    PublicProbe,
    Tunnel,
    redact,
)
from .dry_run import DryRunEntry, DryRunRecorder
from .frp_panel import FrpPanel
from .ng_panel import NgPanel
from .noop import ManualAction, NoopDns, NoopFrp, NoopNg
from .probe import HttpPublicProbe
from .registry import (
    Providers,
    RuntimeBinder,
    build_providers,
    register_dns,
    register_frp,
    register_ng,
)
from .shell import SshDshRuntime, SshExecutor

__all__ = [
    "AliyunDns",
    "ContainerState",
    "DnsProvider",
    "DnsRecord",
    "DryRunEntry",
    "DryRunRecorder",
    "DshRuntime",
    "ExecResult",
    "Executor",
    "FrpPanel",
    "FrpProvider",
    "HttpPublicProbe",
    "IntegrationError",
    "LineSink",
    "ManualAction",
    "NgPanel",
    "NgProvider",
    "NgSite",
    "NoopDns",
    "NoopFrp",
    "NoopNg",
    "NotManaged",
    "ProbeResult",
    "ProviderMode",
    "Providers",
    "PublicProbe",
    "RuntimeBinder",
    "SshDshRuntime",
    "SshExecutor",
    "Tunnel",
    "build_providers",
    "redact",
    "register_dns",
    "register_frp",
    "register_ng",
]
