"""测试替身与夹具：aiosqlite 内存库 + 假 provider，全部调用记进一条 Journal。

假对象严格照 integrations/base.py 的 Protocol 实现；释放流程的「只 stop 不 rm」
与 SEED.md §3 第 4/5 步的语义都靠 Journal 断言。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from dsh_control.db import repo
from dsh_control.db.session import create_all, make_db
from dsh_control.integrations.base import (
    ContainerState,
    DnsRecord,
    ExecResult,
    IntegrationError,
    NgSite,
    NotManaged,
    ProbeResult,
    Tunnel,
)
from dsh_control.main import create_app
from dsh_control.settings import Settings

OP_KEY = "test-operator-key-1234"
XRW = {"X-Requested-With": "1"}


class Journal(list):
    def add(self, *parts: object) -> None:
        self.append(" ".join(str(p) for p in parts))


class FakeDns:
    manual_required = False

    def __init__(self, journal: Journal, *, zone: str = "example.test",
                 manual: bool = False) -> None:
        self.journal = journal
        self.zone = zone
        self.manual_required = manual
        self.records: dict[str, DnsRecord] = {}
        self.existing: dict[str, DnsRecord] = {}  # 预设占用
        self._n = 0

    async def find(self, rr: str) -> DnsRecord | None:
        self.journal.add("dns.find", rr)
        return self.existing.get(rr) or self.records.get(rr)

    async def create(self, rr: str, value: str, ttl: int = 600) -> DnsRecord:
        if self.manual_required:  # 与 registry Noop* 同德性：不落真状态、给合成 id
            self.journal.add("noop.dns.create", rr, value)
            return DnsRecord(record_id="manual", rr=rr, type="A", value=value)
        self.journal.add("dns.create", rr, value)
        self._n += 1
        rec = DnsRecord(record_id=f"rec-{self._n}", rr=rr, type="A", value=value)
        self.records[rr] = rec
        return rec

    async def delete(self, record_id: str) -> None:
        self.journal.add("dns.delete", record_id)
        for rr, rec in list(self.records.items()):
            if rec.record_id == record_id:
                del self.records[rr]
                return
        raise NotManaged(f"RecordId {record_id} 不是本控制台建的")


class FakeFrp:
    manual_required = False

    def __init__(self, journal: Journal, *, manual: bool = False) -> None:
        self.journal = journal
        self.manual_required = manual
        self.tunnels: dict[int, Tunnel] = {}
        self.taken: set[int] = set()
        self._n = 0

    async def list(self, client_id: str | None = None) -> list[Tunnel]:
        return list(self.tunnels.values())

    async def get(self, proxy_id: int) -> Tunnel | None:
        return self.tunnels.get(proxy_id)

    async def find_by_name(self, name: str, client_id: str) -> Tunnel | None:
        return next((t for t in self.tunnels.values()
                     if t.name == name and t.client_id == client_id), None)

    async def ensure(self, *, name: str, client_id: str, server_id: str,
                     local_ip: str, local_port: int, remote_port: int) -> Tunnel:
        assert name.startswith(("dsh_", "dshctl_")), f"隧道名越界: {name}"
        self.journal.add("noop.frp.ensure" if self.manual_required else "frp.ensure",
                         name, f"remote={remote_port}", f"local={local_ip}")
        if self.manual_required:
            return Tunnel(proxy_id=0, name=name, client_id=client_id,
                          server_id=server_id, local_ip=local_ip,
                          local_port=local_port, remote_port=remote_port)
        if remote_port in self.taken:
            raise IntegrationError(f"端口 {remote_port} 已占用")
        self._n += 1
        t = Tunnel(proxy_id=self._n, name=name, client_id=client_id, server_id=server_id,
                   local_ip=local_ip, local_port=local_port, remote_port=remote_port)
        self.tunnels[self._n] = t
        self.taken.add(remote_port)
        return t

    async def delete(self, proxy_id: int, *, expect_name: str) -> None:
        assert expect_name.startswith(("dsh_", "dshctl_")), expect_name
        t = self.tunnels.get(proxy_id)
        if t is None or t.name != expect_name:
            raise NotManaged(f"proxy_id {proxy_id} 与 expect_name 不符")
        self.journal.add("frp.delete", t.name, f"id={proxy_id}")
        del self.tunnels[proxy_id]
        self.taken.discard(t.remote_port)

    async def remote_port_taken(self, port: int) -> bool:
        return port in self.taken


class FakeNg:
    manual_required = False

    def __init__(self, journal: Journal, *, manual: bool = False) -> None:
        self.journal = journal
        self.manual_required = manual
        self.sites: dict[int, NgSite] = {}
        self.reloads = 0
        self._n = 0

    async def find_site(self, server_name: str) -> NgSite | None:
        return next((s for s in self.sites.values() if s.server_name == server_name), None)

    async def ensure_site(self, *, server_name: str, upstream_name: str,
                          upstream_server: str, listen_port: int = 443,
                          cert_name: str | None = None, header_host: str = "$host",
                          websocket: bool = True) -> NgSite:
        assert header_host == "$host", "headerHost 必须是 $host（NG-API.md）"
        assert websocket, "websocket 必须开"
        self.journal.add("noop.ng.ensure" if self.manual_required
                         else "ng.ensure", server_name, upstream_name, upstream_server)
        if self.manual_required:
            return NgSite(server_id=0, server_name=server_name,
                          location_id=0, header_host=header_host,
                          websocket=websocket)
        self._n += 1
        site = NgSite(server_id=self._n, server_name=server_name,
                      location_id=self._n * 10, header_host=header_host,
                      websocket=websocket)
        self.sites[self._n] = site
        return site

    async def delete_site(self, server_id: int, *, expect_name: str) -> None:
        site = self.sites.get(server_id)
        if site is None or site.server_name != expect_name:
            raise NotManaged(f"server_id {server_id} 与 expect_name 不符")
        self.journal.add("ng.delete", site.server_name, f"id={server_id}")
        del self.sites[server_id]

    async def reload(self) -> None:
        self.journal.add("ng.reload")
        self.reloads += 1


class FakeExecutor:
    def __init__(self, journal: Journal, *, manual: bool = False) -> None:
        self.journal = journal
        self.manual_required = manual
        self.handlers: list[tuple[str, ExecResult]] = []
        self.commands: list[str] = []

    async def run(self, *, host: str, port: int, user: str, private_key_pem: str,
                  command: str, timeout: float = 300, line_sink=None) -> ExecResult:
        self.commands.append(command)
        self.journal.add("ssh", host, command[:80].replace("\n", " "))
        if line_sink:
            line_sink("stdout", f"$ {command}")
        for pattern, result in self.handlers:
            if pattern in command:
                return result
        return ExecResult(0, "ok\n", "")


class FakeRuntime:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.exists = True
        self.state = "running"
        self.health = "healthy"
        self.restart_count = 0
        self._token_seq = 0
        self.token = ""
        self._advance()

    def _advance(self) -> None:
        self._token_seq += 1
        self.token = f"tok-{self._token_seq:04d}"

    def advance_token(self) -> str:
        self._advance()
        return self.token

    async def inspect(self, container: str) -> ContainerState | None:
        if not self.exists:
            return None
        return ContainerState(name=container, state=self.state, health=self.health,
                              image="dsh-family:local", image_id="sha256:aaa",
                              restart_count=self.restart_count, ip="172.17.0.9",
                              cpu_pct=1.5, mem_usage_bytes=1 << 30,
                              mem_limit_bytes=1 << 34)

    async def restart(self, container: str, line_sink=None) -> ExecResult:
        self.journal.add("runtime.restart", container)
        self.restart_count += 1
        self._advance()
        self.state, self.health = "running", "healthy"
        if line_sink:
            line_sink("stdout", "docker restart ok")
        return ExecResult(0, "ok", "")

    async def stop(self, container: str, line_sink=None) -> ExecResult:
        self.journal.add("runtime.stop", container)
        self.state = "exited"
        if line_sink:
            line_sink("stdout", "docker stop ok")
        return ExecResult(0, "ok", "")

    async def latest_token(self, container: str) -> tuple[str, str] | None:
        return (self.token, f"https://{container}.example.test/?token={self.token}")

    async def version(self, container: str) -> str:
        return "0.9.9"


class FakeProbe:
    def __init__(self, journal: Journal,
                 result: ProbeResult | None = None) -> None:
        self.journal = journal
        self.result = result or ProbeResult(True, 200, "ok")

    async def check(self, base_url: str) -> ProbeResult:
        self.journal.add("probe", base_url, self.result.verdict)
        return self.result


class Env:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


def make_providers(journal: Journal, *, manual_layers: tuple[str, ...] = ()):
    dns = FakeDns(journal, manual="dns" in manual_layers)
    frp = FakeFrp(journal, manual="frp" in manual_layers)
    ng = FakeNg(journal, manual="ng" in manual_layers)
    runtime = FakeRuntime(journal)
    providers = SimpleNamespace(dns=dns, frp=frp, ng=ng,
        executor=FakeExecutor(journal),
        probe=FakeProbe(journal),
        runtime_for=lambda **kw: runtime,
    )
    return providers, runtime


def make_settings(**over) -> Settings:
    base = {
        "_env_file": None,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "session_secret": "unit-test-secret",
        "dns_zone": "example.test",
        "ingress_ip": "203.0.113.10",
        "public_base": "https://console.example.test",
        "provider_dns": "aliyun", "provider_frp": "frp_panel", "provider_ng": "nginxwebui",
        "frp_port_start": 4893,
        "wait_healthy_seconds": 1,
        "poll_interval": 1,
    }
    base.update(over)
    return Settings(**base)


async def seed_host(db, *, name="nas-1", ssh_host="192.0.2.10", ssh_port=22,
                    ssh_user="ops", data_root="/hdd1/dsh-data",
                    kit_path="/srv/dsh-family", frpc_client_id="ops.c.nas1@1",
                    enabled=True) -> int:
    from sqlalchemy import select

    from dsh_control.db.models import Host

    async with db.session() as s:
        cred = await repo.upsert_credential(
            s, kind="ssh_key", name=f"key-{name}",
            secret={"private_key_pem": "FAKE-PEM"}, note="")
        host = await s.scalar(select(Host).where(Host.name == name))
        if host is None:
            host = Host(name=name)
            s.add(host)
        host.ssh_host, host.ssh_port, host.ssh_user = ssh_host, ssh_port, ssh_user
        host.ssh_credential_id = cred.id
        host.kit_path, host.data_root = kit_path, data_root
        host.frpc_client_id = frpc_client_id
        host.enabled = enabled
        await s.commit()
        return host.id


@pytest_asyncio.fixture
async def env(tmp_path):
    """live provider 环境：两台主机（一台机械盘一台固态），一个运维密钥。"""
    settings = make_settings()
    db = make_db("sqlite+aiosqlite:///:memory:")
    await create_all(db)
    journal = Journal()
    providers, runtime = make_providers(journal)
    app = create_app(db=db, providers=providers, settings=settings,
                     start_watcher=False, static_dir=tmp_path / "dist")
    async with db.session() as s:
        await repo.upsert_credential(s, kind="operator_key", name="default",
                                     secret={"key": OP_KEY}, note="")
    nas = await seed_host(db, name="nas-1", ssh_port=2333, data_root="/hdd1/dsh-data")
    ssd = await seed_host(db, name="host-2", ssh_host="192.0.2.20",
                          data_root="/ssd1/dsh-data")
    # 探测输出：nas 机械盘 10GB，host-2 固态 100GB —— prefer_hdd 应选 nas
    ex = providers.executor
    ex.handlers = [("/hdd1", ExecResult(0, "10485760 1\n", "")),
                   ("/ssd1", ExecResult(0, "104857600 0\n", ""))]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/login", json={"key": OP_KEY})
        assert r.status_code == 200, r.text
        yield Env(db=db, app=app, client=client, providers=providers,
                  runtime=runtime, journal=journal, settings=settings,
                  host_nas=nas, host_ssd=ssd)
    await db.engine.dispose()


async def wait_job(app, job_id: int, timeout: float = 15) -> None:
    await app.state.runner.wait(job_id, timeout)


@pytest_asyncio.fixture
async def noop_env(tmp_path):
    """三层全 noop 的环境：部署必须照样成功，相应步骤标 skipped。"""
    settings = make_settings(provider_dns="noop", provider_frp="noop",
                             provider_ng="noop")
    db = make_db("sqlite+aiosqlite:///:memory:")
    await create_all(db)
    journal = Journal()
    providers, runtime = make_providers(journal, manual_layers=("dns", "frp", "ng"))
    app = create_app(db=db, providers=providers, settings=settings,
                     start_watcher=False, static_dir=tmp_path / "dist")
    async with db.session() as s:
        await repo.upsert_credential(s, kind="operator_key", name="default",
                                     secret={"key": OP_KEY}, note="")
    await seed_host(db, name="solo")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/login", json={"key": OP_KEY})
        assert r.status_code == 200
        yield Env(db=db, app=app, client=client, providers=providers,
                  runtime=runtime, journal=journal, settings=settings)
    await db.engine.dispose()
