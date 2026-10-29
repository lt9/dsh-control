"""deploy 状态机（契约 §6.1）。每步把命令、耗时、stdout 摘要写进 deploy_steps，
stdout/stderr 逐行进 deploy_logs。「失败怎么办」列实现为 rollback_actions。

provider 注册表（docs/SITE-CONFIG.md）：settings.provider_dns/frp/ng 为 `noop` 时，
对应步骤标 skipped 并把要人工做的事打印进日志——没有阿里云/没有 nginxWebUI 也能用。
站点事实（入口 IP、zone、端口起点）一律从 settings（site 段灌入）取，代码零硬编码。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from ..assets import install_fontlib, install_kit
from ..db import repo
from ..db.models import Credential, Instance
from ..domain.config_docs import CONFIG_VERSION, default_rendered
from ..domain.dsh_config import annotate_model_drift
from ..domain.frp_port import pick_direct_port, pick_free_port
from ..domain.safety import MUST_BE_DISABLED, config_violations
from ..domain.slug import derive, dns_rr
from ..images import relay_font_library
from ..integrations import memory_hub as memhub
from ..integrations.base import IntegrationError
from .capabilities import apply_capabilities, read_dsh_config
from .capture import capture_token
from .release import EXTERNAL_ID_FIELDS, clear_external_ids, delete_ng_upstream, idempotent_delete
from .runner import JobCtx, StepFailed, redact_text


def _ep_conn(ctx: JobCtx) -> dict[str, Any]:
    """本次 job 目标宿主机的高保真连接参数（给 assets 的原生 ssh 管道用——
    执行器通道是 JSON 文本的，装不下二进制流，见 images._relay 同一族注释）。"""
    ep = ctx.endpoint()
    return {"host": ep.host, "port": ep.port, "user": ep.user,
            "private_key_pem": ep.private_key_pem}


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


#: 「删行 + 追加」小助手：值可能含 / & 等 sed 分隔符，一律不用 sed 替换。
#: 引号是命门：原实现写的是 '^$1='（单引号），$1 在函数体内不展开，删行从来没
#: 删掉过——2026-10-22 现网发现 dsh 实例的 .env 里堆了 7 行 LLM_BASE_URL（每次
#: 开通追加一行）。compose 取最后一行才没炸生产，但垃圾一直吃进来。改双引号后
#: 每次写都真正清场，历史重复行也会被下一次开通顺手洗干净。
_PUT_FN = ('put() { grep -v "^$1=" "$f" > "$f.tmp" || true; '
           'mv "$f.tmp" "$f"; printf \'%s=%s\\n\' "$1" "$2" >> "$f"; }')


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


async def _reuse_or_pick_port(ctx: JobCtx, names: Any, host: Any,
                              inst: Any) -> tuple[int, Any | None]:
    """续建先认出自己的隧道，再考虑挑新端口；返回 (端口, 复用到的隧道)。

    端口探测器只知道"20002 被占了"，不知道占着它的就是我们自己上次建的那条隧道，
    于是每次续建都往上加一个端口，接着被 ensure 的「隧道已存在但端口不一致，拒绝
    改动既有隧道」挡死（2026-10-21 现网：起点端口别人在用、下一个是我们自己的另一条隧道、
    再下一个是新挑的）。指向别的机器的隧道一律拒绝复用，那种才是真冲突。

    恢复场景（§6.3.1）：隧道已随释放删掉，但库里 `frp_remote_port` 是特意保留不
    复用的——别的实例挑端口时永远避开它，正主回来先探一次占用再赎回，原公网端口
    与原域名一起回来（§9「原域名原端口回来」）。被占才回落原分配逻辑，并 event
    说破换了端口。
    """
    if not _manual(ctx, "frp"):
        prior = await ctx.frp().find_by_name(names.tunnel, host.frpc_client_id)
        if prior is not None:
            lip = str(prior.local_ip or "")
            if lip and lip != host.ssh_host:
                raise StepFailed(
                    f"隧道 {names.tunnel} 已存在且指向 {lip}:{prior.local_port}，与本机 "
                    f"{host.ssh_host} 不符——那是别的服务，不复用也不改")
            await ctx.log("info", f"隧道 {names.tunnel} 是先前给这台实例建的"
                                  f"（proxy_id={prior.proxy_id}，"
                                  f"remotePort={prior.remote_port}），"
                                  "沿用它的端口，不再新挑")
            return int(prior.remote_port or 0), prior
    saved = int(inst.frp_remote_port or 0)
    if saved:
        if not await ctx.frp().remote_port_taken(saved):
            await ctx.log("info", f"恢复：赎回原公网端口 :{saved}"
                                  "（释放时特意保留、其它实例永不复用）")
            return saved, None
        new_port = await _pick_frp_port(ctx)
        await ctx.log("warn", f"原公网端口 :{saved} 已被占用，改用 :{new_port}")
        await ctx.event("frp_port_fallback",
                        f"原公网端口 :{saved} 被占，改用 :{new_port}",
                        {"saved_port": saved, "new_port": new_port})
        return new_port, None
    return await _pick_frp_port(ctx), None


# ------------------------------------------------------ direct 主机端口（§6.1.4）


async def _direct_taken_probe(ctx: JobCtx, host: Any, inst: Any):
    """direct 主机的「端口被占」判据工厂：**探测在目标机上做**，不问 frp-panel。

    占用 = 本机 `ss -ltn` 实听的端口 ∪ 库里同机其它实例已发布的端口。
    一次 ssh 把整表拉回来本地判，别每个端口探一次（起点到找到最多 200 次 ssh）。
    """
    r = await ctx.ssh(
        "ss -ltn 2>/dev/null | awk 'NR>1{print $4}' | sed 's/.*[:.]//' ",
        timeout=30)
    listening: set[int] = set()
    for tok in (r.stdout or "").split():
        if tok.isdigit():
            listening.add(int(tok))
    if not r.ok and not listening:
        # ss 缺失/无权限：只剩库里那半边事实，探测弱了一半——说破，别装没看见
        await ctx.log("warn", "本机 ss -ltn 实探失败，端口占用只认库里登记（请核对目标机）")
    db_ports = await repo.host_in_use_ports(ctx.s, host.id,
                                            exclude_instance_id=inst.id)
    taken = listening | db_ports

    async def is_taken(port: int) -> bool:
        return port in taken

    return is_taken


async def _pick_direct_port(ctx: JobCtx, host: Any, inst: Any) -> int:
    try:
        return await pick_direct_port(
            ctx.s, ctx.settings, await _direct_taken_probe(ctx, host, inst),
            log=lambda m: ctx.log("info", m))
    except RuntimeError as exc:
        raise StepFailed(str(exc)) from exc


async def _own_container_holds_port(ctx: JobCtx, inst: Any, port: int) -> bool:
    """监听 saved 端口的是不是**它自己的容器**——running 直连机重跑 deploy
    （切镜像）时旧容器还没让出端口，ss 会把它读成"占用"，那是自己吓自己：
    compose 重建会先释放再绑回，端口照旧赎回。"""
    name = (inst.container_name or "").strip()
    if not name:
        return False
    r = await ctx.ssh(
        "docker inspect -f "
        "'{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostPort}} "
        "{{end}}{{end}}' " + shlex.quote(name) + " 2>/dev/null || true", timeout=60)
    return str(port) in (r.stdout or "").split()


async def _direct_port(ctx: JobCtx, host: Any, inst: Any) -> int:
    """direct 主机的公网端口：续建/恢复先赎回原端口（本机没在听就当可用），
    没有原端口或被占才新挑。frp_remote_port 列名沿用不改，就是本机发布端口。"""
    saved = int(inst.frp_remote_port or 0)
    if not saved:
        return await _pick_direct_port(ctx, host, inst)
    is_taken = await _direct_taken_probe(ctx, host, inst)
    if not await is_taken(saved):
        await ctx.log("info", f"恢复：赎回原本机公网端口 :{saved}"
                              "（释放时特意保留、其它实例永不复用）")
        return saved
    if await _own_container_holds_port(ctx, inst, saved):
        await ctx.log("info", f"端口 :{saved} 正被这台实例自己的容器占着"
                              "（compose 重建会让出再绑回），照常赎回")
        return saved
    new_port = await _pick_direct_port(ctx, host, inst)
    await ctx.log("warn", f"本机公网端口 :{saved} 已被占用，改用 :{new_port}")
    await ctx.event("frp_port_fallback",
                    f"本机原公网端口 :{saved} 被占，改用 :{new_port}",
                    {"saved_port": saved, "new_port": new_port, "mode": "direct"})
    return new_port


# ------------------------------------------------- 缺镜像自动搬运（§6.1.5 三岔口）

_SHIP_LOAD_TIMEOUT = 1800   # GB 级制品下载 + load 的超时口径（与 publish save 同量级）


def _have_dsh_images(stdout: str) -> str:
    """把 docker images 探测输出里的现有 dsh 标签攒成一行人话（报错附录用）。"""
    have = "; ".join(ln.strip() for ln in (stdout or "").splitlines()
                     if ln.strip() and "[image]" not in ln)
    return have or "（无 dsh 相关镜像）"


async def _image_present(ctx: JobCtx, h: Any, image: str) -> bool:
    """问任意一台主机有没有这个镜像——ctx.ssh 只到目标机，挑源机得自己发探测。"""
    pem = ""
    if h.ssh_credential_id:
        cred = await ctx.s.get(Credential, h.ssh_credential_id)
        pem = (cred.secret or {}).get("private_key_pem", "") if cred else ""
    if not pem:
        return False
    r = await ctx.executor().run(
        host=h.ssh_host, port=h.ssh_port, user=h.ssh_user, private_key_pem=pem,
        command=f"docker image inspect '{image}' >/dev/null 2>&1 "
                "&& echo '[image] PRESENT' || echo '[image] MISSING'",
        timeout=60)
    return "[image] PRESENT" in (r.stdout or "")


async def _ship_missing_image(ctx: JobCtx, host: Any, image: str, have: str) -> None:
    """第 20 步缺镜像分支（§6.1.5）：download 拉制品 / ssh 流式直传 / 报错点名。

    成功=返回（镜像已在目标机）；办不到必须 StepFailed——「没镜像照跑」不存在。
    """
    ship = await repo.current_image_ship(ctx.s, default_base=ctx.settings.public_base)
    mode = ship["mode"]
    base = ship["base_url"]
    # 按主机登记的下载基址优先：入口机拨自家公网域是 hairpin 黑洞（§6.1.5）
    host_base = (getattr(host, "image_base_url", "") or "").strip().rstrip("/")
    if host_base:
        base = host_base
    art = await repo.get_artifact_by_tag(ctx.s, image)
    if mode == "download" and art is not None:
        # ---- §6.1.6 探测选路：候选按 mirror_order 排，外部渠道只认渠道行 ok，
        # console 恒在（现行为）。探测必须在目标机上跑——面板机装了梯子不算数。
        cache_dir = f"{host.data_root}/ship-cache"
        dest = f"{cache_dir}/{art.filename}"
        cands: list[tuple[str, str]] = []
        for ch in ship["mirror_order"]:
            if ch == "console":
                if base:   # base 都没配就没有 console 入口，这一路不存在
                    cands.append(("console", f"{base}/dl/{art.token}/{art.filename}"))
            else:
                row = await repo.get_channel(ctx.s, art.id, ch)
                if row is not None and row.state == "ok" and row.url:
                    cands.append((ch, row.url))
        if not cands:
            raise StepFailed(
                f"分发方式=download，制品 {image} 存在但没有任何可用下载入口："
                "console 基址没配（base_url/public_base 都空）、外部渠道没有 "
                "state=ok 的行。去设置页配好基址，或发布时把渠道推成功。")
        if host_base:
            # 主机显式登记了 image_base_url（局域网/回环这类近端入口）=运维的
            # 就近意图：console 排最前，外部渠道退居备胎（稳定排序，其余相对序
            # 不动）。实测教训：VPS 拉 gitee CDN 206s、自家隧道只要 53s；文件
            # 就在主机本地时（127.0.0.1 基址）更不该绕外网一圈。
            cands.sort(key=lambda c: c[0] != "console")
        picked: tuple[str, str] | None = None
        tried: dict[str, str] = {}
        for ch, url in cands:
            r = await ctx.ssh(f"curl -fsL --max-time 8 -r 0-0 -o /dev/null "
                              f"'{url}' && echo OK", timeout=30)
            if r.ok and "OK" in (r.stdout or ""):
                picked = (ch, url)
                break
            tried[ch] = "不可达"
        if picked is None:
            detail = " / ".join(f"{ch}: {why}" for ch, why in tried.items())
            raise StepFailed(f"{host.name} 把制品的全部渠道都探测失败"
                             f"（{detail}）——渠道是目标机自己拨的，不是面板代拨；"
                             "查该机出网，或换分发方式/渠道。")
        ch, url = picked
        skipped_probe = "、".join(tried) or "无"
        await ctx.log("info", f"缺镜像：选路结果 = {ch}（探测顺序内第一个可达；"
                              f"前面不通：{skipped_probe}）")
        if ch == "gitee":
            # gitee 三连：manifest → 按序拼卷（>>）→ **整包 sha 强校验** → load。
            # 抽直链用 grep -o '"url":"http…"'——manifest 是我们生成的单行 JSON
            # （separators 无空格、无嵌套转义），纯 POSIX 就够，不赌 jq/python3。
            mani = f"{cache_dir}/manifest.json"
            script = (
                f"mkdir -p '{cache_dir}' && rm -f '{dest}' && "
                f"curl -fL --retry 3 '{url}' -o '{mani}' && "
                f"""for u in $(grep -o '"url":"http[^"]*"' '{mani}' """
                f"""| sed 's/^"url":"//; s/"$//'); do """
                f'curl -fL --retry 3 "$u" >> \'{dest}\' || exit 1; done && '
                f"echo '{art.sha256}  {dest}' | sha256sum -c - && "
                f"docker load -i '{dest}' && rm -rf '{cache_dir}'")
            got = "gitee 分卷拼回"
        else:
            # 契约三连一字不差：curl 下载 → sha256sum -c → docker load → 删缓存。
            # 缓存放 {data_root}/ship-cache：不赌 /tmp 是不是 tmpfs。
            script = (
                f"mkdir -p '{cache_dir}' && "
                f"curl -fL --retry 3 '{url}' -o '{dest}' && "
                f"echo '{art.sha256}  {dest}' | sha256sum -c - && "
                f"docker load -i '{dest}' && rm -f '{dest}'")
            got = f"{ch} 整包"
        r = await ctx.ssh(script, timeout=_SHIP_LOAD_TIMEOUT)
        if not r.ok:
            raise StepFailed(f"目标机经 {ch} 拉取或加载制品失败（{got}，"
                             f"含 sha256sum -c/docker load）:\n{r.tail()}")
        if not await _image_present(ctx, host, image):
            raise StepFailed(f"docker load 回执正常但复查 {host.name} 上仍无 {image}"
                             "——load 进去的不是这个 tag")
        await ctx.event("image_shipped", f"{image} 经 {ch} 渠道拉取并加载",
                        {"image": image, "via": ch, "probe": "target-host",
                         "origin_host": art.origin_host})
        return
    if mode == "ssh":
        # enabled 主机按 id 升序找有该镜像的第一台（契约 §6.1.5；面板所在宿主机
        # 通常就是最先登记的那台，id 升序天然优先）。
        cands = sorted((h for h in await repo.list_hosts(ctx.s)
                        if h.enabled and h.id != host.id), key=lambda h: h.id)
        for cand in cands:
            if not await _image_present(ctx, cand, image):
                continue
            await ctx.log("info", f"缺镜像：{cand.name} 有 {image}，SSH 流式直传到 "
                                  f"{host.name}（不落盘，两头之间不需要互信）")
            from ..images import ship_image  # 局部 import：不进模块级依赖面
            try:
                out = await ship_image(ctx.db, ctx.providers, tag=image,
                                       src_ref=cand.name, dst_ref=host.name)
            except RuntimeError as exc:
                raise StepFailed(f"SSH 直传失败: {exc}") from exc
            if not await _image_present(ctx, host, image):
                raise StepFailed(f"直传报成功但复查 {host.name} 上仍无 {image}")
            await ctx.log("info", f"搬运完成：{out['bytes']} 字节 / {out['seconds']}s")
            await ctx.event("image_shipped",
                            f"{image} 经 SSH 从 {cand.name} 流式直传到 {host.name}",
                            {"image": image, "via": "ssh", "from_host": cand.name})
            return
        raise StepFailed(
            f"{host.name} 上没有镜像 {image}，也没有别的 enabled 主机有它"
            f"（SSH 直传没有源可搬）。着手：先 cli images ship 把准备好的版本弄到"
            f"任一 enabled 主机（cli images ship --tag {image} "
            f"--from <有此镜像的主机> --to <enabled 主机>），"
            f"或去设置页发布该 tag 的制品并把分发方式切到 download。"
            f"（目标机现有 dsh 镜像：{have}）")
    # mode=download 但库里没这个 tag 的制品
    raise StepFailed(
        f"{host.name} 上没有镜像 {image}，制品库里也没有它的制品"
        "（当前分发方式=download）。着手：去设置页（镜像制品）从一台有此镜像的"
        "机器发布该 tag 的制品，或把分发方式切回 SSH 直传。"
        f"（目标机现有 dsh 镜像：{have}）")


# 公网探活的重试口径：容器刚起来的几秒里首页可能还回 401（静态壳未就绪），
# DNS 也可能刚建还没生效。单次探活会把"还没准备好"读成"路径坏了"——现网
# dsh-02 就是 status=401 verdict=unexpected，事后从公网实测一切正常。
_PROBE_TRIES = 5
_PROBE_GAP = 6.0


async def env_facts(ctx: JobCtx, slug: str) -> dict[str, str]:
    """把实例目录里 .env 的**生效值**读回来（只取白名单键）。

    配置快照原本在第 10 步按猜测写：端口只能填 0，数据目录按 data_root 拼。
    现网详情页因此写着 CONTAINER_PORT=0、DOCUMENTS_DIRECTORY=/srv/dsh/dsh-family/
    dsh-02，而她真正的数据在 /srv/dsh/dsh-family/dsh-02/data——
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
    快照，详情页因此把宿主机端口标在容器上。真值是 docker inspect 里的
    `8080/tcp -> 0.0.0.0:20002`——容器监听什么端口只有 docker 知道，
    kit 的 .env 里那个键名连它自己都不守（有的实例写 8080，有的写宿主机发布端口）。
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

# ---- 铺字体（第 67 步）：投递口 = 实例 DATA_DIR 的 home/.fonts（容器内 /data/home/.fonts）。
# Word→PDF 有两条转换路，各自认各自的目录：右侧栏预览的 office-to-pdf 插件读
# fontDirectories，官方 office 技能的 LibreOffice Kit CLI 只扫 $HOME/.fonts 默认根；
# home/.fonts 是两条路共同认的位置，面板根本不配 fontDirectories（替换语义，写了
# 会顶掉镜像里已配的目录，炸掉所有预览）。
_FONT_DST_REL = "home/.fonts"

_FONT_SKIP_NO_LIB = ("站点没配字体库（font_library 为空）：本次跳过铺字体，"
                     "实例其他功能不受影响")

_FONT_NEW_RE = re.compile(r"new=(\d+)")


def _font_cp_script(lib: str, dst: str) -> str:
    """宿主上铺字体的脚本：把库里内容 cp 进 dst，落地后文件 644、目录 755。

    - **`cp -n` 是防覆盖红线**：目标里已有的同名文件一律不动（她的字体她自管），
      所以重复部署（redeploy 切镜像续建）随便跑，新增 0 个也算正常完成；
    - **只 cp**：不 bind 挂载 /usr/share/fonts（挂载要改每台 compose，字体也不随
      数据备份搬家）、不跑 fc-cache（容器里 fontconfig 自己会扫）、不碰 compose；
    - 权限整目录刷 644/755：字体本就该全局可读，保证容器内 uid 读得到；
    - 库目录不存在只打标记退 0——「跳过还是失败」是编排层的判定，不发生在宿主上。
    """
    return "\n".join([
        "set -e",
        f"lib={shlex.quote(lib)}",
        f"dst={shlex.quote(dst)}",
        'if [ ! -d "$lib" ]; then echo "[fonts] LIB_MISSING"; exit 0; fi',
        'mkdir -p "$dst"',
        'before=$(find "$dst" -type f 2>/dev/null | wc -l)',
        'cp -rn "$lib"/. "$dst"/',
        'find "$dst" -type d -exec chmod 755 {} +',
        'find "$dst" -type f -exec chmod 644 {} +',
        'after=$(find "$dst" -type f | wc -l)',
        'echo "[fonts] DONE src=$lib dst=$dst new=$((after-before))"',
    ])


# ---------------------------------------------------------------- 记忆中枢接线
# （能力 memory-hub，kind=service：ADR-0001 决策二示例 + docs/site/MEMORY-P0.md）
#
# 位置在容器 healthy（60/65）之后、复核（80/90）之前：装的是配置层的东西，
# home 层补丁与 refs 都落在数据卷里，容器重建不丢；DSH 只在启动时组合一次配置，
# 所以「新铺了补丁」必须重启一次容器才生效——这一步自己负责重启与等健康。
#
# 缺料（无 [memory] 段 / admin key 没登记 / 栈不通）→ 整步 skipped + 一行警告 +
# 事件，**不挡开通**（先例：第 66 步生图技能）。running 实例上的启用/停用走
# kind="memory" 工单重跑同一套机器（jobs/runner 注册点在 main.py，lead 接）。

async def _memory_data_home(ctx: JobCtx, container: str) -> str:
    """容器里的 DSH_HOME（口径同 capabilities：只认容器自己的 ENV，不猜路径）。"""
    r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c "
                      + shlex.quote('printf %s "${DSH_HOME:-/data}"'), timeout=60)
    return (r.stdout or "").strip().rstrip("/") or "/data"


async def _memory_restart_apply(ctx: JobCtx, container: str) -> None:
    """铺/摘 home 层补丁后重启容器让配置重组，并等回健康（先例：restart 工单 10/20 步）。"""
    rt = ctx.runtime()
    r = await rt.restart(container, line_sink=ctx.buffered_sink(_buf := []))
    await ctx.flush_lines(_buf)
    await ctx.log("info", f"docker restart -> exit {r.code}")
    if not r.ok:
        raise StepFailed(f"容器重启失败（记忆补丁已落卷，不丢）: {r.tail()}")
    deadline = float(ctx.settings.wait_healthy_seconds)
    waited = 0.0
    interval = max(0.05, float(getattr(ctx.settings, "wait_healthy_interval", 2.0)))
    while waited < deadline:
        snap = await rt.inspect(container)
        if snap is not None and snap.state == "running" and snap.health in ("healthy", "none"):
            return
        await asyncio.sleep(interval)
        waited += interval
    raise StepFailed(f"{deadline:.0f}s 内容器仍未恢复健康（补丁已落卷）："
                     "容器起来后重跑本步/本工单即可（幂等）")


async def memory_wiring(ctx: JobCtx, inst: Any, host: Any, container: str,
                        *, strict: bool = False) -> dict[str, Any]:
    """记忆接线本体（幂等）。返回事实 dict，由调用方决定 skipped/failed 与重启。

    动作序（全成才算接线）：Hub 建/认用户（确定性 user_id=dshctl-<slug>）→
    台账 upsert(kind=memory_user_key) → key 走 stdin docker exec 合并写 refs →
    home 层补丁指 baseURL（新 marker；P0 旧 marker 认作已接线）→ auth/verify 回读。

    strict=True（详情页点「启用」的工单）：缺料一律 StepFailed——用户主动点的动作
    不能静默跳过；strict=False（开通第 68 步）：缺料 skipped，不挡开通。
    """
    slug = inst.slug
    home = await _memory_data_home(ctx, container)
    r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c "
                      + shlex.quote(memhub.script_patch_state(home)), timeout=60)
    state = memhub.parse_patch_state(r.stdout or "")
    hub, missing = await memhub.build_memory_hub(
        ctx.s, ctx.settings, recorder=getattr(ctx.providers, "recorder", None))
    base = memhub.memory_base_url(ctx.settings, getattr(host, "name", ""),
                                  getattr(host, "ssh_host", ""))
    wired = bool(state["memory"] or state["p0"])
    # 接管语义（2026-10-29 用户点「接管启用」后的更正）：主动点的工单遇到 P0 手写
    # 段要过户——拆旧段重铺控制台段（下走完整动作序）；开通第 68 步（非 strict）
    # 维持兼容短路，不折腾存量。
    takeover = bool(strict and state["p0"] and not state["memory"])
    facts: dict[str, Any] = {
        "skipped": False, "wired": wired,
        "base_url": base if wired else "",
        "user_id": "", "key_masked": "", "patched":
            "memory" if state["memory"] else ("p0_legacy" if state["p0"] else ""),
        "restarted": False, "verified": False,
    }
    if hub is None or getattr(hub, "manual_required", False):
        why = missing or getattr(hub, "reason", "") or "记忆层为 noop"
        if strict:
            raise StepFailed(f"{why}——这是主动点的「启用」，缺料必须先补，不静默跳过")
        facts.update(skipped=True, reason=why)
        return facts
    # 探活：core /health + Proxy TCP。TCP 探的是站点默认出口（控制台视角）——
    # 按主机覆盖的出口（如经隧道）控制台未必够得着，不装能测，容器侧首条消息即见分晓。
    core_ok = await hub.core_health()
    proxy_ok = await memhub.probe_tcp(str(ctx.settings.memory_proxy_url or ""))
    if not core_ok or not proxy_ok:
        why = ("记忆栈不通（core "
               f"{'通' if core_ok else '不通'} / Proxy "
               f"{'通' if proxy_ok else '不通'}）：本次跳过记忆接线，实例其他功能不受影响"
               "——栈恢复后重跑开通，或在详情页点「启用记忆」")
        if strict:
            raise StepFailed(why.replace("本次跳过记忆接线", "启用失败")
                             + "；实例未做任何改动")
        facts.update(skipped=True, reason=why)
        return facts

    cred = await repo.get_credential_by_name(ctx.s, memhub.credential_name_for(slug))
    stored = ""
    if cred is not None and cred.kind == "memory_user_key":
        stored = str((cred.secret or {}).get("user_key") or "")
    want_uid = memhub.user_id_for(slug)

    # 幂等短路：补丁在位 + refs 在位 + 台账 key 在 core 侧仍有效 → 只复核不动手
    # （接管在途时不短路：P0 段要换成控制台段才算数）
    if wired and state["refs"] and stored and not takeover:
        v = await hub.verify_key(stored)
        if v["valid"] and (not v.get("user_id") or v["user_id"] == want_uid):
            facts.update(user_id=v.get("user_id") or want_uid,
                         key_masked=memhub.mask_key(stored), verified=True,
                         detail=("记忆已接线，key 复核有效（auth/verify 通过），本次不重铺"
                                 + ("；接线为 P0 手工版（旧记号 # dsh-control-p0，"
                                    "状态里认作已接线）" if state["p0"] else "")))
            return facts

    try:
        res = await hub.ensure_instance_user(
            slug, team_id=str(ctx.settings.memory_team_id or ""),
            agent_name=f"{slug}（DSH 实例）")
    except IntegrationError as exc:
        raise StepFailed(f"记忆中枢建/认用户失败：{exc}"
                         "（user_id 是确定性的 dshctl-<slug>，恢复后重跑本步不会多出用户）"
                         ) from exc
    uid = str(res.get("user_id") or want_uid)
    key = str(res.get("user_key") or "")
    facts["user_id"] = uid
    if res.get("dry_run"):
        facts.update(skipped=True, reason="dry-run：Hub 写动作只记录未发送，容器侧未改动")
        return facts
    if not memhub.key_shape_ok(key):
        raise StepFailed("记忆中枢没发回可用的 key（user/create 与 user-key/create 都没给 "
                         f"key_value 或形状异常）：查 core 的 key_limit 后重跑本步（幂等，"
                         f"用户 {uid} 不会重复建）")
    facts["agent"] = str(res.get("agent") or "")
    if res.get("team", "").startswith("failed"):
        await ctx.log("warn", f"业务用户拉进 Team 没成功（{res['team']}）：接线照常，"
                              f"请人工把 {uid} 加进 [memory].team_id 指的 Team")

    await repo.upsert_credential(
        ctx.s, kind="memory_user_key", name=memhub.credential_name_for(slug),
        secret={"user_key": key, "user_id": uid},
        note=f"实例 {slug} 的业务 key（记忆中枢用户 {uid}；停用=回收，记忆数据在 Hub 卷）")
    await ctx.s.commit()

    # 业务 key 进容器只走 stdin（不进 argv、不进日志、不进目标机 ps——ADR 硬规则 3）
    r = await ctx.ssh(f"docker exec -i {shlex.quote(container)} sh -c "
                      + shlex.quote(memhub.script_refs_merge(home)),
                      timeout=120, stdin=f"{key}\n", quiet=True)
    if not r.ok:
        raise StepFailed(f"合并写 .credentials.yaml refs 失败（records 段没动过）: "
                         f"{r.tail()}")
    for line in (r.stdout or "").splitlines():
        if line.strip():
            await ctx.log("info", f"[记忆] {line.strip()}"[:200])

    model_id = (ctx.settings.memory_model_id or ctx.settings.llm_model_id or "").strip()
    if not wired or takeover:
        if not base:
            raise StepFailed("出口前缀没解析出来（[memory].proxy_url 为空且无按主机覆盖）："
                             "home 层补丁写不出 baseURL，请先补站点配置")
        if not model_id:
            raise StepFailed("home 层补丁要写 models.id：memory_model_id 与 llm_model_id "
                             "都是空的，补站点配置后重跑（用户和 key 已就位，重跑不重复建）")
        script = (memhub.script_home_takeover if takeover else memhub.script_home_patch)
        r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c "
                          + shlex.quote(script(home, base, model_id=model_id)),
                          timeout=120)
        if not r.ok:
            raise StepFailed(f"home 层补丁铺设失败: {r.tail()}")
        out = r.stdout or ""
        if "PATCH_TAKEOVER_UNSAFE" in out:
            raise StepFailed("接管拒绝：P0 记号到下一条记号之间切出来的段不像记忆段"
                             "（缺 apiKeyEnv/baseURL 特征），未做任何改动——请人工核对该段")
        if "PATCH_ALREADY" in out:
            facts["patched"] = "memory"
        elif "PATCH_WIRED" in out:
            facts["patched"] = "laid"
        else:
            facts["patched"] = "memory"

    v = await hub.verify_key(key)
    if not v["valid"] or (v.get("user_id") and v["user_id"] != uid):
        raise StepFailed(f"发回的 key 过不了 auth/verify 回读（valid={v['valid']}"
                         f"，期望用户 {uid}）：core 侧问题，恢复后重跑本步即可"
                         "（key 会补发一把新的，旧的留在 Hub 台账里）")
    facts.update(verified=True, wired=True, base_url=base,
                 key_masked=memhub.mask_key(key))
    await ctx.event("memory_wired",
                    f"记忆中枢接线完成：用户 {uid} 出口 {base}"
                    + (f"；Team 拉群 {res.get('team')}" if res.get("team") else ""),
                    {"user_id": uid, "key_masked": memhub.mask_key(key),
                     "base_url": base, "patched": facts["patched"],
                     "team": res.get("team", "")})
    return facts


async def memory_wiring_step(ctx: JobCtx, inst: Any, host: Any, container: str,
                             st: Any, *, strict: bool = False) -> dict[str, Any]:
    """把 memory_wiring 的返回落成步骤三态；新铺补丁时负责重启与等健康。"""
    facts = await memory_wiring(ctx, inst, host, container, strict=strict)
    if facts["skipped"]:
        st.state = "skipped"
        st.detail = facts["reason"]
        await ctx.log("warn", facts["reason"])
        await ctx.event("memory_wiring_skipped", facts["reason"],
                        {"slug": inst.slug})
        await ctx.s.commit()
        return facts
    bits = []
    if facts.get("patched") == "laid":
        bits.append(f"home 层补丁新铺（marker {memhub.MEMORY_MARKER}，baseURL "
                    f"{facts['base_url']}）")
    elif facts.get("patched") == "p0_legacy":
        bits.append("认 P0 手工接线为已接线（旧记号 # dsh-control-p0），未重复铺段")
    elif facts.get("patched") == "memory":
        bits.append("home 层补丁早已在位，本次只补 key/refs 或复核")
    if facts.get("user_id"):
        bits.append(f"用户 {facts['user_id']}（key {facts.get('key_masked') or '未变'}）")
    if facts.get("verified"):
        bits.append("auth/verify 回读通过")
    if facts.get("detail"):
        bits = [facts["detail"]]
    st.detail = "；".join(b for b in bits if b)[:500]
    if facts.get("patched") == "laid":
        await ctx.log("info", "home 层补丁新铺：重启容器让 DSH 重组配置"
                              "（只在启动时组合一次，不重启这次接线不算生效）")
        await _memory_restart_apply(ctx, container)
        facts["restarted"] = True
        st.detail += "；已重启容器并确认健康"
    await ctx.s.commit()
    return facts


async def run_memory(ctx: JobCtx) -> None:
    """记忆启用/停用工单（job kind="memory"，params {"action": "enable"|"disable"}）。

    running 实例上可反复重跑：enable 是全套接线（幂等），disable 是
    「回收业务 key（U-7：停用＝回收）+ 摘容器接线 + 重启还原直连」。
    两种动作都不删记忆数据——数据永远在 Hub 卷里，重新 enable 就接回来。
    """
    inst = ctx.instance
    host = ctx.host
    slug = inst.slug
    container = inst.container_name or derive(slug, ctx.settings.dns_zone).container
    action = str((ctx.job.params or {}).get("action") or "").strip()
    if action not in ("enable", "disable"):
        raise StepFailed(f'memory 工单 params.action={action!r}，只认 "enable"/"disable"'
                         "（从详情页重新发起即可，工单不改参数重跑）")

    if action == "enable":
        async with ctx.step(10, "memory_wiring", "启用记忆（接线）") as st:
            facts = await memory_wiring_step(ctx, inst, host, container, st, strict=True)
        if facts.get("restarted"):
            async with ctx.step(20, "token_capture", "重抓登录密钥") as st:
                tok = await capture_token(ctx.s, ctx.runtime(), inst, "restart")
                if tok.changed:
                    st.detail = "接线重启后已抓到最新一键地址"
                    await ctx.event("token_captured", "记忆接线重启后重抓登录密钥",
                                    {"source": "restart", "url": tok.url,
                                     "token": tok.token})
                else:
                    st.detail = "登录密钥与库里最新值一致（未轮换）：不落新行、不记事件"
        else:
            st2 = await repo.add_step(ctx.s, ctx.job.id, 20, "token_capture",
                                      "重抓登录密钥")
            st2.state = "skipped"
            st2.detail = "本次没重启容器（本来就接好线），密钥没轮换"
            await ctx.s.commit()
        return

    # ---- disable：回收 key（Hub 侧）→ 摘容器接线 → 重启还原直连 → 重抓 token
    async with ctx.step(10, "memory_revoke", "停用业务用户（回收 key）") as st:
        hub, missing = await memhub.build_memory_hub(
            ctx.s, ctx.settings, recorder=getattr(ctx.providers, "recorder", None))
        if hub is None or getattr(hub, "manual_required", False):
            raise StepFailed(f"记忆中枢不可达（{missing or getattr(hub, 'reason', '')}）："
                             "停用要在 Hub 侧回收 key。补好站点配置重跑本工单，"
                             "或去 Hub 面板人工回收（容器侧这步失败可重跑，未做改动）")
        uid = memhub.user_id_for(slug)
        try:
            rv = await hub.revoke_user_keys(uid)
        except IntegrationError as exc:
            raise StepFailed(f"回收 {uid} 的 key 失败：{exc}（重跑本工单即可，幂等——"
                             "已回收的不会重复回收）") from exc
        if rv["failed"]:
            detail = "；".join(f"{f['key_id']}: {f['why']}" for f in rv["failed"])
            raise StepFailed(f"回收不完整：{detail}。若是 last_key_cannot_revoke，"
                             f"是 core 保护「用户至少留一把 key」——去 Hub 删除用户 {uid} "
                             "或人工回收后再重跑本工单")
        st.detail = (f"用户 {uid}：回收 {len(rv['revoked'])} 把 active key"
                     "（记忆数据留在 Hub 卷里，本控制台从不删；重新启用即接回）")
        await ctx.event("memory_user_disabled",
                        f"业务用户 {uid} 已停用（回收 {len(rv['revoked'])} 把 key）",
                        {"user_id": uid, "revoked": rv["revoked"]})
        await ctx.s.commit()

    async with ctx.step(20, "memory_strip", "摘容器接线（还原直连）") as st:
        home = await _memory_data_home(ctx, container)
        r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c "
                          + shlex.quote(memhub.script_home_strip(home)),
                          timeout=120)
        if not r.ok:
            raise StepFailed(f"摘除容器接线失败（未做改动或改动可重跑）: {r.tail()}")
        out = r.stdout or ""
        st.detail = "；".join(ln.strip() for ln in out.splitlines()
                              if ln.strip())[:500]
        if "PATCH_P0_LEGACY" in out:
            await ctx.log("warn", "P0 手工段没有结束记号，段没敢自动摘（记忆通路已因回收 key "
                                  "而停用）：要完全还原直连，人工删该段与 refs 键后重启，"
                                  "或先「启用」再「停用」一次让控制台接管")
        if "RESTART_NEEDED" in out:
            await ctx.log("info", "容器接线有改动：重启还原直连")
            await _memory_restart_apply(ctx, container)
            async with ctx.step(30, "token_capture", "重抓登录密钥") as st30:
                tok = await capture_token(ctx.s, ctx.runtime(), inst, "restart")
                if tok.changed:
                    st30.detail = "还原直连重启后已抓到最新一键地址"
                    await ctx.event("token_captured", "记忆停用重启后重抓登录密钥",
                                    {"source": "restart", "url": tok.url,
                                     "token": tok.token})
                else:
                    st30.detail = "登录密钥与库里最新值一致（未轮换）：不落新行、不记事件"
        else:
            st2 = await repo.add_step(ctx.s, ctx.job.id, 30, "token_capture",
                                      "重抓登录密钥")
            st2.state = "skipped"
            st2.detail = "容器侧没有可摘的接线（可能本来就未接线或 P0 手工段），未重启"
            await ctx.s.commit()


async def run_deploy(ctx: JobCtx) -> None:
    inst = ctx.instance
    host = ctx.host
    slug = inst.slug
    names = derive(slug, ctx.settings.dns_zone)
    rr = dns_rr(slug)
    dns_name = inst.dns_name or names.server_name
    public_url = inst.public_url or f"https://{dns_name}"
    data_dir = f"{host.data_root.rstrip('/')}/{slug}"
    # 工单附加参数（§4 deploy body {"image": "目标tag"}）：容器步与第 20 步的镜像
    # 在位检查优先用它，留空 = 站点当前目标 tag（settings.dsh_image）。
    image_param = str((ctx.job.params or {}).get("image") or "").strip()

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
        image = image_param or ctx.settings.require("dsh_image")
        # 443 必须挂证书：面板里 ssl=0 的 443 站点拼出来的配置 nginx 校验不过，
        # 而且公网地址是 https，不挂证书等于建成就用不了。用现成通配证书，不签新的。
        ctx.settings.require("wildcard_cert_name")
        await ctx.log("info", f"slug={slug} 域名={dns_name} 容器={names.container}")
        await ctx.log("info", f"NG 挂现成通配证书 {ctx.settings.wildcard_cert_name}"
                              "（443 不挂证书的面板配置过不了 nginx -t；本站不签新证书）")
        await ctx.log("info", f"镜像（准备好的版本，开通阶段永不构建"
                              f"{'；本工单指定 tag' if image_param else ''}）：{image}"
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
        # 恢复即「我要它再跑起来」：released 实例的 desired_state 翻回 running，
        # 否则工单成功后 watcher 仍按期望 released 跳过采集，等于恢复了没人守护。
        if inst.desired_state != "running":
            inst.desired_state = "running"
            await ctx.log("info", "期望状态改回 running（续建/恢复都是要它跑）")
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
            # 旧行为是甩一句「跑 cli images ship」就拦；§6.1.5 起先按设置自动搬：
            # download 有制品就在目标机 curl 三连，ssh 模式找源机流式直传，
            # 都办不到才拦（文案点名着手指路 + 附该机现有镜像）。
            await _ship_missing_image(
                ctx, host, image, _have_dsh_images(r_img.stdout))
        # kit 脚本版本化（和镜像同一思路，§6.1.2）：入库后控制台是唯一事实源，
        # 目标机 .dshctl-kit-version 标记不符就当场整包下发——只增盖 scripts/、
        # compose.yaml、.env.example，绝不动 instances/（实例目录是运行时状态）。
        # 控制台还没入库 → 沿用机器预装，只记一行 info，不失败工单（存量零冲击）。
        kit_rel = await repo.current_kit_release(ctx.s)
        if kit_rel is None:
            await ctx.log("info", "[kit] 控制台未入库 kit 版本，沿用机器预装"
                                  "（dsh-control kit publish --dir <kit仓库> 可入库）")
        else:
            r_kit = await ctx.ssh(
                f"cat {shlex.quote(kit)}/{'.dshctl-kit-version'} "
                "2>/dev/null || echo MISSING", timeout=60)
            have_v = (r_kit.stdout or "").strip().splitlines()
            have_v = have_v[-1].strip() if have_v else "MISSING"
            if have_v == kit_rel.version:
                await ctx.log("info", f"[kit] 版本已是最新 {have_v}")
            else:
                await ctx.log("info", f"[kit] 机器版本 {have_v!r} ≠ 现行 {kit_rel.version}："
                                      "整包下发（只增盖脚本与模板，不动 instances/）")
                try:
                    info = await install_kit(_ep_conn(ctx), kit_rel.files,
                                             kit_rel.version, kit)
                except RuntimeError as e:
                    raise StepFailed(f"kit 下发失败: {e}") from None
                await ctx.log("info", f"[kit] {kit_rel.version} 就位"
                                      f"（{info['files']} 个文件 / "
                                      f"{max(1, info['bytes'] >> 10)} KiB 压缩）")
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
        if host.upstream_mode == "direct":
            # §6.1.4：本机有公网 IP 的机器不过隧道——整步 skipped，没有隧道
            # 也就没有回滚动作；端口照走同一个池，写进 frp_remote_port
            # （列名沿用不改），它就是容器在本机发布的公网端口。
            port = await _direct_port(ctx, host, inst)
            inst.frp_remote_port = port
            inst.container_port = port
            await ctx.s.commit()
            st.state = "skipped"
            st.detail = (
                f"direct 主机不建隧道（§6.1.4）：本机公网 {host.ssh_host} 直发 :{port}"
                "（容器发布端口同号），frp_proxy_id 留空；\n"
                "  端口占用探测在目标机上做（ss -ltn 实听 + 库里同机实例），没问 frp-panel。")
        elif _manual(ctx, "frp"):
            port, _prior = await _reuse_or_pick_port(ctx, names, host, inst)
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
            port, prior = await _reuse_or_pick_port(ctx, names, host, inst)
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
        # upstream 后端主机（§6.1.4）：direct = 这台机自己的公网地址（NG 仍在入口机，
        # 证书与 location 规则一字不变）；登记了 direct_backend 就用它——云轻量服务器
        # （SWAS 这类）的 EIP 是云端 NAT 映射，网卡上根本没有，入口机恰好=这台机时
        # 拨自家 EIP 出网卡绕云端 hairpin 黑洞（2026-10-22 dsh-05 现网实测），必须
        # 用网卡实址回源。frp = 入口机口径（frps_upstream_host，别动）。
        backend = (host.direct_backend or "").strip()
        if host.upstream_mode == "direct":
            upstream_host = backend or host.ssh_host
        else:
            upstream_host = ctx.settings.require("frps_upstream_host")
        upstream_server = f"{upstream_host}:{inst.frp_remote_port}"
        fw_note = ""
        if host.upstream_mode == "direct":
            if backend:
                fw_note = (f"\n【§6.1.4 云轻量】回源走登记地址 {backend}（EIP 在云端"
                           f"NAT 不在本机网卡，拨 EIP 回源是 hairpin 黑洞）；第 60 步"
                           f"把实例端口绑到 {backend}，公网路由不到它，云防火墙无需"
                           "放行——比原红线更小的暴露面。")
            else:
                fw_note = (f"\n【防火墙红线 §6.1.4】实例端口 :{inst.frp_remote_port} "
                           f"只应对入口机 {ctx.settings.require('ingress_ip')}"
                           "（DSHC_INGRESS_IP）放行，不对全世界开放；云防火墙/安全组"
                           "不由本控制台管理，请运维在云上补。")
        if _manual(ctx, "ng"):
            st.state = "skipped"
            await ctx.ng().ensure_site(
                server_name=dns_name, upstream_name=names.upstream,
                upstream_server=upstream_server,
                listen_port=443, header_host="$host", websocket=True,
                cert_name=ctx.settings.require("wildcard_cert_name"),
            )
            st.detail = (
                "NG provider = noop，需人工配置反向代理：\n"
                f"  upstream {names.upstream}: {upstream_server}\n"
                f"  server_name {dns_name}，location / 反代到该 upstream，"
                "headerHost=$host，websocket=true\n"
                f"  证书：{ctx.settings.wildcard_cert_name}"
                "（443 必须挂证书，否则面板配置过不了 nginx -t）\n"
                "（headerHost 带端口会让 /api/remote.mux 403，见 docs/site/NG-API.md）"
                + _drained(ctx, "ng") + fw_note
            )
            await ctx.s.commit()
        else:
            site = await ctx.ng().ensure_site(
                server_name=dns_name, upstream_name=names.upstream,
                upstream_server=upstream_server,
                listen_port=443, header_host="$host", websocket=True,
                cert_name=ctx.settings.require("wildcard_cert_name"),
            )
            if _synthetic_id(site.server_id):
                st.state = "skipped"
                st.detail = ("provider 返回合成 server_id，未登记。这一层要人工补做：\n"
                             f"  upstream {names.upstream}: {upstream_server}\n"
                             f"  server_name {dns_name}（headerHost=$host，websocket=true）\n"
                             "  补做后把真实 server_id 人工回填，否则释放时删不到。"
                             + _drained(ctx, "ng") + fw_note)
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
                st.detail = (f"upstream {names.upstream} → {upstream_server}；"
                             f"server_name {dns_name}（headerHost=$host，"
                             f"websocket=true，证书 {ctx.settings.wildcard_cert_name}）"
                             + fw_note)
                await ctx.s.commit()
                await ctx.event("ng_created", f"站点 {dns_name} server_id={site.server_id}",
                                {"server_id": site.server_id, "upstream": names.upstream,
                                 "upstream_id": site.upstream_id,
                                 "location_id": site.location_id})

    # ---- 60 container_deploy：跑 kit 的 new-instance.sh + deploy.sh。
    # 契约：此步失败「保留现场，标 error」——从这里开始不再回滚已建对象。
    # ---- 58 capability_gate：兼容闸门（ADR-0001 决策五：升级不静默越过不兼容）。
    # 只对「台账里有 desired=present 的能力」的实例有意义；目标版本从镜像 tag
    # 解析（tag 里取 semver 前缀），解析不出或没货 → skipped
    # 并在 detail 说破（闸门宁可不咬，不瞎咬）。
    async with ctx.step(58, "capability_gate", "能力兼容闸门") as st:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        from ..db.models import Capability, InstanceCapability
        from ..integrations import capability_engine as capeng
        n_caps = await ctx.s.scalar(
            sa_select(func.count()).select_from(InstanceCapability)
            .where(InstanceCapability.instance_id == inst.id,
                   InstanceCapability.desired_state == "present"))
        image_ref = str((ctx.job.params or {}).get("image")
                        or ctx.settings.dsh_image or "")
        target = ""
        tail_tag = image_ref.rsplit(":", 1)[-1] if ":" in image_ref else ""
        for chunk in tail_tag.replace("-", ".").split("."):
            import re as _re
            if _re.match(r"^\d+$", chunk):
                continue
            break
        cand = ".".join(tail_tag.replace("-", ".").split(".")[:3])
        if capeng.parse_version(cand) is not None:
            target = cand
        if not n_caps:
            st.state = "skipped"
            st.detail = "台账里没有启用的能力：闸门无事可咬"
        elif not target:
            st.state = "skipped"
            st.detail = f"目标镜像 tag「{image_ref}」里解析不出 DSH 版本：闸门跳过（升级后由能力工单的现读自检兜底）"
        else:
            blockers: list[str] = []
            rows = (await ctx.s.scalars(
                sa_select(InstanceCapability).where(
                    InstanceCapability.instance_id == inst.id,
                    InstanceCapability.desired_state == "present"))).all()
            for led in rows:
                cap = await ctx.s.scalar(
                    sa_select(Capability).where(
                        Capability.capability_id == led.capability_id,
                        Capability.current.is_(True)))
                if cap is None or not cap.compatible_dsh:
                    continue
                if not capeng.satisfies(target, cap.compatible_dsh):
                    blockers.append(
                        f"{cap.capability_id}@{cap.version} 要求 {cap.compatible_dsh}，"
                        f"目标 {target} 不满足")
            if blockers:
                raise StepFailed("兼容闸门拦截：" + "；".join(blockers)
                                 + "——先禁用该能力再升级，或换镜像")
            st.detail = f"{n_caps} 项能力对目标 DSH {target} 全部兼容"

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
        # 按主机的模型入口覆盖（§2/§6.1.4）：站点值是局域网网关地址，云上机器拨不到；
        # 登记了 host.llm_base_url 就换地址不换密钥（同一网关的公网门/隧道口）。
        host_llm = (host.llm_base_url or "").strip()
        if host_llm:
            llm["base_url"] = host_llm
            await ctx.log("info", f"[env] 该机登记了模型入口覆盖，LLM_BASE_URL 用它：{host_llm}")
        # 资源红线（§2/§6.1.4）：主机配额有值（去空白非空串）就改写进实例 .env，
        # 无值一行都不写——kit 默认（4g/4 核）只该保护独占机，挤共享机必设配额。
        # 改写写法沿用上面 DSH_IMAGE 的 if-grep-sed-else-printf。
        limit_lines: list[str] = []
        echo_keys = ["HOST_PORT", "TRUSTED_HOSTS", "PUBLIC_URL", "DSH_IMAGE",
                     "DATA_DIR", "LLM_BASE_URL", "LLM_MODEL_ID"]
        for key, val in (("MEM_LIMIT", (host.mem_limit or "").strip()),
                         ("CPU_LIMIT", (host.cpu_limit or "").strip())):
            if not val:
                continue
            if not re.fullmatch(r"[A-Za-z0-9._]+", val):
                await ctx.log("warn", f"主机配额 {key}={val!r} 含意外字符，"
                                      "跳过写入（该机实例吃 kit 默认，请改登记）")
                continue
            limit_lines.append(
                f'if grep -q "^{key}=" "$f"; then '
                f'sed -i "s|^{key}=.*|{key}={val}|" "$f"; '
                f'else printf "{key}=%s\\n" {shlex.quote(val)} >> "$f"; fi')
            echo_keys.append(key)
        # direct_backend 兼作监听地址（§6.1.4）：端口只绑登记址（云轻量机=网卡实址），
        # 公网路由不到它，云防火墙一行都不用放行。字符白名单同配额，不过就跳过并 warn。
        if backend:
            if re.fullmatch(r"[A-Za-z0-9._-]+", backend):
                limit_lines.append(
                    f'if grep -q "^HOST_BIND_IP=" "$f"; then '
                    f'sed -i "s|^HOST_BIND_IP=.*|HOST_BIND_IP={backend}|" "$f"; '
                    f'else printf "HOST_BIND_IP=%s\\n" {shlex.quote(backend)} >> "$f"; fi')
                echo_keys.append("HOST_BIND_IP")
            else:
                await ctx.log("warn", f"主机 direct_backend {backend!r} 含意外字符，"
                                      "跳过 HOST_BIND_IP 写入（实例将监听 0.0.0.0，"
                                      "请改登记）")
        echo_re = "|".join(echo_keys)
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
            *limit_lines,
            # 值可能含 / & 等 sed 分隔符，一律"删行 + 追加"，不用 sed 替换
            _PUT_FN,
            f'put LLM_BASE_URL {shlex.quote(llm["base_url"])}',
            # 密钥从 stdin 进来：命令行会进任务日志，也在目标机 ps 里裸奔
            "read -r llm_api_key",
            "read -r search_api_key",
            'put LLM_API_KEY "$llm_api_key"',
            'if [ -n "$search_api_key" ]; then put SEARCH_API_KEY "$search_api_key"; fi',
            (f'put LLM_MODEL_ID {shlex.quote(llm["model_id"])}'
             if llm["model_id"] else ":"),
            # 回显白名单里刻意没有 LLM_API_KEY / SEARCH_API_KEY
            f'grep -E "^({echo_re})=" "$f"',
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

    # ---- 67 fonts：铺字体——把站点字体库 cp 进实例 DATA_DIR 的 home/.fonts。
    # 放在容器部署之后：DATA_DIR 此刻已由 kit 脚本按生效值建好（口径同 65 步，
    # 优先读实例 .env 的 DATA_DIR，读不到才按 data_root 拼）。对已 running 的
    # 实例重复部署（redeploy 切镜像）同样跑：cp -n 幂等，只可能新增、绝不覆盖。
    # 库没配 → 记 warning 跳过，不失败工单（先例：生图技能）。
    # 库目录不在实例宿主机上，按序取货：①控制台制品库的现行版（fontlib_releases，
    # NAS 宕了也不挡开通）；②font_library_source 主机经控制台内存中转（兜底，
    # 形态同 cli images ship）；两者都没有 → warning + skipped。取货失败=铺字体
    # 失败，标 error 可重跑——配了货还缺字体是真故障，不静默。
    async with ctx.step(67, "fonts", "铺字体") as st:
        lib = (ctx.settings.font_library or "").strip().rstrip("/")
        if not lib:
            st.state = "skipped"
            st.detail = _FONT_SKIP_NO_LIB
            await ctx.log("warn", _FONT_SKIP_NO_LIB)
            await ctx.s.commit()
        else:
            facts = await env_facts(ctx, slug)   # 同 65 步口径：DATA_DIR 认机器上的事实
            dst = f"{(facts.get('DATA_DIR') or data_dir).rstrip('/')}/{_FONT_DST_REL}"
            obtained = ""
            r = await ctx.ssh(_font_cp_script(lib, dst), timeout=600)
            if not r.ok:
                raise StepFailed(f"铺字体失败: {r.tail()}")
            out = r.stdout or ""
            if "[fonts] LIB_MISSING" in out:
                src_ref = (ctx.settings.font_library_source or "").strip()
                rel = await repo.current_fontlib_release(ctx.s)
                store_file = (f"{ctx.settings.image_store.rstrip('/')}/{rel.filename}"
                              if rel is not None else "")
                if rel is not None and not os.path.isfile(store_file):
                    await ctx.log("warn", f"账上有字体库制品 {rel.version}，制品库文件却不在"
                                          f"（{store_file}）：降级走兜底渠道")
                    rel = None
                if rel is not None:
                    await ctx.log("info", f"宿主 {host.name} 上没有字体库：从控制台制品库"
                                          f"下发 {rel.version}（{rel.file_count} 个文件 / "
                                          f"{max(1, rel.size_bytes >> 20)} MiB）")
                    info = await install_fontlib(_ep_conn(ctx), store_file, lib)
                    obtained = (f"从制品库 {rel.version} 下发 {info['files']} 个文件"
                                f"落库 {lib}；")
                    await ctx.log("info", obtained.rstrip("；"))
                elif src_ref and src_ref in (host.name, host.ssh_host):
                    raise StepFailed(
                        f"字体库来源主机就是实例宿主机自己（{src_ref}），"
                        f"但库目录 {lib} 不存在：先把字体库入库"
                        "（dsh-control fontlib publish）或在那台机器备好库")
                elif src_ref:
                    await ctx.log(
                        "info",
                        f"宿主 {host.name} 上没有字体库：从 {src_ref} 中转复制"
                        "（源机→控制台内存→目标，两台机器之间不需要互信）")
                    relay = await relay_font_library(ctx.s, src_ref, _ep_conn(ctx), lib)
                    obtained = (f"已从 {relay['src']} 中转：搬 "
                                f"{max(1, relay['bytes'] >> 20)} MiB"
                                f"/{relay['seconds']}s，落库 {relay['files']} 个文件"
                                f"到 {lib}；")
                    await ctx.log("info", obtained.rstrip("；"))
                else:
                    st.state = "skipped"
                    st.detail = (f"字体库目录 {lib} 在主机 {host.name} 上不存在，"
                                 "制品库也没有现行制品、没配 font_library_source："
                                 "本次跳过铺字体，实例其他功能不受影响")
                    await ctx.log("warn", st.detail)
            if st.state != "skipped":
                if obtained:
                    # 库刚落到本机：再跑同一遍 cp（cp -n 幂等，形态不变）
                    r = await ctx.ssh(_font_cp_script(lib, dst), timeout=600)
                    if not r.ok:
                        raise StepFailed(f"铺字体失败（落库后）: {r.tail()}")
                    out = r.stdout or ""
                    if "[fonts] LIB_MISSING" in out:
                        raise StepFailed(f"字体库落库后仍报库目录不存在：{lib}")
                new = _FONT_NEW_RE.search(out)
                st.detail = (obtained + f"{lib} → {dst}：本次新增 "
                             f"{new.group(1) if new else '?'} 个文件"
                             "（cp -n 不覆盖同名，文件 644 / 目录 755）")
                await ctx.log("info", st.detail)
            await ctx.s.commit()

    # ---- 68 memory_wiring：记忆中枢接线（能力 memory-hub，kind=service）
    # 位置：容器 healthy（60/65）之后、复核（80/90）之前。装的是配置层的东西，
    # 数据全在卷里；新铺 home 层补丁时本步自己重启容器并等健康（token 还没抓，
    # 第 70 步抓的就是重启后的最新值）。缺料 → skipped + 警告事件，**不挡开通**
    # （先例：第 66 步生图）。P0 手工接线的台子认作已接线（兼容判据：home 层里
    # 有旧记号 "# dsh-control-p0"，见 integrations/memory_hub.py）。
    mem_facts: dict[str, Any] = {}
    async with ctx.step(68, "memory_wiring", "记忆中枢接线") as st:
        mem_facts = await memory_wiring_step(ctx, inst, host,
                                             inst.container_name or names.container, st)
        await ctx.s.commit()

    # ---- 69 capability_apply：默认能力铺装（ADR-0001 决策二：speckit 默认开）。
    # 只铺目录里 published 且 site 未关的能力（desired 由 provision_default_ledger
    # 立账，已有行不覆盖=实例级可关）。目录是空的 → skipped 不挡开通（先例 66/68）。
    async with ctx.step(69, "capability_apply", "铺默认能力（spec-kit 等）") as st:
        from ..db.models import utcnow
        from ..integrations import capability_engine as capeng
        from .capabilities import capability_ledger, capability_row, capability_secrets
        site_row = await repo.get_credential_by_kind(ctx.s, "site")
        created = await capeng.provision_default_ledger(
            ctx.s, inst.id, (site_row.secret or {}) if site_row else {})
        # 期望态全量：不止本次新建的行——续跑要对存量 desired=present 逐行核对
        from sqlalchemy import select as sa_select69

        from ..db.models import InstanceCapability as IC69
        want_rows = (await ctx.s.scalars(sa_select69(IC69).where(
            IC69.instance_id == inst.id,
            IC69.desired_state == "present"))).all()
        if not want_rows:
            st.state = "skipped"
            st.detail = ("站点默认能力目录是空的（kit 没随包发能力，或都被站点关了）："
                         "跳过铺装，等 dsh-control capability publish 发货后"
                         "在页面点「启用」即可")
            await ctx.log("warn", st.detail)
        else:
            rt69 = ctx.runtime()
            ctn69 = inst.container_name or names.container
            home69 = await capeng.read_dsh_home(ctx.ssh, ctn69)
            laid: list[str] = []
            if created:
                laid.append(f"新立期望 {len(created)} 项")
            # P3 过渡期：这两包由第 66 步带站点参数亲铺（69 步裸 apply 必缺料），
            # 这里只登记不重铺；两包改走工单制后删掉这个排除集
            _HELD_BY_66 = {"agent-team", "comfy-image"}
            for led0 in want_rows:
                cap_id = led0.capability_id
                if cap_id in _HELD_BY_66:
                    laid.append(f"{cap_id} 由 66 步带参铺（此处跳过）")
                    continue
                cap = await capability_row(ctx.s, cap_id)
                man = dict(cap.manifest or {})
                secrets = await capability_secrets(ctx.s, cap_id)
                dsh_ver, source = await capeng.read_dsh_version(rt69, ctn69)
                if cap.compatible_dsh and (
                        not dsh_ver or not capeng.satisfies(dsh_ver, cap.compatible_dsh)):
                    laid.append(f"{cap_id} 跳过（DSH {dsh_ver or '现读不到'} 不在 "
                                f"{cap.compatible_dsh}，source={source}）")
                    continue
                resolved = capeng.resolve_params(man, {}, secrets)
                res = await capeng.run_action(ctx.ssh, ctn69, man, home69, "apply",
                                              resolved, kit_dir=host.kit_path or "",
                                              version=cap.version)
                # 探针回读写台账（成没成以探针为准；69 步与 66/30 步同一口径）
                obs = await capeng.run_probe(ctx.ssh, ctn69, man, home=home69,
                                             values={})
                led69 = await capability_ledger(ctx.s, inst.id, cap_id)
                led69.installed = bool(obs["installed"])
                led69.enabled = bool(obs["enabled"])
                led69.observed_version = obs["observed_version"]
                led69.marker_present = bool(obs["marker_present"])
                led69.health = obs["health"]
                led69.health_detail = redact_text(str(obs["health_detail"]))[:400]
                led69.observed_at = utcnow()
                await ctx.s.commit()
                laid.append(f"{cap_id}@{cap.version} {res.state}"
                            f"（探针 installed={obs['installed']} "
                            f"health={obs['health']}）")
            st.detail = "；".join(laid)[:500]

    # ---- 70 token_capture：从容器日志抓一键地址（标 error，可重试）
    async with ctx.step(70, "token_capture", "从容器日志抓登录密钥"):
        tok = await capture_token(ctx.s, ctx.runtime(), inst, "bootstrap")
        if tok.changed:
            await ctx.event("token_captured", "部署后抓到一键地址",
                            {"source": "bootstrap", "url": tok.url, "token": tok.token})
        else:
            # §3 去重：值与库里最新一条相同就什么都不写、不记事件
            # （恢复/切镜像续建常走到这里——cookie secret 在 DATA_DIR，重建不轮换）。
            await ctx.log("info", "登录密钥与库里最新值一致（未轮换）：不落新行、不记事件")

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
        # 期望端点：主机登记了覆盖就用覆盖（§6.1.4：云上机器拨不到局域网网关，
        # 覆盖值是人写进库里的，走的是自家 frps 隧道——换门不换网关，授权成立）。
        expect_llm = (host.llm_base_url or "").strip() or ctx.settings.llm_base_url
        # 记忆已接线（本次铺的或 P0 旧记号认出的）：模型出口合法值多一个——自家
        # Proxy。没接线的台子传空串，判定与今天逐字节一致。
        expect_mem = str(mem_facts.get("base_url") or "") if mem_facts.get("wired") else ""
        bad = config_violations(gate.stdout or "", base_url=expect_llm,
                                memory_base_url=expect_mem)
        if bad:
            raise StepFailed("安全门不通过（花钱/外传这两条不外包给脚本）：\n  - "
                             + "\n  - ".join(bad))
        await ctx.log("info", f"安全门：{len(MUST_BE_DISABLED)} 条云端路径确认已断开，"
                              f"模型端点确认为 {expect_llm}"
                              + ("（主机登记的入口覆盖）" if host.llm_base_url else "")
                              + (f"；记忆出口确认为 {expect_mem}" if expect_mem else ""))

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
