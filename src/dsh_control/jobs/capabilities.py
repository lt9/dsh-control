"""给实例铺「能力」：编排层（ADR-0001 P3——heredoc 已删）。

放这里而不是散在 deploy/restart 里，是因为这两件事既要在**开通**时做一次，也要在
**重启**时重做一遍——智能体团队走的是 DSH 的 home 层补丁（``$DSH_HOME/cordis.patch.yml``），
它是卷里的文件、entrypoint 不碰，但技能目录可能被人手抖删过，重铺一次是幂等的。

「怎么铺」的知识全部住在 kit 的能力包（``kit/capabilities/<id>/manifest.toml``
与它的资产）：幂等 marker、补丁正文来源、config.env 渲染、观测探针，由
``integrations/capability_engine`` 按 manifest 解释执行。本文件只剩编排——
取参数（站点配置 + credentials）、查目录（capabilities 表）、发命令、回读事实。
新增能力就是在 kit 里发货一个包；评审在这里看到新写的 shell 一律驳回（决策一）。

三条硬约束原样存活：

1. **不重建镜像**。官方把「智能体团队」做成默认关闭的实验组合包，我们开它走
   home 层补丁（官方留给"给某个人开例外"的那一层，在数据卷里，重启不丢）。
2. **补丁正文不抄在我们仓库里**。manifest 的 source 声明顺着镜像里的 dsh
   可执行文件解析官方组合包；找不到就 skipped，不猜路径。
3. **密钥不进命令行**。技能地址与 token 一律 stdin（引擎的 _render_config_env
   是实现；heredoc 时代的 ``read -r`` 三行就是这条，改走引擎不换约束）。
"""

from __future__ import annotations

import shlex

from ..db import repo
from .runner import JobCtx, StepFailed

# home 层补丁里我们的记号：这是**契约常量**——与 agent-team manifest 的 marker
# 字段逐字相同（P3 迁移信号第 4 条：现存实例卷里唯一的幂等依据，差一个空格就会
# 把所有历史实例重铺一遍），与引擎 MARKER_PREFIX 口径同源。判定住在引擎，
# 这里留常量作回归锚点（marker 改文本必须升包 version）。
TEAM_MARKER = "# dsh-control: 智能体团队（实验组合包）由控制台铺设"

# 第 66/5 步经手的就是这两个存量能力（id 与 kit/capabilities/ 目录名一致）
_CAP_TEAM = "agent-team"
_CAP_IMAGE = "comfy-image"

_IMAGE_SKIP = ("站点没配生图服务（image_api_url 为空或没登记 image_key 密钥）："
               "本次跳过生图技能铺设，实例其他功能不受影响")


async def image_material(ctx: JobCtx) -> dict[str, str]:
    """生图服务地址在 settings，token 在 credentials(kind=image_key)。"""
    url = (ctx.settings.image_api_url or "").strip()
    cred = await repo.get_credential_by_kind(ctx.s, "image_key")
    token = str(((cred.secret or {}) if cred else {}).get("token") or "").strip()
    return {"url": url, "backup_url": (ctx.settings.image_backup_url or "").strip(),
            "token": token}


async def _data_home(ctx: JobCtx, container: str) -> str:
    """DSH 的家目录在容器里固定是 /data（镜像 ENV DSH_HOME=/data），取不到也不猜。"""
    r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c "
                      + shlex.quote('printf %s "${DSH_HOME:-/data}"'), timeout=60)
    home = (r.stdout or "").strip().rstrip("/") or "/data"
    return home


async def _engine_apply(ctx: JobCtx, container: str, home: str, cap_id: str, *,
                        given: dict | None = None, secrets: dict | None = None,
                        kit_dir: str = "", site_secret: dict | None = None) -> tuple[str, dict]:
    """引擎铺一个能力：目录取货 → 站点开关 → ``run_action``。返回（事实行, manifest）。

    三态语义照决策三，与 heredoc 现行为逐条对齐：

    - 目录没发货（kit 还没带这个包）→ 只记一行事实、不红——同第 69 步
      「目录空 → skipped 不挡开通」的先例；
    - 站点关停（site.toml 镜像的 capabilities 开关）→ 跳过，不动现场；
    - 引擎 skipped（缺料：镜像里找不到官方组合包、被管机 kit 没到货）→
      记原因不红（comfy 先例）；
    - 引擎 failed → StepFailed（heredoc 当时也是 failed）。
    """
    try:
        cap = await capability_row(ctx.s, cap_id)
    except StepFailed as exc:
        return f"{cap_id} 目录未发货：{exc}", {}
    man = dict(cap.manifest or {})
    if not engine.site_desires(man, site_secret or {}):
        return (f"{cap_id} 已被站点关停（site.toml 的 capabilities 开关）："
                "本次跳过，现有文件不动"), man
    resolved = engine.resolve_params(man, given or {}, secrets or {})
    res = await engine.run_action(ctx.ssh, container, man, home, "apply", resolved,
                                  kit_dir=kit_dir, version=cap.version)
    if res.state == "failed":
        raise StepFailed(f"铺设 {cap_id} 失败："
                         f"{redact_text(res.reason or res.detail or '引擎执行失败')[:800]}")
    return redact_text(res.detail or res.reason or "已执行"), man


async def apply_capabilities(ctx: JobCtx, container: str, *,
                             dsh_home: str | None = None) -> dict:
    """铺能力并回读校验。返回一组可直接写进 step detail / 日志的事实。

    对外契约不变（deploy 第 66 步与 restart 第 5 步照旧按键取值）：
    ``out["team"]``、``out["image"]``，外加逐能力的 ``out["check"]`` 回读行。
    """
    home = dsh_home or await _data_home(ctx, container)
    out: dict[str, str] = {}
    site_row = await repo.get_credential_by_kind(ctx.s, "site")
    site_secret = (site_row.secret or {}) if site_row else {}
    kit_dir = (ctx.host.kit_path or "") if ctx.host is not None else ""

    # ── 1. 智能体团队（home 层补丁，不碰镜像；开关读站点配置）──
    team_line, team_man = await _engine_apply(
        ctx, container, home, _CAP_TEAM,
        given={"max_members": ctx.settings.team_max_members},
        kit_dir=kit_dir, site_secret=site_secret)
    out["team"] = team_line[:200]

    # ── 2. 生图技能（缺材料就跳过，不挡住开通/重启——现行为逐字保留）──
    material = await image_material(ctx)
    if not material["url"] or not material["token"]:
        out["image"] = _IMAGE_SKIP
        await ctx.log("warn", _IMAGE_SKIP)
        image_man: dict = {}
    else:
        # 三个参数按 manifest 都声明为 secret（见包内注释：引擎模板渲染现状
        # 只对 secret 做 stdin 注入），密钥现取现塞，任何出口不回显
        image_line, image_man = await _engine_apply(
            ctx, container, home, _CAP_IMAGE,
            secrets={"url": material["url"], "backup_url": material["backup_url"],
                     "token": material["token"]},
            kit_dir=kit_dir, site_secret=site_secret)
        out["image"] = image_line[:200]

    # ── 3. 回读：观测只认 manifest 探针（决策一推论，不再手拼旁证 shell）──
    probed = False
    for cid, man in ((_CAP_TEAM, team_man), (_CAP_IMAGE, image_man)):
        if not man:
            continue
        obs = await engine.run_probe(ctx.ssh, container, man, home=home)
        line = (f"{cid}：{'已铺' if obs['installed'] else '未铺'}"
                f"（marker {'在' if obs['marker_present'] else '不在'}，"
                f"health={obs['health']}）")
        probed = True
        # 观测写回台账（P3 过渡期：66 步代铺的能力也要有 observed，
        # 否则详情页会红着说「没对上」——货是 66 步亲手铺的）
        if ctx.instance is not None:
            led = await capability_ledger(ctx.s, ctx.instance.id, cid)
            led.installed = bool(obs["installed"])
            led.enabled = bool(obs["enabled"])
            led.observed_version = obs["observed_version"]
            led.marker_present = bool(obs["marker_present"])
            led.health = obs["health"]
            led.health_detail = redact_text(str(obs["health_detail"]))[:400]
            led.observed_at = utcnow()
            await ctx.s.commit()
        out.setdefault("check", "")
        out["check"] += line + "；"
        await ctx.log("info", f"[能力] {line}"[:200])
    if not probed:
        # P3 过渡期（kit 还没发货能力目录）：至少留一眼通用现场事实
        check = "\n".join([
            f"echo \"技能: $(ls -1 {shlex.quote(home)}/skills 2>/dev/null | tr '\\n' ' ')\"",
            f"echo \"home 层: $([ -f {shlex.quote(home)}/cordis.patch.yml ] && echo 有 || echo 无)\"",
        ])
        r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c {shlex.quote(check)}",
                          timeout=60)
        for line in (r.stdout or "").splitlines():
            if line.strip():
                out.setdefault("check", "")
                out["check"] += line.strip() + "；"
                await ctx.log("info", f"[能力] {line.strip()}"[:200])
    return out


async def capabilities_ready(ctx: JobCtx, container: str, *,
                             dsh_home: str | None = None) -> bool:
    """存量能力是否都已就位（重启步骤用来决定要不要重铺）。

    目录发货后观测只认探针；kit 还没发货的过渡期退回旧的文件在位旁证一次
    眼验，免得重启工单因目录空被误判「没就位」而白跑一趟引擎取货。
    """
    home = dsh_home or await _data_home(ctx, container)
    mans: list[dict] = []
    for cap_id in (_CAP_TEAM, _CAP_IMAGE):
        try:
            mans.append(dict((await capability_row(ctx.s, cap_id)).manifest or {}))
        except StepFailed:
            mans = []
            break
    if mans:
        for man in mans:
            obs = await engine.run_probe(ctx.ssh, container, man, home=home)
            if not obs["installed"]:
                return False
        return True
    script = ("[ -f "
              + shlex.quote(f"{home}/cordis.patch.yml") + " ] && [ -d "
              + shlex.quote(f"{home}/skills/{_CAP_IMAGE}") + " ] && echo yes || echo no")
    r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c {shlex.quote(script)}",
                      timeout=60)
    return "yes" in (r.stdout or "")


async def read_dsh_config(runtime, container: str, *, dsh_version: str = "") -> dict:
    """把容器里实际生效的 DSH 配置读成给详情页用的分组事实。

    两次 SSH 往返（dump-config 约 0.6 秒、能力清单约 0.2 秒）。不放进详情接口本身：
    台账页现在一次 ssh 都不做、秒开，把它塞进首屏等于让 NAS 的负载决定页面快慢。
    """
    from datetime import UTC, datetime

    from ..domain import dsh_config

    dump = await runtime.dump_config(container)
    if not dump.strip():
        return {}
    facts = dsh_config.container_facts(await runtime.container_facts(container))
    return dsh_config.build_facts(
        dump,
        bundles=facts["bundles"],
        skills=facts["skills"],
        dsh_version=dsh_version,
        captured_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        home_patch_present=facts["home_patch"],
    )


# ================================================================ 能力工单薄层（ADR-0001 P2）
# 上面两段 heredoc 是 P3 之前的现状（迁移信号没凑齐，删早了把现网铺法弄丢）。
# 新机制的工单执行体在这里：它不认识任何具体能力——取货（capabilities 表）、
# 取密钥（credentials）、发命令（integrations/capability_engine）、记台账
# （instance_capabilities）。deploy 第 66 步将来换成调它，挂点由 lead 统一接。

from sqlalchemy import select

from ..db.models import Capability, Credential, InstanceCapability, utcnow
from ..integrations import capability_engine as engine
from .runner import redact_text


async def capability_row(s, capability_id: str, version: str = "") -> Capability:
    """目录取货：给了版本按版本取，没给取 current 行；只有 published 可铺。"""
    stmt = select(Capability).where(Capability.capability_id == capability_id)
    stmt = stmt.where(Capability.version == version if version
                      else Capability.current.is_(True))
    row = await s.scalar(stmt)
    if row is None:
        raise StepFailed(f"能力目录里没有 {capability_id}@{version or 'current'}"
                         "（先 dsh-control capability publish 发货）")
    if row.state != "published":
        raise StepFailed(f"能力 {capability_id}@{row.version} 状态是 {row.state}，"
                         "只有 published 可铺")
    return row


async def capability_ledger(s, instance_id: int, capability_id: str) -> InstanceCapability:
    """台账取行；没有就建一行（默认期望 present——工单本身就是期望的表达）。"""
    row = await s.scalar(select(InstanceCapability).where(
        InstanceCapability.instance_id == instance_id,
        InstanceCapability.capability_id == capability_id))
    if row is None:
        row = InstanceCapability(instance_id=instance_id, capability_id=capability_id)
        s.add(row)
        await s.commit()
    return row


async def capability_secrets(s, capability_id: str) -> dict[str, str]:
    """密钥现取现用：credentials(name=capability:<id>) 的 secret 表（参数名 → 值）。
    任何出口都不回显（硬规则第 3 条）。"""
    cred = await s.scalar(select(Credential).where(
        Credential.name == f"capability:{capability_id}"))
    return {str(k): str(v) for k, v in ((cred.secret if cred else None) or {}).items()}


async def run_capability(ctx: JobCtx) -> None:
    """能力工单执行体：install / apply / enable / disable / uninstall。

    步骤三态照决策三：幂等短路与缺料 skipped 都不算失败（comfy 先例）；失败
    取 stderr 尾巴进 detail（已脱敏）。步骤 30 用探针回读把 observed_* 写回
    台账——成没成以探针为准，不以「命令退码 0」为准。
    """
    p = ctx.job.params or {}
    action = str(p.get("action") or "")
    cap_id = str(p.get("capability") or "")
    if action not in ("install", "apply", "enable", "disable", "uninstall"):
        raise StepFailed(f"未知能力动作 {action!r}")
    if not cap_id or ctx.instance is None:
        raise StepFailed("工单参数缺 capability 或没挂实例")
    s = ctx.s

    # ---- 10 取货：目录 + 台账 + 期望态翻转 ----
    async with ctx.step(10, "capability_resolve", f"取货与参数：{cap_id} {action}") as st:
        ver_want = str(p.get("version") or "")
        led = await capability_ledger(s, ctx.instance.id, cap_id)
        if action in ("apply", "enable", "disable") and not ver_want:
            ver_want = led.desired_version     # 版本跟随台账（空=跟目录 current）
        given = dict(led.params or {})
        given.update((p.get("params") or {}) if action != "uninstall" else {})
        cap = await capability_row(s, cap_id, ver_want)
        if action == "install":
            led.desired_state = "present"
            led.desired_version = ver_want     # 空串=跟随 current 行
        elif action == "uninstall":
            # 卸载工单先翻 desired 再发命令（表注释的契约）
            led.desired_state = "absent"
        if action != "uninstall" and (p.get("params") or {}):
            led.params = given
        led.last_job_id = ctx.job.id
        await s.commit()
        man = dict(cap.manifest or {})
        secrets = await capability_secrets(s, cap_id)
        version = cap.version
        st.detail = (f"{cap.capability_id}@{cap.version} kind={cap.kind}"
                     f"（on_missing={cap.on_missing}，restart_required="
                     f"{bool(man.get('restart_required', True))}）")

    ssh, container = ctx.ssh, (ctx.instance.container_name or ctx.instance.slug)
    runtime = ctx.runtime()
    home = await engine.read_dsh_home(ssh, container)
    kit_dir = ctx.host.kit_path or ""

    # ---- 20 动手：引擎按 manifest 执行（SSH + docker exec 一条路）----
    async with ctx.step(20, "capability_action", f"引擎执行 {action}：{cap_id}") as st:
        if action == "apply" and not led.enabled:
            st.state = "skipped"
            st.detail = "能力处于禁用态，apply 不越权重新启用——要重开请点「启用」"
            await ctx.log("warn", st.detail)
            await s.commit()
        else:
            resolved = engine.resolve_params(man, given, secrets)
            if action in ("install", "enable") and cap.compatible_dsh:
                # 装/启用前的兼容自检：与闸门同一把尺子，不兼容的货别往上架
                dsh_ver, source = await engine.read_dsh_version(runtime, container)
                if not dsh_ver or not engine.satisfies(dsh_ver, cap.compatible_dsh):
                    raise StepFailed(
                        f"兼容闸门拦截：当前 DSH {dsh_ver or '（现读不到）'} "
                        f"不在 {cap.compatible_dsh} 区间内（source={source}）")
            res = await engine.run_action(ssh, container, man, home, action, resolved,
                                          kit_dir=kit_dir, version=version)
            if res.state == "skipped":
                st.state = "skipped"
                st.detail = redact_text(res.reason or res.detail)[:500]
                await ctx.log("warn", st.detail)
            elif res.state == "failed":
                raise StepFailed(redact_text(res.reason or res.detail or "引擎执行失败")[:800])
            else:
                st.detail = redact_text(res.detail or "已执行")[:500]
                if res.state == "succeeded" and bool(man.get("restart_required", True)) \
                        and action in ("install", "apply", "enable") \
                        and "幂等" not in st.detail:
                    await ctx.log("info", "restart_required=true：改动要重启才生效，"
                                          "工单不追着重启（等自然重启，U-6）")
            await s.commit()

    # ---- 30 回读：探针采观测态写台账（成没成以探针为准，不以退码为准）----
    async with ctx.step(30, "capability_observe", f"探针回读：{cap_id}") as st:
        obs = await engine.run_probe(ssh, container, man, home=home, values=given)
        dsh_ver, source = await engine.read_dsh_version(runtime, container)
        led = await capability_ledger(s, ctx.instance.id, cap_id)
        led.installed = bool(obs["installed"])
        led.enabled = bool(obs["enabled"])
        led.observed_version = obs["observed_version"]
        led.marker_present = bool(obs["marker_present"])
        led.health = obs["health"]
        led.health_detail = redact_text(obs["health_detail"])[:400]
        led.reason = redact_text(obs["reason"]) or f"{action} 完成（工单 {ctx.job.id}）"
        led.observed_at = utcnow()
        await s.commit()
        st.detail = (f"installed={obs['installed']} enabled={obs['enabled']} "
                     f"version={obs['observed_version'] or '未知'} "
                     f"health={obs['health']} dsh={dsh_ver or '未读到'}（{source}）")[:500]
        await ctx.event("capability_action",
                        f"{cap_id}@{version} {action}：{st.detail}",
                        {"action": action, "capability": cap_id, "version": version,
                         "dsh_version": dsh_ver, "dsh_version_source": source,
                         "probe": obs})


CAPABILITY_FLOWS = {"capability": run_capability}
