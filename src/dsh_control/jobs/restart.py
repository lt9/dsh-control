"""restart 状态机（契约 §6.3）：
container_restart → wait_healthy（最多 120s）→ token_capture → probe_public。

重启必然轮换登录 token：token_capture 失败则整个 job 失败（标 error），
因为「详情永远是当前最新密钥」是本控制台的硬承诺；可再点重启或 /token 重抓。
"""

from __future__ import annotations

import asyncio

from ..db import repo
from .capture import capture_token
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
        await repo.add_snapshot(
            ctx.s, inst.id, docker_state=snap.state, health=snap.health,
            restart_count=snap.restart_count, cpu_pct=snap.cpu_pct,
            mem_usage_bytes=snap.mem_usage_bytes, mem_limit_bytes=snap.mem_limit_bytes,
            raw={"image_id": snap.image_id, "ip": snap.ip},
        )
        await ctx.s.commit()

    # ---- 30 token_capture：重启后必须重新抓最新 token 落库
    async with ctx.step(30, "token_capture", "重抓登录密钥"):
        tok = await capture_token(ctx.s, rt, inst, "restart")
        await ctx.log("info", "已抓到重启后的最新一键地址")
        await ctx.event("token_captured", "重启后重新抓到登录密钥",
                        {"source": "restart", "url": tok.url, "token": tok.token})

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
            await ctx.event("probe_degraded", f"重启后探活异常: {res.verdict}",
                            {"status_code": res.status_code, "hint": hint})
