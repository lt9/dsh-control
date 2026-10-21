"""restart 状态机（契约 §6.3）：
container_restart → wait_healthy（最多 120s）→ token_capture → probe_public。

重启必然轮换登录 token：token_capture 失败则整个 job 失败（标 error），
因为「详情永远是当前最新密钥」是本控制台的硬承诺；可再点重启或 /token 重抓。
"""

from __future__ import annotations

import asyncio

from ..db import repo
from ..domain.config_docs import CONFIG_VERSION
from ..domain.dsh_config import annotate_model_drift
from ..integrations.base import ContainerStats
from .capabilities import apply_capabilities, capabilities_ready, read_dsh_config
from .capture import capture_token
from .deploy import env_facts
from .runner import JobCtx, StepFailed

_PROBE_MEANING = {
    "ok": "公网通了，反代 Host 头形状正确",
    "protected": "公网通了，首页也要登录（准入墙生效）",
    "host_origin_mismatch": "Host 与 Origin 又不相等（反代把端口拼进了 Host）",
    "auth_open": "没带 token 的受保护接口被放行（准入墙没生效）",
    "unreachable": "公网连不上（DNS/反代/frp/容器逐层查）",
    "unexpected": "回包不像 DSH（确认门后面是谁）",
}

async def run_restart(ctx: JobCtx) -> None:
    inst = ctx.instance
    container = inst.container_name or inst.slug
    rt = ctx.runtime()

    # ---- 5 capabilities：重启前重铺能力，让这次重启就把它们带上
    # 智能体团队开在 home 层补丁里，DSH 只在启动时组合一次配置，所以"铺完必须重启
    # 才生效"；而 entrypoint 每次启动会重写 profile 层，唯独不碰 home 层与技能目录
    # ——放在这里重铺，既幂等也正好卡在生效前一刻。
    async with ctx.step(5, "capabilities", "重铺技能与智能体团队") as st:
        if await capabilities_ready(ctx, container):
            st.detail = "技能与 home 层补丁都已就位，跳过重铺"
        else:
            caps = await apply_capabilities(ctx, container)
            st.detail = f"{caps.get('team', '')}；{caps.get('image', '')}"[:500]

    # ---- 10 container_restart
    async with ctx.step(10, "container_restart", f"docker restart {container}"):
        r = await rt.restart(container, line_sink=ctx.buffered_sink(_buf := []))
        await ctx.flush_lines(_buf)
        await ctx.log("info", f"docker restart -> exit {r.code}")
        if not r.ok:
            raise StepFailed(f"容器重启失败: {r.tail()}")

    # ---- 20 wait_healthy
    async with ctx.step(20, "wait_healthy", f"等待容器健康（≤{ctx.settings.wait_healthy_seconds}s）"):
        deadline = ctx.settings.wait_healthy_seconds
        waited = 0.0
        interval = max(0.05, getattr(ctx.settings, "wait_healthy_interval", 2.0))
        snap = None
        while waited < deadline:
            snap = await rt.inspect(container)
            if snap is not None and snap.state == "running" and snap.health in ("healthy", "none"):
                break
            await asyncio.sleep(interval)
            waited += interval
        else:
            raise StepFailed(f"{deadline}s 内容器仍未恢复健康")
        assert snap is not None
        inst.image_tag = snap.image or inst.image_tag
        inst.dsh_version = await rt.version(container)
        # 这里必须单独采一次 stats：`docker inspect` 里根本没有 CPU 与内存，而这一行
        # 是"重启完"那一刻写进快照的——拿 inspect 的 None 覆盖上去，详情页运行态
        # 就整片变空（2026-10-21 现网实测：重启之后 CPU / 内存双双显示 —）。
        stats = ContainerStats()
        if snap.state == "running":
            try:
                stats = await rt.stats(container)
            except Exception as exc:  # noqa: BLE001 - 取不到占用不影响重启成功
                await ctx.log("warn", f"采样资源占用失败（不影响重启）: {exc}")
        await repo.add_snapshot(
            ctx.s, inst.id, docker_state=snap.state, health=snap.health,
            restart_count=snap.restart_count, cpu_pct=stats.cpu_pct,
            mem_usage_bytes=stats.mem_usage_bytes, mem_limit_bytes=stats.mem_limit_bytes,
            raw={"image_id": snap.image_id, "ip": snap.ip},
        )
        await ctx.s.commit()

    # ---- 30 token_capture：重启后必须重新抓最新 token（§3 去重：值没变不写不记）
    async with ctx.step(30, "token_capture", "重抓登录密钥"):
        tok = await capture_token(ctx.s, rt, inst, "restart")
        if tok.changed:
            await ctx.log("info", "已抓到重启后的最新一键地址")
            await ctx.event("token_captured", "重启后重新抓到登录密钥",
                            {"source": "restart", "url": tok.url, "token": tok.token})
        else:
            await ctx.log("info", "重启后密钥与库里最新值一致（未轮换）：不落新行、不记事件")

    # ---- 35 dsh_config：重启后把 DSH 生效配置刷新进快照（详情页那格的最新值）
    try:
        dsh = await read_dsh_config(rt, container, dsh_version=inst.dsh_version or "")
    except Exception as exc:  # noqa: BLE001 - 读配置失败不该把重启判失败
        dsh = {}
        await ctx.log("warn", f"重启后读取 DSH 配置失败（不影响服务本身）: {exc}")
    if dsh:
        cfg = await repo.latest_config(ctx.s, inst.id)
        merged = dict((cfg.rendered if cfg else None) or {})
        # 顺手把 .env 的**当前**生效值刷进快照：配置卡上「实例模型名」这类条目，
        # 只靠开通那一次读回填，之后手工改过 .env 就一直是旧值（现网就撞上过一次：
        # 改了模型名、重建了容器，卡上还写着开通那天抄的值）。读不到就保留原值。
        try:
            facts = await env_facts(ctx, inst.slug)
        except Exception as exc:  # noqa: BLE001 - 读 .env 失败不该让重启失败
            facts = {}
            await ctx.log("warn", f"重启后回读 .env 失败（配置卡沿用旧值）: {exc}")
        merged.update({k: v for k, v in facts.items() if v not in (None, "")})
        # .env 写的值与容器里生效的值对账：`docker restart` **不会**重读 .env，
        # 只有 compose 重建容器才会把新值注进环境变量。这个差异常被当成
        # "改了没用"，页面上说破比让人猜强。
        dsh = annotate_model_drift(dsh, merged.get("LLM_MODEL_ID"))
        merged["DSH"] = dsh
        # 卡头上的版本号取自实例行；只写 v2 快照不改这一列，页面就会一边显示
        # "v1"一边渲染 v2 的内容，看着像快照串了台。
        inst.config_version = CONFIG_VERSION
        await repo.add_config_snapshot(ctx.s, inst.id, CONFIG_VERSION, merged)
        await ctx.log("info", f"DSH 配置已刷新：模型 {dsh['model']}，"
                              f"智能体团队{'已开' if dsh['agent_team'] else '未开'}，"
                              f"技能 {len(dsh['skills'])} 个")

    # ---- 40 probe_public：没配公网地址（还没部署完）则标记 skipped
    if not inst.public_url:
        skipped = await repo.add_step(ctx.s, ctx.job.id, 40, "probe_public", "公网探活")
        skipped.state = "skipped"
        skipped.detail = "实例还没有公网地址"
        await ctx.s.commit()
        return
    async with ctx.step(40, "probe_public", "公网探活") as st:
        res = await ctx.probe().check(inst.public_url)
        st.detail = (f"{_PROBE_MEANING.get(res.verdict, res.verdict)}" f"（status={res.status_code}）" + (f"：{res.hint}" if res.hint else ""))
        await ctx.s.commit()
        if res.verdict == "host_origin_mismatch":
            hint = res.hint or "检查 nginxWebUI 该站 location 的 Header Host 是否为 $host"
            await ctx.event("probe_degraded", f"重启后公网探活没通过：{_PROBE_MEANING.get(res.verdict, res.verdict)}",
                            {"status_code": res.status_code, "hint": hint})
