"""purge 状态机（契约 §6.4，released 的终态化）。

顺序与 release 对称：先补删释放时删掉一半的外部对象（若还挂着 id），再删容器，
最后删数据目录。purge 工单**只碰本控制台创建且登记在案的对象**：

| seq | key               | 做什么                                                     |
|-----|-------------------|------------------------------------------------------------|
|  10 | confirm_guard     | 手动核确认词==slug；自动核 source=auto 且保留期确实已过     |
|  20 | external_cleanup  | 库里还挂着的 dns/frp/ng id 逐个补删，404/查无视为已删       |
|  30 | container_remove  | docker rm 容器（还活着先 stop）；镜像不动                   |
|  40 | data_delete       | 删数据目录：路径过护栏才 rm -rf，不满足宁可不删直接失败     |
|  50 | mark_purged       | state=purged、purged_at=now、清外部 id、记 instance_purged   |

params：{"source": "manual", "confirm": "<slug>"} 或 {"source": "auto",
"purge_after_days": <watcher 现读值>}。手动确认词在 routes 层已逐字核过
（422 confirm_mismatch）；这里再核一遍 params——工单表里躺着的参数才是
执行时的事实，别信"入队时是对的"。

数据目录是用户数据，护栏（§6.4 宁可不删，绝不删错）逐条硬核对，任何一条
不满足直接 StepFailed：非空、绝对路径、normpath 后严格位于 hosts.data_root
之下（不是它本身）、slug 非空。
"""

from __future__ import annotations

import posixpath
import shlex
from datetime import UTC, datetime, timedelta
from typing import Any

from ..db import repo
from ..db.models import utcnow
from ..domain.slug import derive
from .release import (
    EXTERNAL_ID_FIELDS,
    _manual,
    _synthetic_id,
    clear_external_ids,
    delete_ng_upstream,
    idempotent_delete,
)
from .runner import JobCtx, StepFailed

# .env 的位置与 deploy 第 60/65 步完全同口径（kit 拥有实例文件，这里只读）
ENV_FILE_TMPL = "{kit}/instances/{slug}/.env"


async def env_data_dir(ctx: JobCtx, slug: str) -> str:
    """从实例 .env 读**生效的** DATA_DIR（与第 65 步同一份文件、同一个 grep 形状；
    env_facts 的键表不含 DATA_DIR，那边是配置快照的口径，这里不搭车）。

    读不到返回空串——契约 §6.4「路径**只认** .env 读回的生效值」，调用方据此
    直接失败（宁可不删），绝不拿 data_root/slug 猜一个路径去删。"""
    file = ENV_FILE_TMPL.format(kit=ctx.host.kit_path, slug=slug)
    r = await ctx.ssh(f"grep -E '^DATA_DIR=' {shlex.quote(file)} 2>/dev/null || true")
    facts: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        k, _, v = line.strip().partition("=")
        if k:
            facts[k] = v.strip().strip('"').strip("'")
    return facts.get("DATA_DIR") or ""


def guard_data_dir(data_dir: str, data_root: str, slug: str) -> str:
    """数据目录护栏：返回规范化后的路径；任何一条不满足 → StepFailed（宁可不删）。

    逐条（契约 §6.4 data_delete）：
    1. slug 非空——空 slug 的实例连"这份数据归谁"都说不清，没资格被删；
    2. 路径非空；
    3. 绝对路径；
    4. normpath 后严格 startswith(规范化的 data_root + "/")——前缀带斜杠，
       `/hdd1/dsh-data-extra` 这种兄弟目录不算在根之下；
    5. 不等于 data_root 本身——删根会把同机所有实例的数据一起带走。

    data_root 为空时 normpath("") 得 "."，第 4 条必然不满足，护栏自然拦下。
    """
    if not (slug or "").strip():
        raise StepFailed("data_delete 护栏：实例 slug 为空，无法确认数据目录归属，拒绝删除")
    raw = (data_dir or "").strip()
    if not raw:
        raise StepFailed("data_delete 护栏：数据目录为空，宁可不删")
    if not raw.startswith("/"):
        raise StepFailed(f"data_delete 护栏：数据目录 {raw!r} 不是绝对路径，宁可不删")
    root = posixpath.normpath((data_root or "").strip())
    path = posixpath.normpath(raw)
    if path == root:
        raise StepFailed(f"data_delete 护栏：{path} 就是主机数据根目录本身，"
                         "删它等于删同机所有实例的数据，拒绝")
    if not path.startswith(root + "/"):
        raise StepFailed(
            f"data_delete 护栏：数据目录 {path} 不严格位于主机数据根目录 "
            f"{root} 之下，宁可不删（绝不删错）")
    return path


async def run_purge(ctx: JobCtx) -> None:
    inst = ctx.instance
    host = ctx.host
    slug = inst.slug
    container = inst.container_name or slug
    names = derive(slug, ctx.settings.dns_zone)
    params: dict[str, Any] = dict(ctx.job.params or {})
    source = str(params.get("source") or "")
    deleted: dict[str, Any] = {}   # instance_purged 事件要交代「删了什么」

    # ---- 10 confirm_guard：手动核确认词，自动核保留期（每轮/每次现读库值）
    async with ctx.step(10, "confirm_guard", "核对确认词与保留期") as st:
        fresh = await repo.get_instance(ctx.s, inst.id)
        if fresh is None or fresh.slug != slug or not fresh.slug:
            raise StepFailed("实例 slug 与工单确认词不再一致，中止彻底释放")
        if fresh.state != "released":
            # 入队与执行之间实例可能被恢复（deploy 续建）：对跑着的实例
            # 删容器删数据是灾难，状态一变就地中止。
            raise StepFailed(f"实例当前状态是 {fresh.state}，不是 released——"
                             "中止彻底释放（中途被恢复的实例不许 purge）")
        if source == "manual":
            confirm = str(params.get("confirm") or "")
            if confirm != slug:
                raise StepFailed("工单里的确认词与 slug 不一致，中止彻底释放")
            st.detail = f"手动工单：确认词与 slug（{slug}）一致"
        elif source == "auto":
            # 保留期现读库里的 site 行（改了 DB 立即生效），不吃启动缓存。
            days = await repo.current_purge_after_days(ctx.s,
                                                       ctx.settings.purge_after_days)
            released_at = fresh.released_at
            if released_at is None:
                raise StepFailed("自动工单：实例没有 released_at，无法确认保留期已过，拒绝")
            if released_at.tzinfo is None:
                released_at = released_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - released_at <= timedelta(days=days):
                raise StepFailed(f"自动工单：保留期（现读 {days} 天）尚未过去，拒绝执行")
            st.detail = (f"自动工单：source=auto，released_at 起算保留期已过"
                         f"（现读保留期 {days} 天）")
        else:
            raise StepFailed('purge 工单 params 缺少合法的 source（"auto"/"manual"），拒绝')
        await ctx.s.commit()

    # ---- 20 external_cleanup：库里残留的 id 逐个补删（404/查无视为已删）
    async with ctx.step(20, "external_cleanup", "补删残留外部对象") as st:
        notes: list[str] = []
        # NG：站点 + 它名下 upstream（顺序照 release：先删引用方再删被引用方）
        if inst.ng_server_id and not _synthetic_id(inst.ng_server_id):
            server_id = inst.ng_server_id
            await idempotent_delete(
                ctx.log, "NG 站点", inst.dns_name,
                lambda: ctx.ng().delete_site(server_id, expect_name=inst.dns_name),
                lambda: ctx.ng().find_site(inst.dns_name))
            await ctx.ng().reload()
            up_name = inst.ng_upstream_name or names.upstream
            up_ok = await delete_ng_upstream(ctx.ng(), inst.ng_upstream_id,
                                             up_name, ctx.log)
            if up_ok:
                await ctx.ng().reload()   # 删完 upstream 再物化一次（同 release）
            await ctx.event("purge_ng_deleted",
                            f"purge 补删 NG 站点 server_id={server_id} name={inst.dns_name}",
                            {"server_id": server_id, "server_name": inst.dns_name,
                             "upstream": up_name, "upstream_cleanup_ok": up_ok})
            deleted["ng_server_id"] = server_id
            inst.ng_server_id = None
            inst.ng_location_id = None
            if up_ok:
                inst.ng_upstream_id = None
                inst.ng_upstream_name = ""
            await ctx.s.commit()
            notes.append("NG 站点已补删")
        elif inst.ng_upstream_name and not _manual(ctx, "ng"):
            # 站点没了但 upstream 名字还挂着：正是「删站点忘删 upstream」的残留形态
            up_ok = await delete_ng_upstream(ctx.ng(), inst.ng_upstream_id,
                                             inst.ng_upstream_name, ctx.log)
            if up_ok:
                deleted["ng_upstream"] = inst.ng_upstream_name
                inst.ng_upstream_id = None
                inst.ng_upstream_name = ""
                await ctx.s.commit()
            notes.append("NG upstream 已补删" if up_ok else "NG upstream 残留，见 error 日志")
        elif _manual(ctx, "ng") and (inst.ng_server_id or inst.ng_upstream_name):
            notes.append("NG 层为人工层：若面板上仍有 "
                         f"{inst.dns_name} / {inst.ng_upstream_name or names.upstream} 请人工删")
            await ctx.log("warn", notes[-1])
        else:
            notes.append("无 NG 登记残留")

        # frp：登记的 proxy_id；id 被面板换号时按名字找回真身（同 release 的红线）
        if inst.frp_proxy_id and not _synthetic_id(inst.frp_proxy_id):
            proxy_id = inst.frp_proxy_id
            client_id = host.frpc_client_id
            if await ctx.frp().get(proxy_id) is None:
                by_name = await ctx.frp().find_by_name(names.tunnel, client_id)
                if by_name is not None:
                    want = inst.frp_remote_port
                    if want is not None and by_name.remote_port != want:
                        raise StepFailed(
                            f"面板上 {names.tunnel} 的 remotePort={by_name.remote_port}，"
                            f"与登记的 {want} 不一致：那不是本控制台为这台实例建的隧道，"
                            "拒绝删除。请人工核对后再处理。")
                    await ctx.log("warn", f"登记的 proxy_id={proxy_id} 已不存在，"
                                          f"按名字找回真身 proxy_id={by_name.proxy_id}")
                    proxy_id = by_name.proxy_id

            async def frp_still_there() -> object:
                # 双探测（同 release D4）：闭包读校正后的 proxy_id——那个 id 上还能
                # 查到东西 = 别家的对象，即便我们的隧道名查无也拒当"已删除"。
                got = await ctx.frp().get(proxy_id)
                return got if got is not None else await ctx.frp().find_by_name(
                    names.tunnel, client_id)

            await idempotent_delete(
                ctx.log, "frp 隧道", names.tunnel,
                lambda: ctx.frp().delete(proxy_id, expect_name=names.tunnel),
                frp_still_there)
            await ctx.event("purge_frp_deleted",
                            f"purge 补删隧道 {names.tunnel} proxy_id={proxy_id}",
                            {"proxy_id": proxy_id, "name": names.tunnel,
                             "remote_port": inst.frp_remote_port})
            deleted["frp_proxy_id"] = proxy_id
            inst.frp_proxy_id = None
            await ctx.s.commit()
            notes.append("frp 隧道已补删")
        elif _manual(ctx, "frp") and inst.frp_remote_port:
            notes.append(f"frp 层为人工层：若面板上仍有隧道 {names.tunnel}"
                         f"（remotePort {inst.frp_remote_port}）请人工删")
            await ctx.log("warn", notes[-1])
        else:
            notes.append("无 frp 登记残留")

        # DNS：登记的 RecordId
        if inst.dns_record_id and not _synthetic_id(inst.dns_record_id):
            record_id = inst.dns_record_id
            await idempotent_delete(
                ctx.log, "DNS 记录", slug,
                lambda: ctx.dns().delete(record_id),
                lambda: ctx.dns().find(slug))
            await ctx.event("purge_dns_deleted",
                            f"purge 补删 DNS 记录 {inst.dns_name} RecordId={record_id}",
                            {"record_id": record_id, "rr": slug,
                             "dns_name": inst.dns_name})
            deleted["dns_record_id"] = record_id
            inst.dns_record_id = ""
            await ctx.s.commit()
            notes.append("DNS 记录已补删")
        elif _manual(ctx, "dns") and inst.dns_name:
            notes.append(f"DNS 层为人工层：若解析仍有 {inst.dns_name} 请人工删")
            await ctx.log("warn", notes[-1])
        else:
            notes.append("无 DNS 登记残留")

        st.detail = "；".join(notes)
        await ctx.s.commit()

    # ---- 30 container_remove：还活着先 stop 再 rm；镜像不动
    async with ctx.step(30, "container_remove", f"删除容器 {container}") as st:
        rt = ctx.runtime()
        snap = await rt.inspect(container)
        if snap is None:
            st.state = "skipped"
            st.detail = f"容器 {container} 不存在，无需删除"
            await ctx.s.commit()
        else:
            if snap.state == "running":
                r = await rt.stop(container, line_sink=ctx.buffered_sink(_buf := []))
                await ctx.flush_lines(_buf)
                await ctx.log("info", f"docker stop -> exit {r.code}")
                if not r.ok:
                    raise StepFailed(f"容器仍在运行且停止失败，拒绝 rm: {r.tail()}")
            r = await ctx.ssh(f"docker rm {shlex.quote(container)}")
            out = f"{r.stdout or ''}{r.stderr or ''}".lower()
            if not r.ok and "no such container" not in out:
                raise StepFailed(f"docker rm 失败: {r.tail()}")
            gone = "no such container" in out
            deleted["container"] = container
            st.detail = (f"容器 {container} 已删除（镜像不动）" if not gone
                         else f"容器 {container} 在 rm 前已不存在，视为已删")
            await ctx.event("purge_container_removed",
                            f"容器已删除（镜像保留）: {container}",
                            {"container": container})
            await ctx.s.commit()

    # ---- 40 data_delete：路径只认生效值 + 护栏，过护栏才 rm -rf
    async with ctx.step(40, "data_delete", "删除数据目录") as st:
        raw_dir = await env_data_dir(ctx, slug)
        # 契约 §6.4：路径**只认** .env 读回的生效值。读不到就宁可不删——
        # 拿 data_root/slug 猜路径，猜错一次就是删错别人的数据。
        if not raw_dir:
            raise StepFailed(
                "data_delete 护栏：读不到实例 .env 的生效 DATA_DIR，"
                "宁可不删（去目标机核对 .env 后重跑本步）")
        data_dir = guard_data_dir(raw_dir, host.data_root, slug)
        script = "\n".join([
            "set -e",
            f"p={shlex.quote(data_dir)}",
            ('if [ -d "$p" ]; then '
             'n=$(find "$p" | wc -l); rm -rf -- "$p"; '
             'echo "[data] DELETED files=$n"; '
             'else echo "[data] ABSENT"; fi'),
        ])
        r = await ctx.ssh(script, timeout=900)
        if not r.ok:
            raise StepFailed(f"删除数据目录失败（路径已核过护栏）: {r.tail()}")
        out = r.stdout or ""
        count = ""
        for line in out.splitlines():
            if "[data] DELETED" in line:
                count = line.strip().split("files=")[-1]
        deleted["data_dir"] = data_dir
        st.detail = (f"实例 .env 生效值：{data_dir}"
                     + (f" 已删除（{count} 个条目）" if "[data] DELETED" in out
                        else " 本来就不存在，视为已删"))
        await ctx.s.commit()

    # ---- 50 mark_purged：终态 + 清外部 id + instance_purged 审计
    async with ctx.step(50, "mark_purged", "标记已彻底释放"):
        inst.purged_at = utcnow()
        await repo.set_instance_state(ctx.s, inst, "purged")
        await clear_external_ids(ctx, inst, reason="彻底释放收尾",
                                 only=EXTERNAL_ID_FIELDS)
        await ctx.event("instance_purged",
                        f"实例 {slug} 已彻底释放（{'自动' if source == 'auto' else '手动'}）："
                        f"容器与数据目录已删，外部对象已清，记录保留只读",
                        {"source": source, "slug": slug, "container": container,
                         "deleted": deleted})
