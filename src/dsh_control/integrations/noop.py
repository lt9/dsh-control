"""noop provider：这一层「人工做」（docs/SITE-CONFIG.md 的通用性硬要求）。

``[providers]`` 填 ``dns=noop`` / ``frp=noop`` / ``ng=noop`` 时注册这里的实现：
- 不发任何请求；
- 每次"本来要写的动作"变成一条结构化 ``ManualAction``（存进 ``manual_actions``，
  有 recorder 时也记一条）。动作名一律 ``manual.<provider>_needs_*``：运维日志与
  dry_run 记录同源，动作名必须读出「还没做」，不许出现 ``dns.create`` 这类
  看似已经建过的口吻；
- 写方法返回**明确标了 manual 的合成对象**（proxy_id/server_id=0、
  record_id="manual"、status="manual"）——它们只是满足类型的占位值，指令文案里
  也逐条写明这一点；编排层见到 ``manual_required`` 应把该步标 ``skipped`` 并把
  instructions 打进日志/详情，绝不要把返回的合成 id 当真实 id 写进 instances 表。
- 读方法返回空（None/[]/False），即「这一层看不见任何外部对象」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import DnsRecord, NgSite, NotManaged, Tunnel
from .dry_run import DryRunRecorder


@dataclass(slots=True)
class ManualAction:
    provider: str
    step: str               # manual.dns_needs_record / manual.frp_needs_tunnel_delete ...
    summary: str            # 一行：要人工做什么
    instructions: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"[manual:{self.provider}] {self.step}: {self.summary}"


class _NoopBase:
    provider_name = "noop"
    manual_required = True

    def __init__(self, recorder: DryRunRecorder | None = None) -> None:
        self.recorder = recorder
        self.manual_actions: list[ManualAction] = []

    def _manual(self, step: str, summary: str, *instructions: str) -> ManualAction:
        """step 一律 `manual.<provider>_needs_*`：动作名必须读出「还没做」，
        不许出现 create/delete 这种「已经做过」的口吻——运维日志与 dry_run 同源。"""
        assert step.startswith("manual."), f"noop 步骤名必须带 manual. 前缀：{step!r}"
        action = ManualAction(self.provider_name, step, summary, list(instructions))
        self.manual_actions.append(action)
        if self.recorder is not None:
            self.recorder.record(
                f"noop:{self.provider_name}", step, method="-", target="(人工)",
                payload={"summary": summary, "instructions": list(instructions)},
                note="noop provider: 未执行任何操作，此步需要人工",
            )
        return action

    def drain_manual_actions(self) -> list[ManualAction]:
        """编排层取走并清空，塞进 deploy_steps.detail / events。"""
        out, self.manual_actions = self.manual_actions, []
        return out

    async def aclose(self) -> None:  # 与真适配器同形，编排层统一关
        return None


class NoopDns(_NoopBase):
    provider_name = "dns"

    def __init__(self, *, zone: str = "", recorder: DryRunRecorder | None = None) -> None:
        super().__init__(recorder)
        self.zone = zone

    async def find(self, rr: str) -> DnsRecord | None:
        return None

    async def create(self, rr: str, value: str, ttl: int = 600) -> DnsRecord:
        domain = f"{rr}.{self.zone}" if self.zone else rr
        self._manual(
            "manual.dns_needs_record",
            f"人工添加 A 记录 {domain} → {value} (TTL {ttl})——noop 层什么都没建",
            f"1. 在你的 DNS 后台给 {self.zone or '你的域'} 添加 A 记录 {rr} → {value}",
            "2. 返回的 record_id=\"manual\" 只是类型占位，不是真记录 id",
            "3. 建好后把真 RecordId（或等价标识）手工填进实例详情，释放流程要靠它",
        )
        return DnsRecord(record_id="manual", rr=rr, type="A", value=value,
                         status="manual")

    async def delete(self, record_id: str) -> None:
        self._manual(
            "manual.dns_needs_record_delete",
            f"人工删除解析记录 id={record_id}——noop 层什么都没删",
            "到 DNS 后台按该 id 删除；只删本控制台登记过的那条",
        )


class NoopFrp(_NoopBase):
    provider_name = "frp"

    async def list(self, client_id: str | None = None) -> list[Tunnel]:
        return []

    async def get(self, proxy_id: int) -> Tunnel | None:
        return None

    async def find_by_name(self, name: str, client_id: str) -> Tunnel | None:
        return None

    async def remote_port_taken(self, port: int) -> bool:
        self._manual(
            "manual.frp_needs_port_check", f"人工确认公网端口 :{port} 未被占用",
            "noop 模式下无法探测 frps 端口占用，分配端口前自己看一眼",
        )
        return False

    async def ensure(
        self, *, name: str, client_id: str, server_id: str, local_ip: str,
        local_port: int, remote_port: int,
    ) -> Tunnel:
        self._manual(
            "manual.frp_needs_tunnel",
            f"人工创建隧道 {name}: frps:{remote_port} → {client_id} → "
            f"{local_ip}:{local_port}——noop 层什么都没建",
            f"1. 在 frp 面板（或 frpc 配置）建 tcp 隧道 {name}",
            f"2. remotePort={remote_port}，local {local_ip}:{local_port}，客户端 {client_id}",
            "3. 返回的 proxy_id=0 只是类型占位，不是真隧道 id；"
            "建好后把面板里的真 proxy id 手工填进实例的 frp_proxy_id",
        )
        return Tunnel(proxy_id=0, name=name, client_id=client_id,
                      server_id=server_id, local_ip=local_ip, local_port=local_port,
                      remote_port=remote_port, raw={"manual": True})

    async def delete(self, proxy_id: int, *, expect_name: str) -> None:
        self._manual(
            "manual.frp_needs_tunnel_delete",
            f"人工删除隧道 {expect_name} (proxy_id={proxy_id})——noop 层什么都没删",
            "只删本控制台登记的那条；别人的隧道一律不碰",
        )


class NoopNg(_NoopBase):
    provider_name = "ng"

    async def find_site(self, server_name: str) -> NgSite | None:
        return None

    async def ensure_site(
        self,
        *,
        server_name: str,
        upstream_name: str,
        upstream_server: str,
        listen_port: int = 443,
        cert_name: str | None = None,
        header_host: str = "$host",
        websocket: bool = True,
    ) -> NgSite:
        self._manual(
            "manual.ng_needs_site",
            f"人工建反代站点 {server_name} → upstream {upstream_name} "
            f"({upstream_server})——noop 层什么都没建",
            f"1. upstream：{upstream_name}，后端 {upstream_server}",
            f"2. 站点：listen {listen_port}" + (f"，证书 {cert_name}" if cert_name else "（无 TLS）"),
            "3. 必须建 location（path=/，反代到 upstream），不要用服务器级反代",
            "4. location 的 Header Host 固定 $host（带端口会被 DSH 403 掉 WebSocket）",
            "5. websocket 打开",
            "6. 返回的 server_id=0 / location_id=null 只是类型占位，不是真 id；"
            "建好后把真 server id / location id / upstream id 手工填进实例的 ng_* 列",
        )
        return NgSite(server_id=0, server_name=server_name, upstream_id=None,
                      location_id=None, header_host=header_host,
                      websocket=websocket)

    async def delete_site(self, server_id: int, *, expect_name: str) -> None:
        self._manual(
            "manual.ng_needs_site_delete",
            f"人工删除站点 {expect_name} (server_id={server_id})——noop 层什么都没删",
            "先删站点、再删它专用的 upstream；别的站点一条不碰",
        )

    async def reload(self) -> None:
        self._manual("manual.ng_needs_reload", "人工 reload nginx（noop 层没有配置可变）",
                     "改完配置记得在面板里点重载（或 nginx -s reload）")


def as_not_managed(obj: Any) -> None:  # pragma: no cover - 占位防误用
    """noop provider 没有拒绝场景，这个函数只是防呆占位。"""
    raise NotManaged("noop provider 不执行任何真实操作")
