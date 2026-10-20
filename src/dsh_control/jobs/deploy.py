"""deploy 状态机（契约 §6.1）。每步把命令、耗时、stdout 摘要写进 deploy_steps，
stdout/stderr 逐行进 deploy_logs。「失败怎么办」列实现为 rollback_actions。"""

from __future__ import annotations

from sqlalchemy import func, select

from ..db import repo
from ..db.models import Instance
from ..domain.config_docs import CONFIG_VERSION, default_rendered
from ..domain.slug import dns_rr
from .capture import capture_token
from .runner import JobCtx, StepFailed


def frp_tunnel_name(slug: str) -> str:
    # 契约 §5.2：本控制台建的隧道 name 固定 dshctl_<slug>
    return f"dshctl_{slug}"


async def _pick_frp_port(ctx: JobCtx) -> int:
    db_max = await ctx.s.scalar(select(func.max(Instance.frp_remote_port))) or 0
    port = max(ctx.settings.frp_port_start, db_max + 1)
    frp = ctx.frp()
    for _ in range(200):
        if not await frp.remote_port_taken(port):
            return port
        await ctx.log("info", f"frp 端口 {port} 已被占用，探测下一个")
        port += 1
    raise StepFailed("连续 200 个 frp 端口都被占用")


async def run_deploy(ctx: JobCtx) -> None:
    inst = ctx.instance
    host = ctx.host
    slug = inst.slug
    rr = dns_rr(slug)
    dns_name = inst.dns_name or f"{slug}.{ctx.settings.dns_zone}"
    public_url = inst.public_url or f"https://{dns_name}"

    # ---- 10 plan：slug 已在建实例时生成，这里去 DNS 确认未被占用（无副作用）
    async with ctx.step(10, "plan", "生成域名前缀并查重"):
        await ctx.log("info", f"slug={slug} rr={rr}")
        existing = await ctx.dns().find(rr)
        if existing is not None:
            raise StepFailed(
                f"DNS 里已存在记录 {rr}.{ctx.settings.dns_zone} (RecordId={existing.record_id})，"
                "请重新创建实例以换一个新的域名前缀"
            )
        inst.dns_name = dns_name
        inst.public_url = public_url
        inst.container_name = inst.container_name or slug
        inst.config_version = CONFIG_VERSION
        await ctx.s.commit()

    # ---- 20 host_prepare：主机可达 / 数据盘 / kit 在位
    async with ctx.step(20, "host_prepare", f"主机 {host.name} 预检"):
        kit = host.kit_path
        r = await ctx.ssh(
            f"set -e; echo '[conn] ok'; "
            f"df -P '{host.data_root}' | tail -1; "
            f"test -x '{kit}/scripts/new-instance.sh' && echo '[kit] new-instance.sh ok'; "
            f"test -x '{kit}/scripts/deploy.sh' && echo '[kit] deploy.sh ok'; "
            f"test -x '{kit}/scripts/verify.sh' && echo '[kit] verify.sh ok'; "
            f"docker --version"
        )
        if not r.ok:
            raise StepFailed(f"主机预检失败: {r.tail()}")
        rendered = default_rendered(
            slug=slug, public_url=public_url,
            trusted_hosts=[dns_name], container_port=0, frp_remote_port=0,
            data_dir=f"{host.data_root.rstrip('/')}/{slug}", image_tag="dsh-family:local",
        )
        await repo.add_config_snapshot(ctx.s, inst.id, CONFIG_VERSION, rendered)

    # ---- 30 dns_create：加 A 记录并回读拿 RecordId；失败删掉刚建的
    created_record_id = ""
    async with ctx.step(30, "dns_create", f"添加 DNS A 记录 {rr}"):
        rec = await ctx.dns().create(rr, ctx.settings.ingress_ip, ttl=600)
        created_record_id = rec.record_id
        ctx.rollback_actions.append(
            ("dns_delete", lambda rid=created_record_id: ctx.dns().delete(rid))
        )
        back = await ctx.dns().find(rr)
        if back is None:
            raise StepFailed("DNS 记录创建后回读不到")
        inst.dns_record_id = back.record_id
        await ctx.s.commit()
        await ctx.event("dns_created", f"A {rr} -> {ctx.settings.ingress_ip}",
                        {"record_id": back.record_id, "rr": rr, "value": ctx.settings.ingress_ip})

    # ---- 40 frp_create：先探测端口再建隧道；失败删刚建的
    async with ctx.step(40, "frp_create", "创建 frp 隧道"):
        port = await _pick_frp_port(ctx)
        tunnel = await ctx.frp().ensure(
            name=frp_tunnel_name(slug), client_id=host.frpc_client_id,
            server_id="default", local_ip="127.0.0.1",
            local_port=port, remote_port=port,
        )
        ctx.rollback_actions.append(
            ("frp_delete", lambda pid=tunnel.proxy_id: ctx.frp().delete(
                pid, expect_name=frp_tunnel_name(slug)))
        )
        inst.frp_proxy_id = tunnel.proxy_id
        inst.frp_remote_port = tunnel.remote_port or port
        inst.container_port = port  # 容器发布端口与隧道 remotePort 同号，全局唯一
        await ctx.s.commit()
        await ctx.event("frp_created", f"隧道 {tunnel.name} 远程端口 {inst.frp_remote_port}",
                        {"proxy_id": tunnel.proxy_id, "name": tunnel.name,
                         "remote_port": inst.frp_remote_port})

    # ---- 50 ng_create：upstream + 反代站点（location: $host + websocket）
    async with ctx.step(50, "ng_create", f"创建 nginxWebUI 反代 {dns_name}"):
        upstream = f"{slug}_up"
        site = await ctx.ng().ensure_site(
            server_name=dns_name, upstream_name=upstream,
            upstream_server=f"127.0.0.1:{inst.frp_remote_port}",
            listen_port=443, header_host="$host", websocket=True,
        )
        ctx.rollback_actions.append(
            ("ng_delete", lambda sid=site.server_id: ctx.ng().delete_site(
                sid, expect_name=dns_name))
        )
        await ctx.ng().reload()
        inst.ng_upstream_name = upstream
        inst.ng_server_id = site.server_id
        inst.ng_location_id = site.location_id
        await ctx.s.commit()
        await ctx.event("ng_created", f"站点 {dns_name} server_id={site.server_id}",
                        {"server_id": site.server_id, "upstream": upstream,
                         "location_id": site.location_id})

    # ---- 60 container_deploy：跑 kit 的 new-instance.sh + deploy.sh。
    # 契约：此步失败「保留现场，标 error」——从这里开始不再回滚已建对象。
    async with ctx.step(60, "container_deploy", "主机上部署容器（kit 脚本）"):
        ctx.rollback_actions.clear()
        data_dir = f"{host.data_root.rstrip('/')}/{slug}"
        env = (
            f"PUBLIC_URL='{public_url}' TRUSTED_HOSTS='{dns_name}' "
            f"CONTAINER_PORT='{inst.container_port}' DOCUMENTS_DIRECTORY='{data_dir}'"
        )
        kit = host.kit_path
        r = await ctx.ssh(
            f"cd '{kit}' && {env} ./scripts/new-instance.sh '{slug}'", timeout=600
        )
        if not r.ok:
            raise StepFailed(f"new-instance.sh 失败: {r.tail()}")
        r = await ctx.ssh(
            f"cd '{kit}' && {env} ./scripts/deploy.sh '{slug}'", timeout=900
        )
        if not r.ok:
            raise StepFailed(f"deploy.sh 失败: {r.tail()}")
        snap = await ctx.runtime().inspect(inst.container_name)
        if snap is not None:
            inst.image_tag = snap.image
            if snap.image_id:
                await repo.add_snapshot(
                    ctx.s, inst.id, docker_state=snap.state, health=snap.health,
                    restart_count=snap.restart_count,
                    raw={"image_id": snap.image_id, "ip": snap.ip},
                )
        inst.dsh_version = await ctx.runtime().version(inst.container_name)
        await ctx.s.commit()

    # ---- 70 token_capture：从容器日志抓一键地址
    async with ctx.step(70, "token_capture", "从容器日志抓登录密钥"):
        tok = await capture_token(ctx.s, ctx.runtime(), inst, "bootstrap")
        await ctx.event("token_captured", "部署后抓到一键地址",
                        {"source": "bootstrap", "url": tok.url, "token": tok.token})

    # ---- 80 probe_public：首页 200 + 无 token 打 remote.mux 期望 401
    async with ctx.step(80, "probe_public", "公网探活") as st:
        res = await ctx.probe().check(public_url)
        st.detail = f"status={res.status_code} verdict={res.verdict}"
        if res.verdict == "host_origin_mismatch":
            st.detail += "\n提示：去 nginxWebUI 把该站 location 的 Header Host 改成 $host"
        await ctx.s.commit()
        if not res.ok:
            hint = res.hint or "检查 nginxWebUI 该站 location 的 Header Host 是否为 $host"
            # 契约：标 degraded + 修正提示。state 枚举里没有 degraded，
            # 用 event + step detail 承载，不影响部署成功判定。
            await ctx.event("probe_degraded", f"公网探活异常: {res.verdict}",
                            {"status_code": res.status_code, "hint": hint})

    # ---- 90 verify：kit 的 verify.sh，只记录不推翻成功
    async with ctx.step(90, "verify", "跑 kit 校验脚本") as st:
        r = await ctx.ssh(f"cd '{host.kit_path}' && ./scripts/verify.sh '{slug}'", timeout=300)
        st.detail = (st.detail + "\n" if st.detail else "") + (
            "校验通过" if r.ok else f"verify.sh 未通过（不影响部署结果）: {r.tail(300)}")
        if not r.ok:
            await ctx.event("verify_failed", f"verify.sh 未通过: {r.tail(300)}", {})
