"""release 状态机（契约 §6.2）。顺序硬性：先断外网入口再停容器——
probe_guard → ng_delete → frp_delete → dns_delete → container_stop → mark_released。

硬边界：
- container_stop 只 `docker stop`。绝不允许 rm / volume prune / 删数据目录——
  实现层（DshRuntime.stop）与这里都不出现这类命令。
- 每删一个对象，把它的 id 与名字写进 events，出事能查到删了什么。
- 确认词在 API 层已逐字核对（不等直接 422）；probe_guard 再核一次库内一致性。
- provider = noop 的层：没有登记过对象就无从删起，步骤标 skipped 并打印要人工删什么。
"""

from __future__ import annotations

from ..db import repo
from ..db.models import utcnow
from ..domain.slug import derive
from .runner import JobCtx, StepFailed


def _manual(ctx: JobCtx, which: str) -> bool:
    """与 deploy 同：noop provider 对象带 manual_required=True（registry 自动落 noop 也算）。"""
    provider = getattr(ctx.providers, which, None)
    return bool(provider is not None and getattr(provider, "manual_required", False))


def _synthetic_id(v: object) -> bool:
    """空 / 0 / "manual" 一律视为「这一层当初是人工做的或没登记」：
    绝不发删除请求，只提示人工删除。曾经被污染的 "manual" 也在这里兜住。"""
    return v is None or v in ("", 0, "0", "manual")


async def run_release(ctx: JobCtx) -> None:
    inst = ctx.instance
    slug = inst.slug
    container = inst.container_name or slug
    names = derive(slug, ctx.settings.dns_zone)

    # ---- 10 probe_guard：确认词已在 API 层逐字核对；再核库内 slug 一致
    async with ctx.step(10, "probe_guard", f"核对确认词与实例 {slug}"):
        fresh = await repo.get_instance(ctx.s, inst.id)
        if fresh is None or fresh.slug != slug or not fresh.slug:
            raise StepFailed("实例 slug 与确认词不再一致，中止释放")

    # ---- 20 ng_delete
    async with ctx.step(20, "ng_delete", "删除反代站点") as st:
        if _synthetic_id(inst.ng_server_id) and _manual(ctx, "ng"):
            st.state = "skipped"
            st.detail = (f"NG 层未登记真实 id（当时为人工层）：请人工删除站点 "
                         f"{inst.dns_name} 与 upstream {names.upstream}")
            await ctx.s.commit()
            await ctx.event("release_ng_manual_pending",
                            f"NG 需人工删除: {inst.dns_name}",
                            {"server_name": inst.dns_name,
                             "upstream": inst.ng_upstream_name})
        elif inst.ng_server_id and not _synthetic_id(inst.ng_server_id):
            server_id = inst.ng_server_id
            await ctx.ng().delete_site(server_id, expect_name=inst.dns_name)
            await ctx.ng().reload()
            await ctx.event(
                "release_ng_deleted",
                f"已删除 NG 站点 server_id={server_id} name={inst.dns_name}",
                {"server_id": server_id, "server_name": inst.dns_name,
                 "upstream": inst.ng_upstream_name},
            )
            inst.ng_server_id = None
            inst.ng_location_id = None
            await ctx.s.commit()
        else:
            st.state = "skipped"
            st.detail = (f"无 NG 站点登记（server_id={inst.ng_server_id!r}）：跳过；"
                         f"若面板上存在 {inst.dns_name} 请人工删除")
            await ctx.s.commit()

    # ---- 30 frp_delete
    async with ctx.step(30, "frp_delete", "删除 frp 隧道") as st:
        if _synthetic_id(inst.frp_proxy_id) and _manual(ctx, "frp"):
            st.state = "skipped"
            st.detail = (f"frp 层未登记真实 id（当时为人工层）：请人工删除隧道 "
                         f"{names.tunnel}（remotePort {inst.frp_remote_port}）")
            await ctx.s.commit()
            await ctx.event("release_frp_manual_pending",
                            f"frp 需人工删除: {names.tunnel}",
                            {"name": names.tunnel, "remote_port": inst.frp_remote_port})
        elif inst.frp_proxy_id and not _synthetic_id(inst.frp_proxy_id):
            proxy_id = inst.frp_proxy_id
            await ctx.frp().delete(proxy_id, expect_name=names.tunnel)
            await ctx.event(
                "release_frp_deleted", f"已删除隧道 {names.tunnel} proxy_id={proxy_id}",
                {"proxy_id": proxy_id, "name": names.tunnel,
                 "remote_port": inst.frp_remote_port},
            )
            inst.frp_proxy_id = None
            await ctx.s.commit()
        else:
            st.state = "skipped"
            st.detail = (f"无 frp 隧道登记（proxy_id={inst.frp_proxy_id!r}）：跳过；"
                         f"若面板上存在 {names.tunnel} 请人工删除")
            await ctx.s.commit()

    # ---- 40 dns_delete
    async with ctx.step(40, "dns_delete", "删除 DNS A 记录") as st:
        if _synthetic_id(inst.dns_record_id) and _manual(ctx, "dns"):
            st.state = "skipped"
            st.detail = (f"DNS 层未登记真实 id（当时为人工层）：请人工删除 "
                         f"A 记录 {slug}（{inst.dns_name}）")
            await ctx.s.commit()
            await ctx.event("release_dns_manual_pending",
                            f"DNS 需人工删除: {slug}", {"rr": slug,
                                                       "dns_name": inst.dns_name})
        elif inst.dns_record_id and not _synthetic_id(inst.dns_record_id):
            record_id = inst.dns_record_id
            await ctx.dns().delete(record_id)
            await ctx.event(
                "release_dns_deleted", f"已删除 DNS 记录 {inst.dns_name} RecordId={record_id}",
                {"record_id": record_id, "rr": slug, "dns_name": inst.dns_name},
            )
            inst.dns_record_id = ""
            await ctx.s.commit()
        else:
            st.state = "skipped"
            st.detail = (f"无 DNS 记录登记（record_id={inst.dns_record_id!r}）：跳过；"
                         f"若解析存在 {inst.dns_name} 请人工删除")
            await ctx.s.commit()

    # ---- 50 container_stop：只 stop！不 rm、不动数据目录、不动镜像
    async with ctx.step(50, "container_stop", f"docker stop {container}"):
        r = await ctx.runtime().stop(container, line_sink=ctx.buffered_sink(_buf := []))
        await ctx.flush_lines(_buf)
        await ctx.log("info", f"docker stop -> exit {r.code}")
        if not r.ok:
            raise StepFailed(f"停止容器失败: {r.tail()}")
        await ctx.event("release_container_stopped",
                        f"容器已停止（容器/镜像/数据目录一律保留）: {container}",
                        {"container": container})

    # ---- 60 mark_released：终态，watcher 从此不再采集
    async with ctx.step(60, "mark_released", "标记 released"):
        inst.desired_state = "released"
        inst.released_at = utcnow()
        await repo.set_instance_state(ctx.s, inst, "released")
        await ctx.event("released", f"实例 {slug} 已释放", {"slug": slug})
