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

from collections.abc import Awaitable, Callable
from typing import Any

from ..db import repo
from ..db.models import utcnow
from ..domain.slug import derive
from ..integrations.base import IntegrationError, NotManaged
from .runner import JobCtx, StepFailed


def _manual(ctx: JobCtx, which: str) -> bool:
    """与 deploy 同：noop provider 对象带 manual_required=True（registry 自动落 noop 也算）。"""
    provider = getattr(ctx.providers, which, None)
    return bool(provider is not None and getattr(provider, "manual_required", False))


def _synthetic_id(v: object) -> bool:
    """空 / 0 / "manual" 一律视为「这一层当初是人工做的或没登记」：
    绝不发删除请求，只提示人工删除。曾经被污染的 "manual" 也在这里兜住。"""
    return v is None or v in ("", 0, "0", "manual")


async def idempotent_delete(log: Callable[[str, str], Awaitable[None]],
                            label: str, obj_name: str,
                            delete: Callable[[], Awaitable[None]],
                            find: Callable[[], Awaitable[Any]]) -> None:
    """释放/回滚的删除包装。区分"本来就没有"与"身份不明"两种查无此 id：

    - 面板按 id 查不到（NotManaged），且按我们该有那条的名字（隧道名/rr/站点名，
      都带我们自己的前缀）也查不到 → 对象本来就不在（被人工删过/重复释放），
      记 info 按已删除处理，重跑不报错；
    - 按名字查得到（但 id 对不上、或名字根本不是我们的）→ 原样上抛 NotManaged。
      "拒绝盲删"的红线在 integrations 层把守，这里只豁免前者，绝不放宽后者。
    """
    try:
        await delete()
    except NotManaged:
        if await find() is None:
            await log("info", f"{label} {obj_name} 面板上已不存在，视为已删除")
            return
        raise


async def delete_ng_upstream(ng: Any, upstream_id: int | None, upstream_name: str,
                             log: Callable[[str, str], Awaitable[None]]) -> bool:
    """删我们自己的 NG upstream 并回读确认（删站点时顺手带走，漏一次攒一条孤儿）。

    - 面板 provider 没有 delete_upstream（noop 层）→ warn 提示人工删，不算失败；
    - 按名字回读不到 → 本来就没有，info 通过（幂等）；
    - 删除后/身份对不上仍看得到 → **error 点名人工删**，返回 False。
      这一步不把释放拖失败：残留已大声记录，人比机器更会删一条 upstream。
      id 缺失时按名字找到谁删谁——upstream 名带 DSH_ 前缀守卫，删不到别人头上。
      _find_upstream 是 ng_panel 的扩展读接口：没有它就没有回读确认可言。
    """
    if not upstream_name:
        return True
    deleter = getattr(ng, "delete_upstream", None)
    if deleter is None:
        await log("warn", f"NG provider 不支持删 upstream：请人工在面板删除 {upstream_name}")
        return True
    find = getattr(ng, "_find_upstream", None)
    if find is not None and await find(upstream_name) is None:
        await log("info", f"NG upstream {upstream_name} 已不存在，跳过")
        return True
    target = upstream_id
    if target is None or target in (0, "0"):
        rec = await find(upstream_name) if find is not None else None
        if rec is None:
            await log("error", f"upstream {upstream_name} 无登记 id 且查不到定位，需人工删")
            return False
        target = int(rec["id"])
    try:
        await deleter(int(target), expect_name=upstream_name)
    except NotManaged as exc:
        if find is not None and await find(upstream_name) is None:
            await log("info", f"NG upstream {upstream_name} 已不存在，视为已删除")
            return True
        await log("error", f"NG 里残留 upstream {upstream_name}（{exc}），需人工删")
        return False
    except IntegrationError as exc:
        await log("error", f"NG 里残留 upstream {upstream_name}（删除失败：{exc}），需人工删")
        return False
    if find is not None and await find(upstream_name) is not None:
        await log("error", f"NG 里残留 upstream {upstream_name}（删除后仍可见），需人工删")
        return False
    await log("info", f"已删除 NG upstream {upstream_name}（回读确认不在）")
    return True


# 开通失败回滚后要清空的登记字段：那次部署什么都没留下，库里留着指向已不存在
# 对象的 id 就是假凭据——页面显示一条不存在的隧道，下一轮释放还拿死 id 去删。
# 含 frp_remote_port：回滚的实例没占住任何隧道，端口该放回池子给下一个实例
# （释放成功路径不同：那边按契约保留端口不复用，不走这里）。
# 键=字段名，值=清空后的缺省。
EXTERNAL_ID_FIELDS: dict[str, Any] = {
    "dns_record_id": "",
    "frp_proxy_id": None,
    "frp_remote_port": None,
    "ng_server_id": None,
    "ng_location_id": None,
    "ng_upstream_id": None,
    "ng_upstream_name": "",
}


async def clear_external_ids(ctx: JobCtx, inst: Any, *, reason: str) -> None:
    """清空指向已删对象的 id（含 frp 公网端口：清了才允许后续实例重新用这个端口）。"""
    cleared = {f: getattr(inst, f) for f in EXTERNAL_ID_FIELDS
               if getattr(inst, f, None) not in (None, "")}
    for f, default in EXTERNAL_ID_FIELDS.items():
        setattr(inst, f, default)
    await ctx.s.commit()
    if cleared:
        await ctx.event("external_ids_cleared",
                        f"{reason}，外部 id 已清空: {cleared}", cleared)


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
            await idempotent_delete(
                ctx.log, "NG 站点", inst.dns_name,
                lambda: ctx.ng().delete_site(server_id, expect_name=inst.dns_name),
                lambda: ctx.ng().find_site(inst.dns_name))
            await ctx.ng().reload()
            # 站点删了不删 upstream，就是在面板里攒孤儿（dsh-01 实测踩过）
            up_name = inst.ng_upstream_name or names.upstream
            up_ok = await delete_ng_upstream(ctx.ng(), inst.ng_upstream_id,
                                             up_name, ctx.log)
            await ctx.event(
                "release_ng_deleted",
                f"已删除 NG 站点 server_id={server_id} name={inst.dns_name}"
                + ("" if up_ok else f"；upstream {up_name} 残留，见 error 日志"),
                {"server_id": server_id, "server_name": inst.dns_name,
                 "upstream": up_name, "upstream_cleanup_ok": up_ok},
            )
            inst.ng_server_id = None
            inst.ng_location_id = None
            if up_ok:
                inst.ng_upstream_id = None
                inst.ng_upstream_name = ""
            await ctx.s.commit()
        else:
            # 站点没登记也要清 upstream：站点已删、upstream 忘了删正是残留形态
            up_name = inst.ng_upstream_name or names.upstream
            up_ok = await delete_ng_upstream(ctx.ng(), inst.ng_upstream_id,
                                             up_name, ctx.log)
            if up_ok:
                inst.ng_upstream_id = None
                inst.ng_upstream_name = ""
                await ctx.s.commit()
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
            client_id = ctx.host.frpc_client_id

            async def frp_still_there() -> object:
                # 双探测（D4）：登记的 proxy_id 还能查到东西 = 那个 id 上现在是
                # 别家的对象——即便我们的隧道名查无，也拒当"已删除"，更不许删它；
                # id 没了且我们的名字也查无，才是"本来就没有"。
                got = await ctx.frp().get(proxy_id)
                return got if got is not None else await ctx.frp().find_by_name(
                    names.tunnel, client_id)

            await idempotent_delete(
                ctx.log, "frp 隧道", names.tunnel,
                lambda: ctx.frp().delete(proxy_id, expect_name=names.tunnel),
                frp_still_there)
            await ctx.event(
                "release_frp_deleted", f"已删除隧道 {names.tunnel} proxy_id={proxy_id}",
                {"proxy_id": proxy_id, "name": names.tunnel,
                 "remote_port": inst.frp_remote_port},
            )
            inst.frp_proxy_id = None
            # frp_remote_port 故意保留（test_release 钉死的契约）：公网端口不复用，
            # 防新实例撞上一条对端还没清干净的旧隧道。只有开通失败回滚才放回池子。
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
            await idempotent_delete(
                ctx.log, "DNS 记录", slug,
                lambda: ctx.dns().delete(record_id),
                lambda: ctx.dns().find(slug))
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
