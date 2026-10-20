"""deploy 状态机（契约 §6.1）。每步把命令、耗时、stdout 摘要写进 deploy_steps，
stdout/stderr 逐行进 deploy_logs。「失败怎么办」列实现为 rollback_actions。

provider 注册表（docs/SITE-CONFIG.md）：settings.provider_dns/frp/ng 为 `noop` 时，
对应步骤标 skipped 并把要人工做的事打印进日志——没有阿里云/没有 nginxWebUI 也能用。
站点事实（入口 IP、zone、端口起点）一律从 settings（site 段灌入）取，代码零硬编码。
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from ..db import repo
from ..db.models import Instance
from ..domain.config_docs import CONFIG_VERSION, default_rendered
from ..domain.dsh_config import annotate_model_drift
from ..domain.frp_port import pick_free_port
from ..domain.safety import MUST_BE_DISABLED, config_violations
from ..domain.slug import derive, dns_rr
from .capabilities import apply_capabilities, read_dsh_config
from .capture import capture_token
from .release import EXTERNAL_ID_FIELDS, clear_external_ids, delete_ng_upstream, idempotent_delete
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


def _rollback_del(ctx: JobCtx, inst: Any, name: str,
                  delete_act: Callable[[], Awaitable[None]],
                  fields: tuple[str, ...]) -> Callable[[], Awaitable[None]]:
    """回滚动作 = 删掉这次建的对象 + 只抹它自己的登记字段。

    登记 id 的清理必须跟"谁删的"绑在一起：通吃式地清一遍，会让一次失败的**续建**
    把上一次尝试建的、这次根本没动的对象 id 也抹掉，对象当场变孤儿。
    """
    async def act() -> None:
        await delete_act()
        await clear_external_ids(ctx, inst, reason=f"开通失败已回滚 {name}", only=fields)
    return act


_DRIFT_MEANING = {
    "db_dirty": "库里 headerHost 带端口、磁盘是干净的——replace 会把它洗坏",
    "dirty_both": "库里脏、磁盘同样脏——replace 不改现状，但该站点公网正被准入墙拦住",
    "disk_dirty": "库里干净、磁盘被面板外手改过——replace 会按库把它重建",
}


def classify_drift(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把库盘体检结果分成「必须拦」和「只提醒」两堆。

    只拦 ``db_dirty``：那是 replace 会把**正在正常服务**的站点洗坏的一种，代价由
    用户承担，不能拿它换一次开通。``dirty_both`` 的站点此刻公网本就 403 拦着、
    replace 对它中性；``disk_dirty`` 是面板外手改、replace 会按库纠正——这两种
    大声提醒但不拦，否则一个与本次无关的坏站点会让所有开通永远卡死。
    ``dsh_proven=False``（证明不了后端是 DSH 的）一律不是证据，两堆都不进。
    """
    proven = [r for r in rows if r.get("dsh_proven")]
    abort = [r for r in proven if r.get("verdict") == "db_dirty"]
    warn = [r for r in proven if r.get("verdict") in ("dirty_both", "disk_dirty")]
    return abort, warn


async def _pre_replace_drift_check(ctx: JobCtx, dns_name: str) -> None:
    """replace 之前的只读库盘体检；会把好盘洗坏的站点存在时直接中止本步。

    nginxWebUI 的 ``/api/nginx/replace`` 是「按库重建**全部**站点」：库里脏的会被
    写成脏、库里没有的 conf 会被删。所以动它之前必须先证明库和磁盘对得上，
    见 docs/ARCHITECTURE.md §5.4 与 docs/05-故障排查.md。
    """
    scan = getattr(ctx.ng(), "drift_scan", None)
    if scan is None:  # noop / 测试假件：没这个能力就直说，绝不编造"已检查通过"
        await ctx.log("info", "NG provider 不提供库盘体检，本次跳过 replace 前置检查")
        return
    res = await ctx.s.scalars(select(Instance.dns_name)
                              .where(Instance.desired_state != "released"))
    known = {str(n).lower() for n in res.all() if n}
    known.add(dns_name.lower())
    rows = list(await scan(known_dsh=known) or [])
    abort, warn = classify_drift(rows)
    for r in warn:
        await ctx.log("warn", f"[体检提醒] {r['server_name']}："
                              f"{_DRIFT_MEANING.get(r['verdict'], r['verdict'])}"
                              "（不拦本次开通，但请核对）")
    if abort:
        lines = "\n".join(f"  · {r['server_name']}：{_DRIFT_MEANING[r['verdict']]}"
                          for r in abort)
        raise StepFailed(
            "nginxWebUI 体检发现：一旦按库重建（replace）就会洗坏正在服务的站点，"
            "本次不做 replace，避免连累你其它在跑的服务：\n"
            f"{lines}\n"
            "处置：用面板把该站点 location / 的 headerHost 改回 $host"
            "（本控制台的 patch_location 也能改，会自动 reload 生效），"
            "体检干净后重新开通即可。")


async def _pick_frp_port(ctx: JobCtx) -> int:
    """共享实现见 domain/frp_port.py（主控台入口 expose 用同一套，不许另写一份）。"""
    try:
        return await pick_free_port(
            ctx.s, ctx.frp(), ctx.settings, skip_probe=_manual(ctx, "frp"),
            log=lambda m: ctx.log("info", m))
    except RuntimeError as exc:
        raise StepFailed(str(exc)) from exc


async def _llm_material(ctx: JobCtx) -> dict[str, str]:
    """取站点级模型配置：地址在 settings，密钥在 credentials(kind=llm_key)。

    kit 的 compose 把 LLM_BASE_URL 和 LLM_API_KEY 写成必填（``${VAR:?缺少 …}``），
    少一个容器直接拒启（2026-10-21 现网第 60 步就是这么炸的：报错说容器里的
    ``$LLAMA_API_KEY`` 没设置，那是 compose 改名映射后的容器视角，要补的是实例
    ``.env`` 里的 ``LLM_API_KEY``，见 kit 的 docs/02 第 2 节）。
    """
    base = ctx.settings.llm_base_url
    if not base:
        raise StepFailed(
            "站点没配 llm_base_url：kit 的 compose 里 LLM_BASE_URL 必填，缺了容器起不来。"
            "补站点配置（deploy/site.toml 的 [site].llm_base_url）后重跑开通")
    cred = await repo.get_credential_by_kind(ctx.s, "llm_key")
    secret = (cred.secret or {}) if cred else {}
    api_key = str(secret.get("api_key") or "").strip()
    if not api_key:
        raise StepFailed(
            "站点没登记模型密钥：credentials 里需要一条 kind=llm_key 且 api_key 非空。"
            "compose 里 LLM_API_KEY 必填，缺了容器起不来（密钥只在第 60 步经 stdin "
            "写进实例 .env，不进命令行、不进日志）")
    return {"base_url": base, "model_id": ctx.settings.llm_model_id,
            "api_key": api_key,
            "search_key": str(secret.get("search_api_key") or "").strip()}


async def _reuse_or_pick_port(ctx: JobCtx, names: Any, host: Any) -> tuple[int, Any | None]:
    """续建先认出自己的隧道，再考虑挑新端口；返回 (端口, 复用到的隧道)。

    端口探测器只知道"4894 被占了"，不知道占着它的就是我们自己上次建的那条隧道，
    于是每次续建都往上加一个端口，接着被 ensure 的「隧道已存在但端口不一致，拒绝
    改动既有隧道」挡死（2026-10-21 现网：4893 别人在用、4894 是我们自己的 dsh_01、
    4895 是新挑的）。指向别的机器的隧道一律拒绝复用，那种才是真冲突。
    """
    if _manual(ctx, "frp"):
        return await _pick_frp_port(ctx), None
    prior = await ctx.frp().find_by_name(names.tunnel, host.frpc_client_id)
    if prior is None:
        return await _pick_frp_port(ctx), None
    lip = str(prior.local_ip or "")
    if lip and lip != host.ssh_host:
        raise StepFailed(
            f"隧道 {names.tunnel} 已存在且指向 {lip}:{prior.local_port}，与本机 "
            f"{host.ssh_host} 不符——那是别的服务，不复用也不改")
    await ctx.log("info", f"隧道 {names.tunnel} 是先前给这台实例建的"
                          f"（proxy_id={prior.proxy_id}，remotePort={prior.remote_port}），"
                          "沿用它的端口，不再新挑")
    return int(prior.remote_port or 0), prior


# 公网探活的重试口径：容器刚起来的几秒里首页可能还回 401（静态壳未就绪），
# DNS 也可能刚建还没生效。单次探活会把"还没准备好"读成"路径坏了"——现网
# dsh-02 就是 status=401 verdict=unexpected，事后从公网实测一切正常。
_PROBE_TRIES = 5
_PROBE_GAP = 6.0


async def env_facts(ctx: JobCtx, slug: str) -> dict[str, str]:
    """把实例目录里 .env 的**生效值**读回来（只取白名单键）。

    配置快照原本在第 10 步按猜测写：端口只能填 0，数据目录按 data_root 拼。
    现网详情页因此写着 CONTAINER_PORT=0、DOCUMENTS_DIRECTORY=/mnt/hdd2/dsh-family/
    dsh-02，而她真正的数据在 /mnt/hdd2/dsh-family/dsh-family/dsh-02/data——
    kit 的公式是 DATA_ROOT/dsh-family/<名>/data，拼出来的和落盘的不是一回事。
    密钥类键名一律不取：这行命令的 stdout 会回到本进程，快照要写进库。
    """
    env_path = f"{ctx.host.kit_path}/instances/{slug}/.env"
    keys = ("HOST_PORT", "DATA_DIR", "DSH_IMAGE", "PUBLIC_URL", "TRUSTED_HOSTS",
            "LLM_MODEL_ID")   # 模型名是页面要讲清的事实，不是密钥
    grep = " ".join(f"-e '^{k}='" for k in keys)   # 空格拼接（写成 " | " 会把 -e 当命令执行）
    r = await ctx.ssh(f"cat {shlex.quote(env_path)} 2>/dev/null | grep {grep}",
                      timeout=60, quiet=True)
    out: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in keys:
            out[k] = v.strip().strip('"').strip("'")
    return out


PORT_MAP_SNIPPET = """
docker inspect --format '{{range $p, $b := .HostConfig.PortBindings}}\
{{$p}}={{(index $b 0).HostPort}} {{end}}' __NAME__ 2>/dev/null || echo absent
"""


async def _port_map(ctx: JobCtx, container: str) -> tuple[int, int]:
    """返回 (容器内监听端口, 宿主机发布端口)，读的是 docker 自己的端口映射。

    现网第 27 个缺陷：第 65 步把 .env 里的 HOST_PORT 当成"容器内监听端口"写进
    快照，详情页因此把 4895 标在容器上。真值是 docker inspect 里的
    `8080/tcp -> 0.0.0.0:4895`——容器监听什么端口只有 docker 知道，
    kit 的 .env 里那个键名连它自己都不守（dsh-01 写 8080，dsh-02 写 4895）。
    """
    cmd = PORT_MAP_SNIPPET.replace("__NAME__", shlex.quote(container))
    r = await ctx.ssh(cmd, timeout=60, quiet=True)
    out = (r.stdout or "").strip()
    if not out or out == "absent" or "{" in out:   # 模板没渲染成功也不许当真值
        return (0, 0)
    for part in out.split():
        left, _, right = part.partition("=")
        inner = left.split("/")[0].strip()
        if inner.isdigit():
            return (int(inner), int(right) if right.strip().isdigit() else 0)
    return (0, 0)


_PROBE_MEANING = {
    "ok": "公网通了，反代 Host 头形状正确",
    "protected": "公网通了，首页也要登录（准入墙生效）",
    "host_origin_mismatch": "Host 与 Origin 又不相等（反代把端口拼进了 Host）",
    "auth_open": "没带 token 的受保护接口被放行（准入墙没生效）",
    "unreachable": "公网连不上（DNS/反代/frp/容器逐层查）",
    "unexpected": "回包不像 DSH（确认门后面是谁）",
}

async def run_deploy(ctx: JobCtx) -> None:
    inst = ctx.instance
    host = ctx.host
    slug = inst.slug
    names = derive(slug, ctx.settings.dns_zone)
    rr = dns_rr(slug)
    dns_name = inst.dns_name or names.server_name
    public_url = inst.public_url or f"https://{dns_name}"
    data_dir = f"{host.data_root.rstrip('/')}/{slug}"

    # 回滚兜底。runner 按 reversed() 倒序执行，这里最先 append 就最后执行：
    # 外部对象都删完之后，把库里指向它们的登记 id 抹掉——死 id 留着就是假凭据
    # （页面显示一条不存在的隧道，后续释放还拿死 id 去删，实测卡死过释放）。
    # 只清**本次 job 写过**的字段，且跳过 kept_fields（续建时按名字挂回去的既有
    # 对象）：上一次尝试建的东西还活着，一次失败的续建没资格清它们的登记。
    baseline = {f: getattr(inst, f, None) for f in EXTERNAL_ID_FIELDS}

    def _fields_written_by_this_job() -> set[str]:
        return {f for f, old in baseline.items()
                if getattr(inst, f, None) != old and f not in ctx.kept_fields}

    ctx.rollback_actions.append(
        ("external_ids_clear", lambda: clear_external_ids(
            ctx, inst, reason="开通失败已回滚（仅本次写入的登记）",
            only=_fields_written_by_this_job()))
    )

    # ---- 10 plan：slug 已在建实例时生成；去 DNS 与库确认未被占用（无副作用）
    async with ctx.step(10, "plan", "域名标签查重") as st:
        # NG 回源地址必须站点显式声明（DSHC_FRPS_UPSTREAM_HOST / site.toml）：
        # 代码里不许猜、不许回落 ingress_ip——云上无 NAT 回环，公网 IP 在入口机上不通。
        frps_host = ctx.settings.require("frps_upstream_host")
        image = ctx.settings.require("dsh_image")
        # 443 必须挂证书：面板里 ssl=0 的 443 站点拼出来的配置 nginx 校验不过，
        # 而且公网地址是 https，不挂证书等于建成就用不了。用现成通配证书，不签新的。
        ctx.settings.require("wildcard_cert_name")
        await ctx.log("info", f"slug={slug} 域名={dns_name} 容器={names.container}")
        await ctx.log("info", f"NG 挂现成通配证书 {ctx.settings.wildcard_cert_name}"
                              "（443 不挂证书的面板配置过不了 nginx -t；本站不签新证书）")
        await ctx.log("info", f"镜像（准备好的版本，开通阶段永不构建）：{image}"
                              "——20 步会在目标主机上核验它在位")
        await ctx.log("info", f"NG 将以 {frps_host} 回源（frps 监听地址，"
                              "需 NG 容器视角可达；本站若 NG 为 host 网络通常填 127.0.0.1）")
        await _llm_material(ctx)
        await ctx.log("info", "模型 provider 已就位："
                              f"{ctx.settings.llm_base_url}"
                              + (f"，模型 {ctx.settings.llm_model_id}"
                                 if ctx.settings.llm_model_id else "（模型用 kit 默认）")
                              + "；密钥从 credentials(kind=llm_key) 取，"
                                "只经 stdin 写入实例 .env")
        if _manual(ctx, "dns"):
            await ctx.log("info", f"DNS 层为 noop：请自行确认 {rr} 记录未被占用")
        else:
            existing = await ctx.dns().find(rr)
            if existing is not None:
                rid = str(existing.record_id or "")
                ours = bool(inst.dns_record_id) and rid == str(inst.dns_record_id)
                # 名字对得上不足以证明是我们的（域名由 slug 推出来，谁手动建一条
                # 同名记录都长这样）。再看**记录值**：指向本站入口 IP 的那条才是
                # 本控制台建的，续建直接复用；值不一样就是别人在用——不复用，
                # 也绝不删。2026-10-21 就是这里判错，把用户自己的实例永久锁死在
                # error，同时差点让他去删别人的解析。
                ingress = ctx.settings.require("ingress_ip")
                same_target = str(existing.value or "").strip() == ingress
                if ours or same_target:
                    if rid:
                        inst.dns_record_id = rid
                    ctx.kept_fields.add("dns_record_id")
                    await ctx.log("info", f"DNS 记录 {rr} → {existing.value} 复用"
                                          f"（RecordId={rid}；记录值与本站入口一致，"
                                          "判为本控制台先前所建）")
                else:
                    raise StepFailed(
                        f"DNS 里已存在记录 {rr}.{ctx.settings.dns_zone} "
                        f"(RecordId={existing.record_id})，指向 {existing.value}，"
                        f"与本站入口 {ingress} 不一致 → 不是本控制台建的：不复用、"
                        "也不删（那是别人的解析）。换标签重新创建实例。")
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
            # 数据根目录归控制台建：缺目录时 df 报错、第 60 步挂载也会失败，
            # 不该让人先手动 mkdir 才能开通（零输入要求）。
            f"mkdir -p '{host.data_root}' && test -w '{host.data_root}' "
            f"&& echo '[dir] writable'; "
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
            ingress = ctx.settings.require("ingress_ip")
            found = await ctx.dns().find(rr)
            # 续建幂等：同名同值的 A 记录就是本控制台上次给这台实例建的，复用即可。
            # 重复 create 只会往 DNS 里堆记录，阿里云还会直接报 AlreadyExists
            # （2026-10-21 现网续建就卡在这儿）。
            reuse = found is not None and str(found.value or "").strip() == ingress
            rec = found if reuse else await ctx.dns().create(rr, ingress, ttl=600)
            if reuse:
                ctx.kept_fields.add("dns_record_id")
                await ctx.log("info", f"DNS 已存在 {rr} A -> {found.value}"
                                      f"（RecordId={found.record_id}），复用不重建")
                await ctx.event("dns_reused", f"复用 A 记录 {rr} -> {found.value}",
                                {"record_id": found.record_id, "rr": rr})
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
            if not st_skip_dns and not reuse:
                # 复用来的记录不进回滚：不是这次建的，就没资格删。
                ctx.rollback_actions.append(
                    ("dns_delete", _rollback_del(
                        ctx, inst, "DNS 记录",
                        lambda rid=rec.record_id, r=rr: idempotent_delete(
                            ctx.log, "DNS 记录", r,
                            lambda: ctx.dns().delete(rid),
                            lambda: ctx.dns().find(r)),
                        ("dns_record_id",)))
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
        port, prior = await _reuse_or_pick_port(ctx, names, host)
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
            # 复用到上次建的隧道就不新建：ensure 见到端口不一致会拒改既有隧道，
            # 那是它对别人的保护，我们自己本来就不该走到那一步。
            tunnel = prior if prior is not None else await ctx.frp().ensure(
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
                client_id = host.frpc_client_id

                async def frp_still_there(pid: int = tunnel.proxy_id,
                                          n: str = names.tunnel) -> object:
                    # 与 release 第 30 步同款双探测：id 还见得到东西就拒判"已删除"
                    got = await ctx.frp().get(pid)
                    return got if got is not None else await ctx.frp().find_by_name(
                        n, client_id)

                if prior is None:  # 复用来的隧道不是这次建的，回滚没资格删
                    ctx.rollback_actions.append(
                        ("frp_delete", _rollback_del(
                            ctx, inst, "frp 隧道",
                            lambda pid=tunnel.proxy_id, n=names.tunnel: idempotent_delete(
                                ctx.log, "frp 隧道", n,
                                lambda: ctx.frp().delete(pid, expect_name=n),
                                frp_still_there),
                            ("frp_proxy_id", "frp_remote_port")))
                )
                inst.frp_proxy_id = tunnel.proxy_id
                inst.frp_remote_port = tunnel.remote_port or port
                # 容器发布端口与隧道 remotePort 同号，全局唯一
                inst.container_port = port
                if prior is not None:
                    # 挂回来的既有隧道：本次回滚一律不许抹它的登记
                    ctx.kept_fields.update({"frp_proxy_id", "frp_remote_port"})
                await ctx.s.commit()
                await ctx.event("frp_reused" if prior is not None else "frp_created",
                                f"{'复用隧道' if prior is not None else '隧道'} "
                                f"{tunnel.name} 远程端口 {inst.frp_remote_port}",
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
                cert_name=ctx.settings.require("wildcard_cert_name"),
            )
            st.detail = (
                "NG provider = noop，需人工配置反向代理：\n"
                f"  upstream {names.upstream}: "
                f"{ctx.settings.require('frps_upstream_host')}"
                f":{inst.frp_remote_port}\n"
                f"  server_name {dns_name}，location / 反代到该 upstream，"
                "headerHost=$host，websocket=true\n"
                f"  证书：{ctx.settings.wildcard_cert_name}"
                "（443 必须挂证书，否则面板配置过不了 nginx -t）\n"
                "（headerHost 带端口会让 /api/remote.mux 403，见 docs/site/NG-API.md）"
                + _drained(ctx, "ng")
            )
            await ctx.s.commit()
        else:
            site = await ctx.ng().ensure_site(
                server_name=dns_name, upstream_name=names.upstream,
                upstream_server=f"{ctx.settings.require('frps_upstream_host')}"
                                f":{inst.frp_remote_port}",
                listen_port=443, header_host="$host", websocket=True,
                cert_name=ctx.settings.require("wildcard_cert_name"),
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
                # upstream 删除必须排在站点删除**之后**执行；runner 倒序跑
                # rollback_actions，所以 append 顺序是反的：先 upstream 后站点。
                if not _synthetic_id(site.upstream_id):
                    ctx.rollback_actions.append(
                        ("ng_upstream_delete", _rollback_del(
                            ctx, inst, "NG upstream",
                            lambda uid=site.upstream_id, un=names.upstream: delete_ng_upstream(
                                ctx.ng(), uid, un, ctx.log),
                            ("ng_upstream_id", "ng_upstream_name")))
                    )
                ctx.rollback_actions.append(
                    ("ng_delete", _rollback_del(
                        ctx, inst, "NG 站点",
                        lambda sid=site.server_id, n=dns_name: idempotent_delete(
                            ctx.log, "NG 站点", n,
                            lambda: ctx.ng().delete_site(sid, expect_name=n),
                            lambda: ctx.ng().find_site(n)),
                        ("ng_server_id", "ng_location_id")))
                )
                await _pre_replace_drift_check(ctx, dns_name)
                await ctx.ng().reload()  # ensure_site 不会自动 reload（对接要点 1）
                inst.ng_upstream_name = names.upstream
                inst.ng_upstream_id = (None if _synthetic_id(site.upstream_id)
                                       else site.upstream_id)
                inst.ng_server_id = site.server_id
                inst.ng_location_id = site.location_id
                await ctx.s.commit()
                await ctx.event("ng_created", f"站点 {dns_name} server_id={site.server_id}",
                                {"server_id": site.server_id, "upstream": names.upstream,
                                 "upstream_id": site.upstream_id,
                                 "location_id": site.location_id})

    # ---- 60 container_deploy：跑 kit 的 new-instance.sh + deploy.sh。
    # 契约：此步失败「保留现场，标 error」——从这里开始不再回滚已建对象。
    async with ctx.step(60, "container_deploy", "主机上部署容器（kit 脚本）"):
        ctx.rollback_actions.clear()
        kit = host.kit_path
        inst_dir = f"{kit}/instances/{slug}"
        env_file = f"{inst_dir}/.env"
        port = int(inst.container_port or 0)
        # kit 的接口是**位置参数**：new-instance.sh <名字> <宿主机端口>；它只认
        # DATA_ROOT / LAN_IP 两个环境变量，别的一概不读。此前我们把端口塞进
        # CONTAINER_PORT 环境变量，脚本当场报「用法：… <宿主机端口>」（现网实测）。
        # 目录已存在就不再重建：续建要沿用上次建好的实例目录（幂等续跑）。
        r = await ctx.ssh(
            f"if [ -d {shlex.quote(inst_dir)} ]; then "
            f"echo '[kit] 实例目录已存在，续建沿用'; "
            f"else cd {shlex.quote(kit)} && DATA_ROOT={shlex.quote(host.data_root)} "
            f"./scripts/new-instance.sh {shlex.quote(slug)} {port}; fi", timeout=600)
        if not r.ok:
            raise StepFailed(f"new-instance.sh 失败: {r.tail()}")
        for line in (r.stdout or "").splitlines():
            if line.strip():
                await ctx.log("info", f"[kit] {line.strip()}"[:200])
        # 公网那两条 kit 不管（它只预填局域网白名单）：TRUSTED_HOSTS 少了域名，
        # DSH 准入墙会把 /api 判 403；PUBLIC_URL 缺了，容器日志里打的一键登录地址
        # 就是局域网地址，抓到的 token 拼出的链接对外没用。镜像也在这里落到 .env。
        llm = await _llm_material(ctx)  # 现取现用：中途可能换密钥
        patch = "\n".join([
            "set -e",
            f"f={shlex.quote(env_file)}",
            'th="$(sed -n "s/^TRUSTED_HOSTS=//p" "$f" | tail -1)"',
            f'case ",$th," in ",{dns_name},"*) : ;;',
            f'*) sed -i "s|^TRUSTED_HOSTS=.*|TRUSTED_HOSTS=$th,{dns_name}|" "$f" ;;',
            "esac",
            (f'if grep -q "^PUBLIC_URL=" "$f"; then '
             f'sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL={public_url}|" "$f"; '
             f'else printf "PUBLIC_URL=%s\\n" {shlex.quote(public_url)} >> "$f"; fi'),
            (f'if grep -q "^DSH_IMAGE=" "$f"; then '
             f'sed -i "s|^DSH_IMAGE=.*|DSH_IMAGE={image}|" "$f"; '
             f'else printf "DSH_IMAGE=%s\\n" {shlex.quote(image)} >> "$f"; fi'),
            # 值可能含 / & 等 sed 分隔符，一律"删行 + 追加"，不用 sed 替换
            ("put() { grep -v '^$1=' \"$f\" > \"$f.tmp\" || true; "
             "mv \"$f.tmp\" \"$f\"; printf '%s=%s\\n' \"$1\" \"$2\" >> \"$f\"; }"),
            f'put LLM_BASE_URL {shlex.quote(llm["base_url"])}',
            # 密钥从 stdin 进来：命令行会进任务日志，也在目标机 ps 里裸奔
            "read -r llm_api_key",
            "read -r search_api_key",
            'put LLM_API_KEY "$llm_api_key"',
            'if [ -n "$search_api_key" ]; then put SEARCH_API_KEY "$search_api_key"; fi',
            (f'put LLM_MODEL_ID {shlex.quote(llm["model_id"])}'
             if llm["model_id"] else ":"),
            # 回显白名单里刻意没有 LLM_API_KEY / SEARCH_API_KEY
            ('grep -E "^(HOST_PORT|TRUSTED_HOSTS|PUBLIC_URL|DSH_IMAGE|DATA_DIR'
             '|LLM_BASE_URL|LLM_MODEL_ID)=" "$f"'),
            ('if grep -q "^LLM_API_KEY=." "$f"; then echo "LLM_API_KEY 已写入（值不回显）"; '
             'else echo "LLM_API_KEY 缺失"; exit 1; fi'),
        ])
        r = await ctx.ssh(f"sh -c {shlex.quote(patch)}", timeout=120,
                          stdin=f"{llm['api_key']}\n{llm['search_key']}\n")
        if not r.ok:
            raise StepFailed(f"给实例 .env 写公网/模型配置失败: {r.tail()}")
        for line in (r.stdout or "").splitlines():
            if line.strip():
                await ctx.log("info", f"[env] {line.strip()}"[:200])
        r = await ctx.ssh(
            f"cd {shlex.quote(kit)} && ./scripts/deploy.sh {shlex.quote(slug)}", timeout=900)
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

    # ---- 65 config_backfill：把第 10 步那份"开工前的猜测"换成机器上的事实
    async with ctx.step(65, "config_backfill", "回填生效配置") as st:
        facts = await env_facts(ctx, slug)
        if not facts:
            # 读不回事实不该让开通失败（容器已经起来了），但也不许把估算值当事实
            # 摆在详情页上：快照里明写来源，页面上就看得见这是开工前的推测。
            await ctx.log("warn", "读不到实例 .env，配置快照沿用开通前的估算值（已在快照里标注）")
        cport, hport = await _port_map(ctx, inst.container_name)
        if facts.get("HOST_PORT") and hport and str(hport) != str(facts["HOST_PORT"]):
            await ctx.log("warn", f"端口对不上：docker 映射的宿主机端口是 {hport}，"
                                  f"实例 .env 写的 HOST_PORT 是 {facts['HOST_PORT']}，"
                                  "详情页以 docker 的映射为准")
        rendered = default_rendered(
            slug=slug, public_url=public_url, trusted_hosts=[dns_name],
            # 容器内监听端口只认 docker 的端口映射；读不到就留 0 并在来源里写明，
            # 绝不拿 HOST_PORT 顶替（那是宿主机端口，两个数字含义不同）。
            container_port=cport or int(inst.container_port or 0),
            frp_remote_port=int(inst.frp_remote_port or port or 0),
            data_dir=facts.get("DATA_DIR") or data_dir,
            image_tag=facts.get("DSH_IMAGE") or image,
        )
        rendered["HOST_PORT"] = hport or int(facts.get("HOST_PORT") or 0)
        rendered["LLM_MODEL_ID"] = facts.get("LLM_MODEL_ID") or ""
        if facts and cport:
            rendered["CONFIG_SOURCE"] = "生效值（实例 .env + docker 端口映射）"
        elif facts or cport:
            rendered["CONFIG_SOURCE"] = "部分生效值（" +                 ("没读到 docker 端口映射）" if not cport else "没读到实例 .env）")
        else:
            rendered["CONFIG_SOURCE"] = "估算（未读到实例 .env 与端口映射）"
        await repo.add_config_snapshot(ctx.s, inst.id, CONFIG_VERSION, rendered)
        await ctx.s.commit()
        st.detail = (f"配置快照来源：{rendered['CONFIG_SOURCE']}；"
                     f"容器内 {rendered['CONTAINER_PORT']} ← 宿主机 "
                     f"{rendered['HOST_PORT']} ← 公网 {rendered['FRP_REMOTE_PORT']}；"
                     f"数据目录 {rendered['DOCUMENTS_DIRECTORY']}")

    # ---- 66 capabilities：铺生图技能与智能体团队，并读回 DSH 自己那套配置
    # 放在配置快照之后、抓 token 之前：技能铺完，快照里"她能用些什么"才是真的；
    # 智能体团队走 home 层补丁（官方留给个人例外的层），不重建镜像也能开。
    async with ctx.step(66, "capabilities", "铺生图技能与智能体团队") as st:
        rt = ctx.runtime()
        container = inst.container_name or names.container
        caps = await apply_capabilities(ctx, container)
        st.detail = f"{caps.get('team', '')}；{caps.get('image', '')}"[:500]
        dsh = await read_dsh_config(rt, container, dsh_version=inst.dsh_version or "")
        if dsh:
            cfg = await repo.latest_config(ctx.s, inst.id)
            merged = dict((cfg.rendered if cfg else None) or {})
            dsh = annotate_model_drift(dsh, merged.get("LLM_MODEL_ID"))
            merged["DSH"] = dsh
            await repo.add_config_snapshot(ctx.s, inst.id, CONFIG_VERSION, merged)
            st.detail += (f"；DSH 配置已读取：默认模型 {dsh['model']}，"
                          f"智能体团队{'已开' if dsh['agent_team'] else '未开'}，"
                          f"技能 {len(dsh['skills'])} 个")
        else:
            await ctx.log("warn", "读不到 DSH 配置（dsh web --dump-config 空返回），"
                                  "详情页的 DSH 配置卡会是空的，可以事后点「重新读取」")
        await ctx.s.commit()

    # ---- 70 token_capture：从容器日志抓一键地址（标 error，可重试）
    async with ctx.step(70, "token_capture", "从容器日志抓登录密钥"):
        tok = await capture_token(ctx.s, ctx.runtime(), inst, "bootstrap")
        await ctx.event("token_captured", "部署后抓到一键地址",
                        {"source": "bootstrap", "url": tok.url, "token": tok.token})

    # ---- 80 probe_public：首页 200 + 无 token 打 remote.mux 期望 401
    async with ctx.step(80, "probe_public", "公网探活") as st:
        res = None
        tries = 0
        for attempt in range(1, _PROBE_TRIES + 1):
            tries = attempt
            res = await ctx.probe().check(public_url)
            if res.ok:
                break
            if attempt < _PROBE_TRIES:
                await ctx.log("info", f"公网探活第 {attempt} 次没过"
                                      f"（{res.verdict}），{int(_PROBE_GAP)}s 后重试")
                await asyncio.sleep(_PROBE_GAP)
        st.detail = (f"{_PROBE_MEANING.get(res.verdict, res.verdict)}" f"（共探 {tries} 次，status={res.status_code}）" + (f"：{res.hint}" if res.hint else ""))
        if res.verdict == "host_origin_mismatch":
            st.detail += "\n提示：反代 location 的 Header Host 必须是 $host（不能带端口）"
        await ctx.s.commit()
        if not res.ok:
            hint = res.hint or "检查反代 location 的 Header Host 是否为 $host"
            # 契约：标 degraded + 修正提示。state 枚举里没有 degraded，
            # 用 event + step detail 承载，不影响部署成功判定。
            await ctx.event("probe_degraded", f"公网探活没通过：{_PROBE_MEANING.get(res.verdict, res.verdict)}",
                            {"status_code": res.status_code, "hint": hint})

    # ---- 90 verify：kit 的 verify.sh <slug>，收条目数，不推翻成功
    async with ctx.step(90, "verify", "安全门自检 + kit 校验脚本") as st:
        # 顺序刻意是先自检再跑脚本：安全门是控制台自己算的，不依赖 kit 脚本写法
        # （kit 那份在 pipefail 下出过假警报，见 domain/safety.py 与 kit 的提交）。
        gate = await ctx.ssh(
            f"docker exec {shlex.quote(inst.container_name or names.container)} sh -c "
            + shlex.quote('cd "${DSH_HOME:-/data}" && dsh web --dump-config'),
            timeout=180, quiet=True)  # 整份配置不进日志，只写一行结论
        bad = config_violations(gate.stdout or "", base_url=ctx.settings.llm_base_url)
        if bad:
            raise StepFailed("安全门不通过（花钱/外传这两条不外包给脚本）：\n  - "
                             + "\n  - ".join(bad))
        await ctx.log("info", f"安全门：{len(MUST_BE_DISABLED)} 条云端路径确认已断开，"
                              f"模型端点确认为 {ctx.settings.llm_base_url}")

        r = await ctx.ssh(f"cd {shlex.quote(host.kit_path)} && "
                          f"./scripts/verify.sh {shlex.quote(slug)}", timeout=300)
        warns = [ln.strip() for ln in (r.stdout or "").splitlines() if "\u2717" in ln]
        st.detail = (st.detail + "\n" if st.detail else "") + (
            "kit verify.sh 全项通过" if r.ok else
            f"kit verify.sh 有 {len(warns) or '?'} 项不通过（给人看的参考，"
            f"安全门已单独自检）:\n" + "\n".join(f"  {w}" for w in warns[:12]))
        if not r.ok:
            await ctx.event("verify_warnings",
                            f"kit verify.sh 提示 {len(warns) or '?'} 项: "
                            f"{'; '.join(warns[:5])[:400]}",
                            {"count": len(warns), "items": warns[:12]})
