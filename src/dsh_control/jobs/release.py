"""release 状态机（契约 §6.2）。顺序硬性：先断外网入口再停容器——
probe_guard → ng_delete → frp_delete → dns_delete → container_stop → mark_released。

硬边界：
- container_stop 只 `docker stop`。绝不允许 rm / volume prune / 删数据目录——
  实现层（DshRuntime.stop）与这里都不出现这类命令。
- 每删一个对象，把它的 id 与名字写进 events，出事能查到删了什么。
- 确认词在 API 层已逐字核对（不等直接 422）；probe_guard 再核一次库内一致性。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repo
from ..db.models import utcnow
from .deploy import frp_tunnel_name
from .runner import JobCtx, StepFailed


async def run_release(ctx: JobCtx) -> None:
    inst = ctx.instance
    slug = inst.slug
    container = inst.container_name or slug

    # ---- 10 probe_guard：确认词已逐字核对过；再核对库里 slug / 容器名一致
    async with ctx.step(10, "probe_guard", f"核对确认词与实例 {slug}"):
        fresh = await repo.get_instance(ctx.s, inst.id)
        if fresh is None or fresh.slug != slug:
            raise StepFailed("实例 slug 与确认词不再一致，中止释放")
        if not fresh.slug:
            raise StepFailed("实例缺少 slug，无法核对确认词")

    # ---- 20 ng_delete
    async with ctx.step(20, "ng_delete", "删除 nginxWebUI 站点"):
        if inst.ng_server_id:
            await ctx.ng().delete_site(inst.ng_server_id, expect_name=inst.dns_name)
            await ctx.ng().reload()
            await ctx.event(
                "release_ng_deleted",
                f"已删除 NG 站点 server_id={inst.ng_server_id} name={inst.dns_name}",
                {"server_id": inst.ng_server_id, "server_name": inst.dns_name,
                 "upstream": inst.ng_upstream_name},
            )
            inst.ng_server_id = None
            inst.ng_location_id = None
            await ctx.s.commit()
        else:
            await ctx.log("info", "无 NG 站点登记，跳过")

    # ---- 30 frp_delete
    async with ctx.step(30, "frp_delete", "删除 frp 隧道"):
        if inst.frp_proxy_id:
            name = frp_tunnel_name(slug)
            await ctx.frp().delete(inst.frp_proxy_id, expect_name=name)
            await ctx.event(
                "release_frp_deleted", f"已删除隧道 {name} proxy_id={inst.frp_proxy_id}",
                {"proxy_id": inst.frp_proxy_id, "name": name,
                 "remote_port": inst.frp_remote_port},
            )
            inst.frp_proxy_id = None
            await ctx.s.commit()
        else:
            await ctx.log("info", "无 frp 隧道登记，跳过")

    # ---- 40 dns_delete
    async with ctx.step(40, "dns_delete", "删除 DNS A 记录"):
        if inst.dns_record_id:
            record_id = inst.dns_record_id
            await ctx.dns().delete(record_id)
            await ctx.event(
                "release_dns_deleted", f"已删除 DNS 记录 {inst.dns_name} RecordId={record_id}",
                {"record_id": record_id, "rr": slug, "dns_name": inst.dns_name},
            )
            inst.dns_record_id = ""
            await ctx.s.commit()
        else:
            await ctx.log("info", "无 DNS 记录登记，跳过")

    # ---- 50 container_stop：只 stop！不 rm、不动数据目录、不动镜像
    async with ctx.step(50, "container_stop", f"docker stop {container}"):
        r = await ctx.runtime().stop(container, line_sink=ctx.buffered_sink(_buf := []))
        await ctx.flush_lines(_buf)
        await ctx.log("info", f"docker stop -> exit {r.code}")
        if not r.ok:
            raise StepFailed(f"停止容器失败: {r.tail()}")
        await ctx.event("release_container_stopped",
                        f"容器已停止（保留容器/镜像/数据目录）: {container}",
                        {"container": container})

    # ---- 60 mark_released：终态，watcher 从此不再采集
    async with ctx.step(60, "mark_released", "标记 released"):
        inst.desired_state = "released"
        inst.released_at = utcnow()
        await repo.set_instance_state(ctx.s, inst, "released")
        await ctx.event("released", f"实例 {slug} 已释放", {"slug": slug})


async def release_no_active_job(s: AsyncSession, instance_id: int) -> bool:
    """释放前不该再有别的在途任务。"""
    from ..db.models import DeployJob
    from sqlalchemy import select

    n = await s.scalar(
        select(DeployJob.id)
        .where(DeployJob.instance_id == instance_id,
               DeployJob.state.in_(("pending", "running")))
        .limit(1)
    )
    return n is None
