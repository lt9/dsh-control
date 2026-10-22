"""镜像制品发布工单（契约 §6.1.5）：源机 save 流进制品库 → 算指纹 → 落行。

全站工单：不挂实例（deploy_jobs.instance_id=0），源主机由 params.from_host_id
指定（runner 里换成 ctx.host）。两步：

    10 save     源机 `docker save <tag> | gzip -n` 的 stdout 流进
                DSHC_IMAGE_STORE/<file>.tmp（GB 级超时给足；进度按字节回显）
    20 record   算 sha256/size → 改名去 .tmp → **文件落定才插行/换行**——
                表里的行永远指向完整文件。同 tag 重发：行就地换（token 同步
                换新——旧链接立死，就是下架语义）；文件名由 tag 唯一决定，
                新文件原子覆盖旧文件，没有"旧文件"要单独删的余地。

发布走 images._save_to_store_sync 直连 subprocess：integrations/shell.py 的
执行通道是 JSON 文本，装不下 GB 级二进制流——与 ship 内存管道同一族。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from pathlib import Path

from ..db import repo
from ..db.models import utcnow
from ..images import SHIP_PROGRESS_EVERY, _save_to_store_sync
from .runner import StepFailed

_SAVE_TIMEOUT_HINT = 1800   # GB 级公网机 save 的超时口径（契约 §6.1.5）


def safe_tag(tag: str) -> str:
    """标签合法性：与 ship_image 同一口径，且绝不以 '-' 开头（远端命令拼串安全）。"""
    tag = (tag or "").strip()
    if not tag or tag.startswith("-") or " " in tag:
        raise StepFailed(f"镜像标签不合法: {tag!r}")
    return tag


def artifact_filename(tag: str) -> str:
    """`<tag 安全化>.tar.gz`（契约）：非 [A-Za-z0-9._-] 一律换成 '_'。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", tag) + ".tar.gz"


def new_download_token() -> str:
    """公开链接门票：随机 26 字符 urlsafe（token 即鉴权，撤销=删行删文件）。"""
    return secrets.token_urlsafe(19)   # 19 字节 → 26 个 urlsafe 字符


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 23)   # 8MB：GB 级文件不吃内存
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _store_ready(store: Path) -> str:
    """制品库可写预检：返回空串=就绪，否则人话原因（不静默降级，契约红线）。"""
    if not store.is_dir():
        return (f"制品库目录不存在: {store}——给控制台挂一个可写目录"
                "（如 -v /srv/dsh-console/images:/var/lib/dsh-control/images）"
                "并设 DSHC_IMAGE_STORE")
    if not os.access(store, os.W_OK | os.X_OK):
        return f"制品库目录不可写: {store}（检查挂载与属主）"
    return ""


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def run_publish(ctx) -> None:
    params = ctx.job.params or {}
    tag = safe_tag(str(params.get("tag") or ""))
    host = ctx.host
    store = Path(ctx.settings.image_store)
    filename = artifact_filename(tag)
    final = store / filename
    tmp = store / (filename + ".tmp")

    # ---- 10 save：源机 docker save | gzip -n 流进本机 <file>.tmp
    async with ctx.step(10, "image_save", f"从 {host.name} save {tag}") as st:
        why = _store_ready(store)
        if why:
            raise StepFailed(why)
        if not ctx.ssh_key_pem:
            raise StepFailed(f"主机 {host.name} 没有可用的 SSH 私钥，无法 save")
        conn = {"host": host.ssh_host, "port": host.ssh_port, "user": host.ssh_user,
                "private_key_pem": ctx.ssh_key_pem}
        await ctx.log("info", f"docker save {tag!r} | gzip -n -> {tmp}"
                              f"（每 {SHIP_PROGRESS_EVERY >> 20} MB 回显一行，"
                              f"按 {_SAVE_TIMEOUT_HINT // 60} 分钟量级给超时）")

        async def _stream() -> int:
            # 工作线程只往 notes 里塞话；日志由本协程泵出去（session 不跨线程）
            notes: list[str] = []
            worker = asyncio.ensure_future(asyncio.to_thread(
                _save_to_store_sync, conn, tag, str(tmp),
                lambda n: notes.append(
                    f"制品库已落 {n / (1 << 20):.0f} MB -> {filename}.tmp")))
            try:
                while not worker.done():
                    while notes:
                        await ctx.log("info", notes.pop(0))
                    await asyncio.sleep(0.2)
                while notes:
                    await ctx.log("info", notes.pop(0))
                return await worker
            finally:
                if not worker.done():
                    worker.cancel()

        try:
            total = await _stream()
        except RuntimeError as exc:
            _cleanup(tmp)
            raise StepFailed(str(exc)) from exc
        if total == 0:
            _cleanup(tmp)
            raise StepFailed(f"{tag} 的 save 流出 0 字节——标签真的存在吗？")
        # 半成品不许过夜：后面任何一步炸都把它扫掉（runner 的 rollback 是 LIFO）
        async def _drop_tmp() -> None:
            _cleanup(tmp)
        ctx.rollback_actions.append(("删除半成品 .tmp", _drop_tmp))
        st.detail = (f"源机 {host.name} `docker save | gzip -n` 流式落盘 "
                     f"{total} 字节（{total / (1 << 20):.1f} MB）")

    # ---- 20 record：指纹 → 改名落定 → 插行/换行（行存在即文件完整）
    async with ctx.step(20, "image_record", "算 sha256 并登记制品") as st:
        sha = await asyncio.to_thread(_sha256_of, tmp)
        size = tmp.stat().st_size
        prev = await repo.get_artifact_by_tag(ctx.s, tag)
        prev_filename = prev.filename if prev is not None else ""
        # 改名去 .tmp：os.replace 原子落定。从这一刻起 final 指向完整文件。
        await asyncio.to_thread(os.replace, str(tmp), str(final))

        async def _drop_final() -> None:
            _cleanup(final)
        ctx.rollback_actions.append(("删除已落定制品文件", _drop_final))

        token = new_download_token()
        if prev is None:
            await repo.add_artifact(ctx.s, tag=tag, filename=filename, size_bytes=size,
                                    sha256=sha, token=token, origin_host=host.name)
        else:
            # 同 tag 重发：行就地换（一次 UPDATE 提交 = 原子换行）。token 同步
            # 换新——旧链接的 token 再也查不到，"旧链接立死"由这一提交生效。
            prev.filename = filename
            prev.size_bytes = size
            prev.sha256 = sha
            prev.token = token
            prev.origin_host = host.name
            prev.created_at = utcnow()
            await ctx.s.commit()
        if prev_filename and prev_filename != filename:
            # 老登记的指向别的文件（例如旧的命名口径）：换行成功后才清旧文件
            _cleanup(store / prev_filename)
        if prev is not None:
            # 同 tag 重发=制品换了：外部渠道那份是旧货，打回 pending 等 30/40
            # 重推恢复 ok——不然第 20 步会兴冲冲去拉上一个版本。
            n = await repo.demote_channels(ctx.s, prev.id,
                                           "制品同 tag 重发，旧渠道副本作废，等重推")
            if n:
                await ctx.log("info", f"{n} 条渠道登记打回 pending（旧副本作废，30/40 重推）")
        st.detail = (f"{filename}：{size} 字节，sha256={sha[:16]}…，"
                     f"来源 {host.name}；公开链接门票已换新"
                     + ("（同 tag 重发，旧链接立死）" if prev is not None else ""))
        await ctx.event("image_published", f"镜像制品已发布：{tag}",
                        {"tag": tag, "filename": filename, "size_bytes": size,
                         "sha256": sha, "origin_host": host.name})

    # ---- 30 / 40：推外部渠道（§6.1.6）。前置不齐 skipped，推不动才红。
    await _mirror_step(ctx, 30, "github", tag, filename, store)
    await _mirror_step(ctx, 40, "gitee", tag, filename, store)


# ------------------------------------------------ 渠道推送（契约 §6.1.6，30/40 步）

async def _mirror_step(ctx, seq: int, channel: str, tag: str, filename: str,
                       store: Path) -> None:
    """单渠道推送一步：前置不齐就 skipped（不算失败），推不动才红。

    渠道行永远跟着最新制品行走（20 record 之后 artifact_id 已定）；成功落
    ok+url，失败落 failed+error 并 StepFailed——工单红着重跑，幂等。
    """
    title = "推送 GitHub Releases" if channel == "github" else "推送 Gitee Releases（分卷）"
    async with ctx.step(seq, f"mirror_{channel}", title) as st:
        ship = await repo.current_image_ship(ctx.s, default_base=ctx.settings.public_base)
        art = await repo.get_artifact_by_tag(ctx.s, tag)
        assert art is not None and art.filename == filename
        if not ship["auto_mirror"]:
            st.state = "skipped"
            st.detail = "auto_mirror=false：本次不推外部渠道（设置页可开）"
            return
        token = await repo.mirror_token(ctx.s, channel)
        repos = await repo.mirror_repos(ctx.s)
        repo_name = repos.get(channel, "")
        if not token:
            st.state = "skipped"
            st.detail = (f"{channel} 的 mirror_token 未配置（设置页写 token），"
                         "本步跳过——console 渠道不受影响")
            return
        if not repo_name:
            st.state = "skipped"
            st.detail = (f"{channel} 的仓库名未配置（mirrors.{channel}.repo，"
                         "形如 owner/name），本步跳过")
            return
        path = store / art.filename
        from . import mirror  # 局部 import：httpx 只在真要推送时进调用栈
        try:
            if channel == "github":
                url, rel_tag = await mirror.github_push(
                    token, repo_name, tag, art.filename, path, art.sha256,
                    art.size_bytes)
            else:
                url, rel_tag = await mirror.gitee_push(
                    token, repo_name, tag, art.filename, path, art.sha256,
                    art.size_bytes, log=lambda m: ctx.log("info", m))
        except mirror.MirrorError as exc:
            await repo.upsert_channel(ctx.s, artifact_id=art.id, channel=channel,
                                      state="failed", error=str(exc)[:1000])
            raise StepFailed(str(exc)) from exc
        row = await repo.upsert_channel(ctx.s, artifact_id=art.id, channel=channel,
                                        state="ok", url=url, release_tag=rel_tag,
                                        error="")
        st.detail = f"{url}（release {rel_tag}）"
        await ctx.event("image_channel_ok", f"制品 {tag} 已推上 {channel}",
                        {"tag": tag, "channel": channel, "url": url,
                         "release_tag": rel_tag})
        if channel == "gitee":
            # 仓库永远只留最新一版（1G 配额）：新版成了才删旧版，
            # 删不动只 warn——那是软约束，不许卡已经成功的发布。
            for old in await repo.other_ok_rows(ctx.s, "gitee", row.id):
                if not old.release_tag or old.release_tag == rel_tag:
                    continue
                try:
                    await mirror.gitee_delete_release(token, repo_name,
                                                      old.release_tag)
                except mirror.MirrorError as exc:
                    await ctx.log("warn", f"删旧 gitee release {old.release_tag} "
                                          f"失败（配额软约束，不拦）: {exc}")
                    continue
                old.state = "failed"
                old.error = "被更新版本替换，release 已删"
                await ctx.s.commit()
                await ctx.log("info", f"旧 gitee release 已删：{old.release_tag}")
