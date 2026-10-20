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
           bufsize: int = 1 << 20) -> int:
    """内存管道：src.stdout → dst.stdin，返回搬运字节数。不落盘。"""
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
            f"源机 docker save 失败（exit {src_proc.returncode}）——"
            "先 cli images ls --host <源机> 确认标签在不在")
    if dst_rc != 0:
        raise RuntimeError(f"目标机 docker load 失败（exit {dst_rc}）")
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
