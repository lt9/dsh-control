"""cli expose：给主控台自己的公网入口开三处外部对象。

顺序与实例开通一致：DNS → frp → NG → 探活。**只创建，绝不删除。**

幂等靠库不靠记性：三处 id 存在 credentials(kind='site', name='console-endpoint')
的 secret JSON 里；每次先按名字回读外部对象，已存在就跳过并打印现状。
dry-run 时写请求由各适配器（settings.dry_run=True）送进 recorder，一个不发；
本模块在 dry-run 下也绝不写自己的库。

站点事实全部来自配置：回源地址必须 frps_upstream_host（不许回落 ingress_ip，
云上无 NAT 回环）；主控台自己所在主机由 DSHC_CONSOLE_HOST 指定（hosts.name 或
ssh_host），恰好只有一台 enabled 主机时可省略。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select

from .db import repo
from .db.models import Credential, Host
from .domain.frp_port import pick_free_port

CONSOLE_TUNNEL = "dshctl_console"        # 隧道名（dsh_ 前缀，删除守卫同样适用）
CONSOLE_UPSTREAM = "DSHCTL_CONSOLE"      # NG upstream 名（与 DSH_NN 同族规则）
ENDPOINT_CRED_KIND = "site"
ENDPOINT_CRED_NAME = "console-endpoint"

LogFn = Callable[[str, str], Awaitable[None]]


async def _noop_log(_level: str, msg: str) -> None:
    print(msg)


def _synthetic_id(v: object) -> bool:
    return v is None or v in ("", 0, "0", "manual")


def console_hostname(public_base: str) -> str:
    host = urlsplit(public_base).hostname or ""
    if not host:
        raise RuntimeError(f"DSHC_PUBLIC_BASE（{public_base!r}）里没有主机名")
    return host


def console_rr(hostname: str, zone: str) -> str:
    if not hostname.endswith(f".{zone}") or hostname == zone:
        raise RuntimeError(
            f"入口域名 {hostname!r} 不在 DNS zone {zone!r} 之下——"
            "A 记录没法挂；改 DSHC_PUBLIC_BASE 或先 seed 正确的 dns_zone")
    rr = hostname[: -(len(zone) + 1)]
    if "." in rr:
        raise RuntimeError(f"入口域名 {hostname!r} 不是一级子域（通配证书只覆盖一层）")
    return rr


async def _resolve_console_host(s: Any, settings: Any) -> Host:
    hosts = [h for h in await repo.list_hosts(s) if h.enabled]
    want = getattr(settings, "console_host", "") or ""
    if want:
        for h in hosts:
            if want in (h.name, h.ssh_host):
                return h
        raise RuntimeError(
            f"DSHC_CONSOLE_HOST={want!r} 没有匹配到 enabled 主机"
            "（按 hosts.name 或 ssh_host 匹配）")
    if len(hosts) == 1:
        return hosts[0]
    names = ", ".join(f"{h.name}({h.ssh_host})" for h in hosts) or "（没有）"
    raise RuntimeError(
        f"hosts 表里有 {len(hosts)} 台 enabled 主机（{names}），"
        "无法确定主控台自己跑在哪台：设 DSHC_CONSOLE_HOST=<name 或 ssh_host>")


async def run_expose(
    db: Any,
    providers: Any,
    settings: Any,
    *,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """幂等地把主控台入口打通；返回汇总 dict（cli 负责最终打印）。"""
    log = log or _noop_log
    # ---- 前置：缺任何一项都直接拒绝，不带着空值往下走 ----
    hostname = console_hostname(settings.require("public_base"))
    zone = settings.require("dns_zone")
    ingress_ip = settings.require("ingress_ip")
    frps_host = settings.require("frps_upstream_host")  # 不许用 ingress_ip 顶
    rr = console_rr(hostname, zone)

    summary: dict[str, Any] = {"hostname": hostname, "rr": rr,
                               "dns": "?", "frp": "?", "ng": "?", "probe": "?"}

    async def persist(s: Any, saved: dict[str, Any], **updates: Any) -> None:
        clean = {k: v for k, v in updates.items() if not _synthetic_id(v)}
        saved.update(clean)
        if not dry_run:
            await repo.upsert_credential(
                s, kind=ENDPOINT_CRED_KIND, name=ENDPOINT_CRED_NAME,
                secret=saved, note="cli expose：主控台入口三处外部对象的 id 登记")
            await log("info", f"id 已登记进 credentials({ENDPOINT_CRED_KIND}/{ENDPOINT_CRED_NAME})")

    async with db.session() as s:
        cred = await s.scalar(select(Credential).where(
            Credential.kind == ENDPOINT_CRED_KIND,
            Credential.name == ENDPOINT_CRED_NAME))
        saved: dict[str, Any] = dict(cred.secret or {}) if cred else {}
        host = await _resolve_console_host(s, settings)
        await log("info", f"入口域名 {hostname}（A 记录 rr={rr} -> {ingress_ip}）；"
                          f"主控台主机 {host.name}（{host.ssh_host}）；"
                          f"NG 将以 {frps_host} 回源")

        # ---- 1 DNS ----
        existing = await providers.dns.find(rr)
        if existing is not None:
            summary["dns"] = "exists"
            await log("info", f"DNS 已存在：{rr} A -> {existing.value} "
                              f"(RecordId={existing.record_id})，跳过")
            if _synthetic_id(saved.get("dns_record_id")) and not dry_run:
                await persist(s, saved, dns_record_id=existing.record_id)
        else:
            rec = await providers.dns.create(rr, ingress_ip, ttl=600)
            back = None if _synthetic_id(rec.record_id) else await providers.dns.find(rr)
            rid = (back.record_id if back else rec.record_id)
            summary["dns"] = "dry-run" if dry_run else "created"
            await log("info", f"DNS {'[dry-run] ' if dry_run else ''}A {rr} -> {ingress_ip}"
                              f"（RecordId={rid or '（dry-run 合成）'}）")
            await persist(s, saved, dns_record_id=rid)

        # ---- 2 frp ----
        tunnel = await providers.frp.find_by_name(CONSOLE_TUNNEL, host.frpc_client_id)
        if tunnel is not None and not _synthetic_id(tunnel.proxy_id):
            summary["frp"] = "exists"
            port = tunnel.remote_port or saved.get("frp_remote_port") or 0
            await log("info", f"frp 已存在：隧道 {CONSOLE_TUNNEL} proxy_id={tunnel.proxy_id}"
                              f" remotePort={port}，跳过")
            await persist(s, saved, frp_proxy_id=tunnel.proxy_id,
                          frp_remote_port=port)
        else:
            port = await pick_free_port(
                s, providers.frp, settings,
                extra_taken=[saved.get("frp_remote_port")],
                log=lambda m: log("info", m))
            t = await providers.frp.ensure(
                name=CONSOLE_TUNNEL, client_id=host.frpc_client_id,
                server_id="default", local_ip=host.ssh_host,
                local_port=settings.listen_port, remote_port=port)
            summary["frp"] = "dry-run" if dry_run else "created"
            await log("info", f"frp {'[dry-run] ' if dry_run else ''}隧道 {CONSOLE_TUNNEL}："
                              f"{host.ssh_host}:{settings.listen_port} -> "
                              f"公网端口 {port}（proxy_id={t.proxy_id or '（dry-run 合成）'}）")
            await persist(s, saved, frp_proxy_id=t.proxy_id, frp_remote_port=port)
        summary["frp_remote_port"] = port

        # ---- 3 NG ----
        site = await providers.ng.find_site(hostname)
        if site is not None and not _synthetic_id(site.server_id):
            summary["ng"] = "exists"
            await log("info", f"NG 已存在：站点 {hostname} server_id={site.server_id}，跳过")
            await persist(s, saved, ng_server_id=site.server_id,
                          ng_location_id=site.location_id,
                          ng_upstream_name=saved.get("ng_upstream_name")
                          or CONSOLE_UPSTREAM)
        else:
            cert = getattr(settings, "wildcard_cert_name", "") or None
            site = await providers.ng.ensure_site(
                server_name=hostname, upstream_name=CONSOLE_UPSTREAM,
                upstream_server=f"{frps_host}:{port}", listen_port=443,
                cert_name=cert, header_host="$host", websocket=True)
            await providers.ng.reload()   # ensure_site 不会自动 reload（对接要点 1）
            summary["ng"] = "dry-run" if dry_run else "created"
            await log("info", f"NG {'[dry-run] ' if dry_run else ''}站点 {hostname}："
                              f"upstream {CONSOLE_UPSTREAM} = {frps_host}:{port}，"
                              "headerHost=$host + websocket，reload 完成"
                              + ("（cert 未配置，走适配器默认）" if not cert else f"（cert={cert}）"))
            await persist(s, saved, ng_server_id=site.server_id,
                          ng_location_id=site.location_id,
                          ng_upstream_name=CONSOLE_UPSTREAM)

    # ---- 4 探活 ----
    base_url = f"https://{hostname}"
    res = await providers.probe.check(base_url)
    summary["probe"] = res.verdict
    if res.ok:
        await log("info", f"探活 ok：{base_url}（HTTP {res.status_code}）")
    else:
        await log("warn", f"探活 {res.verdict}：{res.hint}")
        if res.verdict == "host_origin_mismatch":
            await log("warn", "host_origin_mismatch = NG 的 headerHost 没对齐："
                              "入口站 location 必须是 headerHost=$host（不能带端口）")

    if dry_run:
        recorder = getattr(providers, "recorder", None)
        entries = getattr(recorder, "entries", []) if recorder else []
        if entries:
            await log("info", f"dry-run：以下 {len(entries)} 个写请求将被拦下、一个不发：")
            for e in entries:
                await log("info", e.line())
        else:
            await log("info", "dry-run：noop/假 provider 没有 recorder——"
                              "写请求将由真实适配器在 dry_run 下逐条记录")

    # ---- 收尾打印（含人工确认项）----
    await log("info", f"入口 URL：{base_url}/?token=…（登录密钥走详情页 reveal）")
    await log("info", f"公网端口：{summary['frp_remote_port']}（frps -> "
                      f"{host.ssh_host}:{settings.listen_port}）")
    await log("info", f"[人工确认] 云上安全组/防火墙需放行 TCP {summary['frp_remote_port']}"
                      "——安全组不在我们的 API 管辖内，必须人工开；"
                      "探活 unreachable 时先查这里，再查 DNS TTL")
    return summary
