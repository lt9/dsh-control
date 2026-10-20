"""deploy 状态机（契约 §6.1）。每步把命令、耗时、stdout 摘要写进 deploy_steps，
stdout/stderr 逐行进 deploy_logs。「失败怎么办」列实现为 rollback_actions。

provider 注册表（docs/SITE-CONFIG.md）：settings.provider_dns/frp/ng 为 `noop` 时，
对应步骤标 skipped 并把要人工做的事打印进日志——没有阿里云/没有 nginxWebUI 也能用。
站点事实（入口 IP、zone、端口起点）一律从 settings（site 段灌入）取，代码零硬编码。
"""

from __future__ import annotations

from ..db import repo
from ..domain.config_docs import CONFIG_VERSION, default_rendered
from ..domain.frp_port import pick_free_port
from ..domain.slug import derive, dns_rr
from .capture import capture_token
from .runner import JobCtx, StepFailed


def _synthetic_id(v: object) -> bool:
    """registry 约定：noop 返回的合成占位 id（record_id="manual"、proxy_id=0）。
    这类值**绝不能**写进 instances 登记列——否则释放会拿假 id 去调真删除，
    把「只删登记过的 id」防线污染成「假 id 也算登记」。"""
    return v is None or v in ("", 0, "0", "manual")


def _drained(ctx: JobCtx, which: str) -> str:
    """把 noop provider 记录器里的人工操作清单排空、并入步骤详情。

    registry 约定：noop 写方法不抛异常，把「要人工做什么」留在自己对象里，
    编排层 skipped 时负责打印（integrations 对接要点 3）。"""
    prov = getattr(ctx.providers, which, None)
    drain = getattr(prov, "drain_manual_actions", None)
    if drain is None:
        return ""
    lines: list[str] = []
    for act in drain():
        lines.append(f"  · {act.line()}")
        lines.extend(f"      {s}" for s in getattr(act, "instructions", []))
    return "\n" + "\n".join(lines) if lines else ""


def _manual(ctx: JobCtx, which: str) -> bool:
    """noop 判定以 provider 对象为准（registry 缺凭据也会自动落 noop）：
    registry 的 Noop* 实现都带 manual_required=True。"""
    provider = getattr(ctx.providers, which, None)
    return bool(provider is not None and getattr(provider, "manual_required", False))


async def _pick_frp_port(ctx: JobCtx) -> int:
    """共享实现见 domain/frp_port.py（主控台入口 expose 用同一套，不许另写一份）。"""
    try:
        return await pick_free_port(
            ctx.s, ctx.frp(), ctx.settings, skip_probe=_manual(ctx, "frp"),
            log=lambda m: ctx.log("info", m))
    except RuntimeError as exc:
        raise StepFailed(str(exc)) from exc


async def run_deploy(ctx: JobCtx) -> None:
    inst = ctx.instance
    host = ctx.host
    slug = inst.slug
    names = derive(slug, ctx.settings.dns_zone)
    rr = dns_rr(slug)
    dns_name = inst.dns_name or names.server_name
    public_url = inst.public_url or f"https://{dns_name}"
    data_dir = f"{host.data_root.rstrip('/')}/{slug}"

    # ---- 10 plan：slug 已在建实例时生成；去 DNS 与库确认未被占用（无副作用）
    async with ctx.step(10, "plan", "域名标签查重") as st:
        # NG 回源地址必须站点显式声明（DSHC_FRPS_UPSTREAM_HOST / site.toml）：
        # 代码里不许猜、不许回落 ingress_ip——云上无 NAT 回环，公网 IP 在入口机上不通。
        frps_host = ctx.settings.require("frps_upstream_host")
        image = ctx.settings.require("dsh_image")
        await ctx.log("info", f"slug={slug} 域名={dns_name} 容器={names.container}")
        await ctx.log("info", f"镜像（准备好的版本，开通阶段永不构建）：{image}"
                              "——20 步会在目标主机上核验它在位")
        await ctx.log("info", f"NG 将以 {frps_host} 回源（frps 监听地址，"
                              "需 NG 容器视角可达；本站若 NG 为 host 网络通常填 127.0.0.1）")
        if _manual(ctx, "dns"):
            await ctx.log("info", f"DNS 层为 noop：请自行确认 {rr} 记录未被占用")
        else:
            existing = await ctx.dns().find(rr)
            if existing is not None:
                raise StepFailed(
                    f"DNS 里已存在记录 {rr}.{ctx.settings.dns_zone} "
                    f"(RecordId={existing.record_id})，重新创建实例以换新标签"
                )
        inst.dns_name = dns_name
        inst.public_url = public_url
        inst.container_name = names.container
        inst.config_version = CONFIG_VERSION
        await ctx.s.commit()

    # ---- 20 host_prepare：主机可达 / 数据盘剩余空间 / kit 在位
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
        # 镜像在位检查：开通常驻「准备好的版本」（DSHC_IMAGE），不在就现在拦下，
        # 别等 DNS/隧道/站点都建好了才发现没镜像可跑。
        img_fmt = "docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}'"
        r_img = await ctx.ssh(
            f"docker image inspect '{image}' >/dev/null 2>&1 "
            f"&& echo '[image] PRESENT' || echo '[image] MISSING'; "
            f"{img_fmt} | grep -i dsh || true")
        for line in (r_img.stdout or "").splitlines():
            if line.strip():
                await ctx.log("info", f"[image] {line.strip()}")
        if "[image] MISSING" in (r_img.stdout or ""):
            have = "; ".join(
                ln.strip() for ln in (r_img.stdout or "").splitlines()
                if ln.strip() and "[image]" not in ln) or "（无 dsh 相关镜像）"
            raise StepFailed(
                f"{host.name} 上没有镜像 {image}。先搬运过去："
                f"cli images ship --tag {image} --from <有此镜像的主机> "
                f"--to {host.name}（该机现有 dsh 镜像：{have}）")
        rendered = default_rendered(
            slug=slug, public_url=public_url, trusted_hosts=[dns_name],
            container_port=0, frp_remote_port=0, data_dir=data_dir,
            image_tag=image,
        )
        await repo.add_config_snapshot(ctx.s, inst.id, CONFIG_VERSION, rendered)

    # ---- 30 dns_create：加 A 记录并回读拿 RecordId；失败删掉刚建的
    async with ctx.step(30, "dns_create", f"添加 DNS A 记录 {rr}") as st:
        if _manual(ctx, "dns"):
            st.state = "skipped"
            await ctx.dns().create(rr, ctx.settings.require("ingress_ip"), ttl=600)
            st.detail = (
                "DNS provider = noop，需人工添加：\n"
                f"  记录: {rr} A -> {ctx.settings.require('ingress_ip')} TTL=600\n"
                f"  完整域名: {dns_name}\n"
                "完成后无需回填，容器侧不依赖 RecordId 之外的字段。"
                + _drained(ctx, "dns")
            )
            await ctx.s.commit()
        else:
            rec = await ctx.dns().create(rr, ctx.settings.require("ingress_ip"), ttl=600)
            if _synthetic_id(rec.record_id):
                # provider 没标 manual_required 却返回合成占位：同样按人工层处理
                st.state = "skipped"
                st.detail = ("provider 返回合成占位 id，未登记。这一层要人工补做：\n"
                             f"  记录: {rr} A -> {ctx.settings.require('ingress_ip')}\n"
                             "  补做后把真实 RecordId 人工回填，否则释放时删不到。"
                             + _drained(ctx, "dns"))
                await ctx.s.commit()
                await ctx.event("dns_manual_pending",
                                f"DNS 层返回合成 id，需人工补做并回填: {rr}",
                                {"rr": rr})
                st_skip_dns = True
            else:
                st_skip_dns = False
            if not st_skip_dns:
                ctx.rollback_actions.append(
                    ("dns_delete", lambda rid=rec.record_id: ctx.dns().delete(rid))
                )
                back = await ctx.dns().find(rr)
                if back is None:
                    raise StepFailed("DNS 记录创建后回读不到")
                if _synthetic_id(back.record_id):
                    raise StepFailed(f"DNS 回读得到合成 id {back.record_id!r}，拒绝登记")
                inst.dns_record_id = back.record_id
                await ctx.s.commit()
                await ctx.event("dns_created", f"A {rr} -> {ctx.settings.ingress_ip}",
                                {"record_id": back.record_id, "rr": rr,
                                 "value": ctx.settings.ingress_ip})

    # ---- 40 frp_create：先探测端口再建隧道；失败删刚建的
    async with ctx.step(40, "frp_create", "创建 frp 隧道") as st:
        port = await _pick_frp_port(ctx)
        if _manual(ctx, "frp"):
            st.state = "skipped"
            await ctx.frp().ensure(
                name=names.tunnel, client_id=host.frpc_client_id,
                server_id="default", local_ip=host.ssh_host,
                local_port=port, remote_port=port,
            )
            st.detail = (
                "frp provider = noop，需人工建立隧道：\n"
                f"  名称: {names.tunnel}  客户端: {host.frpc_client_id}\n"
                f"  remotePort: {port} -> {host.ssh_host}:0（容器端口见 60 步）\n"
                "释放时请把这条隧道删掉并在此登记。"
                + _drained(ctx, "frp")
            )
            inst.frp_remote_port = port
            inst.container_port = port
            await ctx.s.commit()
        else:
            tunnel = await ctx.frp().ensure(
                name=names.tunnel, client_id=host.frpc_client_id,
                server_id="default", local_ip=host.ssh_host,
                local_port=port, remote_port=port,
            )
            if _synthetic_id(tunnel.proxy_id):
                st.state = "skipped"
                st.detail = ("provider 返回合成 proxy_id，未登记。这一层要人工补做：\n"
                             f"  隧道: {names.tunnel}（client {host.frpc_client_id}，"
                             f"remotePort {port}）\n"
                             "  补做后把真实 proxy_id 人工回填，否则释放时删不到。"
                             + _drained(ctx, "frp"))
                # 端口是我们自己选的合法值，照常登记（防复用逻辑要用）
                inst.frp_remote_port = port
                inst.container_port = port
                await ctx.s.commit()
                await ctx.event("frp_manual_pending",
                                f"frp 层返回合成 id，需人工补做并回填: {names.tunnel}",
                                {"name": names.tunnel, "remote_port": port})
            else:
                ctx.rollback_actions.append(
                    ("frp_delete", lambda pid=tunnel.proxy_id, n=names.tunnel: ctx.frp().delete(
                        pid, expect_name=n))
                )
                inst.frp_proxy_id = tunnel.proxy_id
                inst.frp_remote_port = tunnel.remote_port or port
                # 容器发布端口与隧道 remotePort 同号，全局唯一
                inst.container_port = port
                await ctx.s.commit()
                await ctx.event("frp_created",
                                f"隧道 {tunnel.name} 远程端口 {inst.frp_remote_port}",
                                {"proxy_id": tunnel.proxy_id, "name": tunnel.name,
                                 "remote_port": inst.frp_remote_port})

    # ---- 50 ng_create：upstream + 反代站点（location：$host + websocket）
    async with ctx.step(50, "ng_create", f"创建反代站点 {dns_name}") as st:
        if _manual(ctx, "ng"):
            st.state = "skipped"
            await ctx.ng().ensure_site(
                server_name=dns_name, upstream_name=names.upstream,
                upstream_server=f"{ctx.settings.require('frps_upstream_host')}"
                                f":{inst.frp_remote_port}",
                listen_port=443, header_host="$host", websocket=True,
            )
            st.detail = (
                "NG provider = noop，需人工配置反向代理：\n"
                f"  upstream {names.upstream}: "
                f"{ctx.settings.require('frps_upstream_host')}"
                f":{inst.frp_remote_port}\n"
                f"  server_name {dns_name}，location / 反代到该 upstream，"
                "headerHost=$host，websocket=true\n"
                "（headerHost 带端口会让 /api/remote.mux 403，见 docs/NG-API.md）"
                + _drained(ctx, "ng")
            )
            await ctx.s.commit()
        else:
            site = await ctx.ng().ensure_site(
                server_name=dns_name, upstream_name=names.upstream,
                upstream_server=f"{ctx.settings.require('frps_upstream_host')}"
                                f":{inst.frp_remote_port}",
                listen_port=443, header_host="$host", websocket=True,
            )
            if _synthetic_id(site.server_id):
                st.state = "skipped"
                st.detail = ("provider 返回合成 server_id，未登记。这一层要人工补做：\n"
                             f"  upstream {names.upstream}: "
                             f"{ctx.settings.require('frps_upstream_host')}"
                             f":{inst.frp_remote_port}\n"
                             f"  server_name {dns_name}（headerHost=$host，websocket=true）\n"
                             "  补做后把真实 server_id 人工回填，否则释放时删不到。"
                             + _drained(ctx, "ng"))
                await ctx.s.commit()
                await ctx.event("ng_manual_pending",
                                f"NG 层返回合成 id，需人工补做并回填: {dns_name}",
                                {"server_name": dns_name, "upstream": names.upstream})
            else:
                ctx.rollback_actions.append(
                    ("ng_delete", lambda sid=site.server_id, n=dns_name: ctx.ng().delete_site(
                        sid, expect_name=n))
                )
                await ctx.ng().reload()  # ensure_site 不会自动 reload（对接要点 1）
                inst.ng_upstream_name = names.upstream
                inst.ng_server_id = site.server_id
                inst.ng_location_id = site.location_id
                await ctx.s.commit()
                await ctx.event("ng_created", f"站点 {dns_name} server_id={site.server_id}",
                                {"server_id": site.server_id, "upstream": names.upstream,
                                 "location_id": site.location_id})

    # ---- 60 container_deploy：跑 kit 的 new-instance.sh + deploy.sh。
    # 契约：此步失败「保留现场，标 error」——从这里开始不再回滚已建对象。
    async with ctx.step(60, "container_deploy", "主机上部署容器（kit 脚本）"):
        ctx.rollback_actions.clear()
        env = (
            f"DSH_IMAGE='{image}' "
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
        rt = ctx.runtime()
        snap = await rt.inspect(inst.container_name)
        if snap is not None:
            inst.image_tag = snap.image
            await repo.add_snapshot(
                ctx.s, inst.id, docker_state=snap.state, health=snap.health,
                restart_count=snap.restart_count,
                raw={"image_id": snap.image_id, "ip": snap.ip},
            )
        try:
            inst.dsh_version = await rt.version(inst.container_name)
        except Exception:  # noqa: BLE001, S110 —— 版本取不到不推翻部署
            pass
        await ctx.s.commit()

    # ---- 70 token_capture：从容器日志抓一键地址（标 error，可重试）
    async with ctx.step(70, "token_capture", "从容器日志抓登录密钥"):
        tok = await capture_token(ctx.s, ctx.runtime(), inst, "bootstrap")
        await ctx.event("token_captured", "部署后抓到一键地址",
                        {"source": "bootstrap", "url": tok.url, "token": tok.token})

    # ---- 80 probe_public：首页 200 + 无 token 打 remote.mux 期望 401
    async with ctx.step(80, "probe_public", "公网探活") as st:
        res = await ctx.probe().check(public_url)
        st.detail = f"status={res.status_code} verdict={res.verdict}"
        if res.verdict == "host_origin_mismatch":
            st.detail += "\n提示：反代 location 的 Header Host 必须是 $host（不能带端口）"
        await ctx.s.commit()
        if not res.ok:
            hint = res.hint or "检查反代 location 的 Header Host 是否为 $host"
            # 契约：标 degraded + 修正提示。state 枚举里没有 degraded，
            # 用 event + step detail 承载，不影响部署成功判定。
            await ctx.event("probe_degraded", f"公网探活异常: {res.verdict}",
                            {"status_code": res.status_code, "hint": hint})

    # ---- 90 verify：kit 的 verify.sh <slug>，收条目数，不推翻成功
    async with ctx.step(90, "verify", "跑 kit 校验脚本") as st:
        r = await ctx.ssh(f"cd '{host.kit_path}' && ./scripts/verify.sh '{slug}'", timeout=300)
        st.detail = (st.detail + "\n" if st.detail else "") + (
            "校验通过" if r.ok else f"verify.sh 未通过（不影响部署结果）: {r.tail(300)}")
        if not r.ok:
            await ctx.event("verify_failed", f"verify.sh 未通过: {r.tail(300)}", {})
