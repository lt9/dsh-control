"""镜像搬运（契约 §6.1.2：镜像是「准备好的版本」，开通阶段永不构建）。

- `ls`：列某主机上的 dsh 相关镜像标签与大小（命令行排障用，不提供 API/前端交互）。
- `ship`：两台之间**流式搬运**——源机 `docker save` 的 stdout 经本进程内存管道
  接目标机 `docker load` 的 stdin。不落盘；两台之间不要求任何互信，
  只走我们已有的两条 console→主机 SSH 通道（临时 0600 密钥文件用后即删，
  与 integrations/shell.py 同一策略）。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import tempfile
import time
from typing import Any

from .db import repo
from .db.models import Credential, Host
from .jobs.runner import get_executor

_LS_CMD = "docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}'"


def parse_image_lines(text: str) -> list[dict[str, str]]:
    """解析 docker images 的 `repo:tag<TAB>size` 行，只留 dsh 相关标签。"""
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        tag, _, size = line.partition("\t")
        tag = tag.strip()
        if not tag or "dsh" not in tag.lower():
            continue
        out.append({"tag": tag, "size": size.strip()})
    return out


async def find_host(s: Any, ref: str) -> Host:
    """--from/--to 按 hosts.name 或 ssh_host 匹配，必须唯一命中。"""
    hits = [h for h in await repo.list_hosts(s)
            if ref in (h.name, h.ssh_host) and h.enabled]
    if len(hits) == 1:
        return hits[0]
    raise RuntimeError(
        f"主机 {ref!r} 需要唯一匹配一台 enabled 主机（按 name 或 ssh_host），"
        f"实际命中 {len(hits)} 台")


async def _host_conn(s: Any, h: Host) -> dict[str, Any]:
    pem = ""
    if h.ssh_credential_id:
        cred = await s.get(Credential, h.ssh_credential_id)
        pem = (cred.secret or {}).get("private_key_pem", "") if cred else ""
    if not pem:
        raise RuntimeError(f"主机 {h.name} 没有可用的 SSH 私钥（ssh_credential_id 缺失）")
    return {"host": h.ssh_host, "port": h.ssh_port, "user": h.ssh_user,
            "private_key_pem": pem}


async def image_ls(db: Any, providers: Any, host_ref: str) -> list[dict[str, str]]:
    async with db.session() as s:
        h = await find_host(s, host_ref)
        conn = await _host_conn(s, h)
    res = await get_executor(providers).run(
        command=_LS_CMD, timeout=60, line_sink=None, **conn)
    if not res.ok:
        raise RuntimeError(f"{h.name} 列镜像失败: {res.tail()}")
    return parse_image_lines(res.stdout)


def _ssh_argv(conn: dict[str, Any], key_path: str) -> list[str]:
    return ["ssh", "-i", key_path, "-p", str(conn["port"]),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            f"{conn['user']}@{conn['host']}"]


def _tmp_key(pem: str) -> str:
    fd, path = tempfile.mkstemp(prefix="dshctl-ship-", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(pem if pem.endswith("\n") else pem + "\n")
    os.chmod(path, 0o600)
    return path


def _relay(src_proc: subprocess.Popen, dst_proc: subprocess.Popen,
           bufsize: int = 1 << 20, *,
           src_label: str = "源机 docker save",
           dst_label: str = "目标机 docker load") -> int:
    """内存管道：src.stdout → dst.stdin，返回搬运字节数。不落盘。

    报错文案按场景给（docker 搬运之外，字体库中转也走这条管道）。"""
    assert src_proc.stdout is not None and dst_proc.stdin is not None
    total = 0
    try:
        while True:
            chunk = src_proc.stdout.read(bufsize)
            if not chunk:
                break
            dst_proc.stdin.write(chunk)
            total += len(chunk)
        dst_proc.stdin.close()
    finally:
        try:
            src_proc.wait(timeout=60)
        finally:
            dst_rc = dst_proc.wait()
    if src_proc.returncode != 0:
        raise RuntimeError(
            f"{src_label}失败（exit {src_proc.returncode}）"
            + ("——先 cli images ls --host <源机> 确认标签在不在" if "docker" in src_label else ""))
    if dst_rc != 0:
        raise RuntimeError(f"{dst_label}失败（exit {dst_rc}）")
    return total


def _ship_sync(src_conn: dict, dst_conn: dict, tag: str) -> dict[str, Any]:
    key_src = key_dst = ""
    t0 = time.monotonic()
    try:
        key_src = _tmp_key(src_conn["private_key_pem"])
        key_dst = _tmp_key(dst_conn["private_key_pem"])

        sp = subprocess.Popen(
            [*_ssh_argv(src_conn, key_src), f"docker save {tag!r}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        dp = subprocess.Popen(
            [*_ssh_argv(dst_conn, key_dst), "docker load"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        total = _relay(sp, dp)
        loaded = (dp.stdout.read().decode("utf-8", "replace").strip()
                  if dp.stdout else "")
        return {"bytes": total, "seconds": round(time.monotonic() - t0, 1),
                "loaded": loaded}
    finally:
        for k in (key_src, key_dst):
            if k:
                try:
                    os.unlink(k)
                except OSError:
                    pass


async def ship_image(db: Any, providers: Any, *, tag: str,
                     src_ref: str, dst_ref: str) -> dict[str, Any]:
    if not tag or tag.startswith("-") or " " in tag:
        raise RuntimeError(f"镜像标签不合法: {tag!r}")
    async with db.session() as s:
        src_h = await find_host(s, src_ref)
        dst_h = await find_host(s, dst_ref)
        if src_h.id == dst_h.id:
            raise RuntimeError("源与目标是同一台主机，无需搬运")
        src_conn = await _host_conn(s, src_h)
        dst_conn = await _host_conn(s, dst_h)
    out = await asyncio.to_thread(_ship_sync, src_conn, dst_conn, tag)
    out.update({"src": src_h.name, "dst": dst_h.name, "tag": tag})
    return out


# ------------------------------------------------- 字体库中转（deploy 67 步）

def _fontlib_install_script(lib: str) -> str:
    """目标机接 tar.gz 落库：解到 `${lib}.install.$$` 临时目录 → cp -a 并进正式目录
    → 权限 644/755。先解临时目录再 cp 的意义：**中转断在半路时正式目录原封不动**，
    下次开通不会把半成品库当成真库直接铺。trap 兜底删临时目录。"""
    q = shlex.quote(lib)
    esc = (lib.replace("\\", "\\\\").replace('"', '\\"')
              .replace("$", "\\$").replace("`", "\\`"))
    return "\n".join([
        "set -e",
        f'tmp="{esc}.install.$$"',
        'trap \'rm -rf "$tmp"\' EXIT',
        'rm -rf "$tmp"; mkdir -p "$tmp"',
        'tar -xzf - -C "$tmp"',
        f'mkdir -p {q}',
        f'cp -a "$tmp"/. {q}/',
        f'find {q} -type d -exec chmod 755 {{}} +',
        f'find {q} -type f -exec chmod 644 {{}} +',
        f'echo "[fontlib] FILES=$(find {q} -type f | wc -l)"',
    ])


def _fontlib_relay_sync(src_conn: dict, dst_conn: dict, lib: str) -> dict[str, Any]:
    """源机 `tar -cf - -C <lib> . | gzip -n` 的 stdout 经本进程内存管道进目标机
    落库（形态同 _ship_sync：直连 subprocess、临时 0600 密钥用后即删，两台机器
    之间不需要互信）。只用 gzip——目标机未必有 zstd/xz。"""
    key_src = key_dst = ""
    t0 = time.monotonic()
    try:
        key_src = _tmp_key(src_conn["private_key_pem"])
        key_dst = _tmp_key(dst_conn["private_key_pem"])
        # 管道的退出码是 gzip 的：源机目录不存在时 tar 的失败会被吞掉、
        # 20 字节的空 gzip 一路"成功"到目标——先 [ -d ] 挡下，exit 41 见名知意。
        src_cmd = (f"[ -d {shlex.quote(lib)} ] || "
                   "{ echo 'no font library directory on source' >&2; exit 41; }; "
                   f"tar -cf - -C {shlex.quote(lib)} . | gzip -n")
        sp = subprocess.Popen(
            [*_ssh_argv(src_conn, key_src), src_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        dp = subprocess.Popen(
            [*_ssh_argv(dst_conn, key_dst), _fontlib_install_script(lib)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            total = _relay(sp, dp, src_label="源机字体库 tar",
                           dst_label="目标机字体库落库")
        except RuntimeError as exc:
            se = (sp.stderr.read().decode("utf-8", "replace").strip()
                  if sp.stderr else "")
            de = (dp.stderr.read().decode("utf-8", "replace").strip()
                  if dp.stderr else "")
            extra = f"｜源机: {se[-200:]}" if se else (f"｜目标机: {de[-200:]}" if de else "")
            raise RuntimeError(f"{exc}{extra}") from None
        out = dp.stdout.read().decode("utf-8", "replace") if dp.stdout else ""
        m = re.search(r"FILES=(\d+)", out)
        return {"bytes": total, "seconds": round(time.monotonic() - t0, 1),
                "files": int(m.group(1)) if m else -1}
    finally:
        for k in (key_src, key_dst):
            if k:
                try:
                    os.unlink(k)
                except OSError:
                    pass


async def relay_font_library(s: Any, src_ref: str,
                             dst_conn: dict[str, Any], lib: str) -> dict[str, Any]:
    """字体库中转：源主机（name 或 ssh_host）→ 控制台内存 → 目标（实例宿主机）。
    session 由调用方给（deploy 步骤复用 ctx.s）。"""
    src_h = await find_host(s, src_ref)
    src_conn = await _host_conn(s, src_h)
    out = await asyncio.to_thread(_fontlib_relay_sync, src_conn, dst_conn, lib)
    out["src"] = src_h.name
    return out


# ------------------------------------------------- 制品发布（契约 §6.1.5）

SHIP_PROGRESS_EVERY = 100 << 20   # 每落 ~100MB 回显一行进度（测试可 monkeypatch 调小）


def _save_to_store_sync(src_conn: dict, tag: str, dest_path: str,
                        on_progress=None, bufsize: int = 1 << 20) -> int:
    """源机 `docker save <tag> | gzip -n` 的 stdout 流进本机 dest_path，返回字节数。

    与 _relay 同一族：直连 subprocess（JSON 执行通道装不下二进制流），临时 0600
    密钥文件用后即删。只用 gzip——目标机未必有 zstd/xz，curl 与 sha256sum 全员自带。
    on_progress 从工作线程回调（发布工单里转成任务日志行），不许在这里直接写库。
    """
    key = _tmp_key(src_conn["private_key_pem"])
    total = 0
    next_mark = SHIP_PROGRESS_EVERY
    try:
        sp = subprocess.Popen(
            [*_ssh_argv(src_conn, key), f"docker save {tag!r} | gzip -n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert sp.stdout is not None
        try:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = sp.stdout.read(bufsize)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if on_progress is not None and total >= next_mark:
                        next_mark += SHIP_PROGRESS_EVERY
                        on_progress(total)
        finally:
            rc = sp.wait()
        if rc != 0:
            err = (sp.stderr.read().decode("utf-8", "replace").strip()
                   if sp.stderr else "")
            raise RuntimeError(
                f"源机 docker save 失败（exit {rc}）——先 cli images ls 确认标签在不在"
                + (f": {err}" if err else ""))
        return total
    finally:
        try:
            os.unlink(key)
        except OSError:
            pass
