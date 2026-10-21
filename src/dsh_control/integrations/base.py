"""外部系统适配器的冻结接口。

后端编排只依赖这个文件里的类型，不直接 import 具体实现。三个实现
（frp_panel / aliyun_dns / ng_panel）与 SSH 执行器都实现这里的 Protocol，
方法签名以本文件为准；要改签名先改 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

LineSink = Callable[[str, str], None]  # (level: stdout|stderr, line)


class IntegrationError(RuntimeError):
    """外部系统返回非成功，或触发了 managed_only 白名单保护。"""

    def __init__(self, message: str, *, code: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class NotManaged(IntegrationError):
    """试图操作不属于本控制台创建的对象。必须抛这个，调用方据此中止整条流程。"""


@dataclass(slots=True)
class DnsRecord:
    record_id: str
    rr: str
    type: str
    value: str
    status: str = ""


@runtime_checkable
class DnsProvider(Protocol):
    zone: str

    async def find(self, rr: str) -> DnsRecord | None: ...
    async def create(self, rr: str, value: str, ttl: int = 600) -> DnsRecord: ...
    async def delete(self, record_id: str) -> None:
        """record_id 必须来自本控制台登记过的记录，否则抛 NotManaged。"""
        ...


@dataclass(slots=True)
class Tunnel:
    proxy_id: int
    name: str
    client_id: str
    server_id: str
    type: str = "tcp"
    local_ip: str = "127.0.0.1"
    local_port: int = 0
    remote_port: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class FrpProvider(Protocol):
    async def list(self, client_id: str | None = None) -> list[Tunnel]: ...
    async def get(self, proxy_id: int) -> Tunnel | None: ...
    async def find_by_name(self, name: str, client_id: str) -> Tunnel | None: ...
    async def ensure(self, *, name: str, client_id: str, server_id: str, local_ip: str,
                     local_port: int, remote_port: int) -> Tunnel:
        """幂等：同名同客户端存在就核对端口，不一致抛 IntegrationError（不许静默改别人的门）。"""
        ...
    async def delete(self, proxy_id: int, *, expect_name: str) -> None:
        """expect_name 必须以 dshctl_ 或 dsh_ 开头且与读回的 name 相等，否则抛 NotManaged。"""
        ...
    async def remote_port_taken(self, port: int) -> bool:
        """在 frps 所在主机上探测该公网端口是否已被占用或已被某条隧道声明。"""
        ...


@dataclass(slots=True)
class NgSite:
    server_id: int
    server_name: str
    upstream_id: int | None = None
    location_id: int | None = None
    header_host: str = "$host"
    websocket: bool = True


@runtime_checkable
class NgProvider(Protocol):
    async def find_site(self, server_name: str) -> NgSite | None: ...
    async def ensure_site(self, *, server_name: str, upstream_name: str,
                          upstream_server: str, listen_port: int = 443,
                          cert_name: str | None = None,
                          header_host: str = "$host",
                          websocket: bool = True) -> NgSite:
        """幂等创建 upstream + 反代站点。

        header_host 必须是 "$host"，不能带端口：DSH 要求 Origin 的 authority 与 Host
        完全相等，带端口会 403 掉 WebSocket（见 ARCHITECTURE §5.4）。
        """
        ...
    async def delete_site(self, server_id: int, *, expect_name: str) -> None: ...
    async def reload(self) -> None: ...


@dataclass(slots=True)
class ExecResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, n: int = 800) -> str:
        return (self.stderr or self.stdout).strip()[-n:]


@runtime_checkable
class Executor(Protocol):
    async def run(self, *, host: str, port: int, user: str, private_key_pem: str,
                  command: str, timeout: float = 300,
                  line_sink: LineSink | None = None,
                  stdin: str | None = None) -> ExecResult:
        """stdin 只用来递密钥：命令行会进日志（打码只认 token/secret/password
        这类名字，认不出 LLM_API_KEY），在目标机上 `ps` 也看得见。"""
        ...


@dataclass(slots=True)
class ContainerState:
    name: str
    state: str
    health: Literal["healthy", "unhealthy", "starting", "none"]
    image: str
    image_id: str
    restart_count: int
    ip: str
    cpu_pct: float | None = None
    mem_usage_bytes: int | None = None
    mem_limit_bytes: int | None = None
    failures: int = 0


# 占位符用 __NAME__，**不要用 str.format**：这段里全是 Go 模板的 {{…}}，
# `.format()` 会把 `{{` 收成 `{`，docker 拿到 `{.State.Status}` 这种残缺模板
# 不报错，直接把原文打出来（2026-10-21 现网：镜像、IP、状态在页面上全是
# `{.Config.Image}` 这类模板原文，好端端跑着的实例因此被误判成 error）。
DOCKER_INSPECT_SNIPPET = """
docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|\\\
{{.Config.Image}}|{{.Image}}|{{.RestartCount}}|{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \\
  __NAME__ 2>/dev/null || echo absent
"""


DOCKER_STATS_SNIPPET = """
docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}' \
  __NAME__ 2>/dev/null || echo absent
"""

# 一次调用取回"她能用些什么"的三件事：技能目录、profile 组合包、home 层补丁。
# 分段落用 @@标记@@ 隔开，是因为这几样东西分散在不同路径，拼成 JSON 反而要在
# 容器里装 jq；行扫描在我们这边做，容器侧只跑 POSIX sh。
DOCKER_FACTS_SNIPPET = """
h="${DSH_HOME:-/data}"
echo "@@HOME@@ $h"
echo "@@PATCH@@ $([ -f "$h/cordis.patch.yml" ] && echo yes || echo no)"
echo "@@BUNDLES@@"
cat "$h/profiles/web/package.json" 2>/dev/null || true
echo "@@SKILLS@@"
ls -1 "$h/skills" 2>/dev/null || true
echo "@@END@@"
"""


@dataclass
class ContainerStats:
    """一次 `docker stats` 采样。

    `docker inspect` 里**没有** CPU 与内存，所以运行态卡片的这两格从建库第一天
    起就一直是空的——不是"没采到"，是那条命令根本给不出这个值。
    """

    cpu_pct: float | None = None
    mem_usage_bytes: int | None = None
    mem_limit_bytes: int | None = None


@runtime_checkable
class DshRuntime(Protocol):
    """被管主机上的 DSH 容器操作。实现方负责把命令拼成单条 SSH 调用。"""

    async def inspect(self, container: str) -> ContainerState | None: ...
    async def stats(self, container: str) -> ContainerStats:
        """CPU / 内存。取不到就返回全 None，不许拿 0 冒充"没负载"。"""
        ...
    async def restart(self, container: str,
                      line_sink: LineSink | None = None) -> ExecResult: ...
    async def stop(self, container: str,
                   line_sink: LineSink | None = None) -> ExecResult:
        """只 stop。任何实现里都不允许出现 rm / volume prune / 删数据目录。"""
        ...
    async def latest_token(self, container: str) -> tuple[str, str] | None:
        """从容器日志里取最后一条含 ?token= 的地址，返回 (token, url)。"""
        ...
    async def version(self, container: str) -> str: ...
    async def dump_config(self, container: str) -> str:
        """`dsh web --dump-config` 原文（容器里实际生效的那份）。取不到给空串。"""
        ...
    async def container_facts(self, container: str) -> str:
        """技能目录 / profile 组合包 / home 层补丁，一次 SSH 往返取回分段文本。"""
        ...


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    status_code: int
    # protected = 首页也要登录（准入墙生效），这是 DSH 的正常状态；
    # auth_open = 没带 token 的受保护接口竟然放行，属于危险状态。
    verdict: Literal["ok", "protected", "host_origin_mismatch", "auth_open",
                     "unreachable", "unexpected"]
    hint: str = ""


@runtime_checkable
class PublicProbe(Protocol):
    async def check(self, base_url: str, *, expect: str = "dsh") -> ProbeResult:
        """打两次：首页期望 200；无 token 打 /api/remote.mux 带正确 Origin。

        401 = 反代头形状正确（拦在没登录）；403 = Host 与 Origin 又不等了，
        提示去 nginxWebUI 把该站 location 的 Header Host 改成 $host。

        ``expect="service"`` 用于门后面不是 DSH 的场合（比如主控台自己的入口）：
        那里没有 DSH 的准入墙，mux 的状态码不作判据，首页 2xx 即通。
        """
        ...


ProviderMode = Literal["live", "dry_run"]


def redact(obj: Any) -> Any:
    """日志安全：把这些键的值换成 ***。任何往日志/事件里塞 dict 的地方都要先过这里。"""
    sensitive = {"token", "secret", "password", "pass", "ak", "sk", "accesskeyid",
                 "accesskeysecret", "private_key_pem", "privatekeypem", "key",
                 "authorization", "cookie", "config"}
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in sensitive else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(x) for x in obj]
    return obj
