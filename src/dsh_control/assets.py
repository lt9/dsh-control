"""站点内容资产：kit 脚本与字体库的版本化入库与统一下发。

存储位置按体量分（docs/SITE-CONFIG.md「资产分发」）：
- **kit 脚本**（约 100KB 文本）→ 数据库 `kit_releases.files`。发布是事务、
  取用一条查询、没有文件生命周期要管——小文本资产就该住在库里。
- **字体库**（约 370MB 二进制）→ 制品库目录（image_store，和镜像制品同盘同
  生命周期），库里只记 `fontlib_releases` 账本。「行存在即文件已完整落库」
  沿用 image_artifacts 硬约定（0008）：文件写完、改好名，才插行。

下发只有一条管道：**控制台内存 → ssh stdin → 目标机脚本解包**
（images._relay 同一族）。不让目标机 HTTP 出去拉——NAT 机器拨自家公网域是
hairpin 黑洞，而 ssh 永远可达（整个开通流程本来就建立在它上面）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import shlex
import subprocess
import tarfile
import time
from typing import Any

from .db import repo
from .images import _fontlib_install_script, _host_conn, _ssh_argv, _tmp_key, find_host

KIT_VERSION_FILE = ".dshctl-kit-version"
_VERSION_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# kit 的清单：deploy.sh 在 `cd "$(dirname $0)/.."` 之后用的就是 kit 根的
# compose.yaml，new-instance.sh 拿 .env.example 生成实例 .env——脚本三件套
# 加这两个模板就是开通的全部依赖。rootfs/plugins 是镜像的原料，不进 kit 包。
# capabilities/ 是 ADR-0001 决策五扩的一类货：能力包（manifest + 资产）随
# kit 整包换装到被管机，第 66 步引擎在机器上就地取用。目录是**可选**的——
# dsh-family 仓库还没收编 capabilities/ 之前，发布必须照常成功。
_KIT_DIRS = ("scripts",)
_KIT_DIRS_OPTIONAL = ("capabilities",)
_KIT_TOP = ("compose.yaml", ".env.example")
_KIT_SKIP = ("__pycache__", ".pyc", ".swp", "~")
_KIT_MAX_TOTAL = 20 << 20
_KIT_MAX_FILES = 200


def default_version(prefix: str) -> str:
    return time.strftime(f"{prefix}-%Y%m%d-%H%M%S")


def _check_version(version: str) -> str:
    if not _VERSION_RE.fullmatch(version):
        raise RuntimeError(f"版本号不合法（只许 [A-Za-z0-9._-]，≤64 字符）: {version!r}")
    return version


# ------------------------------------------------------------------ kit

def collect_kit_files(root: str) -> dict[str, dict[str, Any]]:
    """读 kit 仓库目录 → {路径: {b64, mode}}。清单外的东西一概不带。"""
    out: dict[str, dict[str, Any]] = {}
    total = 0

    def add(rel: str, abspath: str) -> None:
        nonlocal total
        with open(abspath, "rb") as f:
            data = f.read()
        total += len(data)
        if total > _KIT_MAX_TOTAL or len(out) >= _KIT_MAX_FILES:
            raise RuntimeError(
                f"kit 包过大（>{_KIT_MAX_TOTAL >> 20}MB 或 >{_KIT_MAX_FILES} 文件）："
                "确认清单没把 instances/、rootfs/ 这类不该进包的东西卷进来")
        mode = os.stat(abspath).st_mode & 0o777
        out[rel] = {"b64": base64.b64encode(data).decode(), "mode": mode}

    for d in _KIT_DIRS:
        droot = os.path.join(root, d)
        if not os.path.isdir(droot):
            raise RuntimeError(f"kit 目录缺 {d}/：{root} 是 dsh-family 仓库根吗？")
        for dirpath, dirnames, filenames in os.walk(droot):
            dirnames[:] = [x for x in dirnames if x not in _KIT_SKIP]
            for name in sorted(filenames):
                if any(name.endswith(x) for x in _KIT_SKIP):
                    continue
                ap = os.path.join(dirpath, name)
                add(os.path.relpath(ap, root), ap)
    for d in _KIT_DIRS_OPTIONAL:
        # 能力包一类货：有就带上（随整包换装到货），没有不报错（dsh-family
        # 还没收编 capabilities/ 是常态，不能因此挡了 kit 发布）
        droot = os.path.join(root, d)
        if not os.path.isdir(droot):
            continue
        for dirpath, dirnames, filenames in os.walk(droot):
            dirnames[:] = [x for x in dirnames if x not in _KIT_SKIP]
            for name in sorted(filenames):
                if any(name.endswith(x) for x in _KIT_SKIP):
                    continue
                ap = os.path.join(dirpath, name)
                add(os.path.relpath(ap, root), ap)
    for name in _KIT_TOP:
        ap = os.path.join(root, name)
        if not os.path.isfile(ap):
            raise RuntimeError(f"kit 根目录缺 {name}（deploy.sh/new-instance.sh 依赖它）")
        add(name, ap)
    return out


def kit_tar(files: dict[str, dict[str, Any]], version: str) -> bytes:
    """整包 → tar.gz 字节流（含版本标记文件）。mtime/uid 归零：同内容同字节。"""
    buf = io.BytesIO()

    def put(path: str, data: bytes, mode: int) -> None:
        ti = tarfile.TarInfo(path)
        ti.size, ti.mode, ti.mtime, ti.uid, ti.gid = len(data), mode, 0, 0, 0
        tf.addfile(ti, io.BytesIO(data))

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(files):
            meta = files[path]
            put(path, base64.b64decode(meta["b64"]), int(meta.get("mode", 0o644)))
        put(KIT_VERSION_FILE, f"{version}\n".encode(), 0o644)
    return buf.getvalue()


def _kit_install_script(kit_dir: str, version: str) -> str:
    """目标机落位：解包只增盖清单内文件（tar 不删任何东西），instances/ 原样。
    scripts 一律给可执行位——kit 脚本从仓库到库里 mode 可能丢，这里兜底。"""
    q = shlex.quote(kit_dir)
    return "\n".join([
        "set -e",
        f"mkdir -p {q}",
        f"tar -xzf - -C {q}",
        f"chmod 755 {q}/scripts/*.sh 2>/dev/null || true",
        f"echo {shlex.quote(version)} > {q}/{KIT_VERSION_FILE}",
        'echo "[kit] INSTALLED"',
    ])


def _deliver_sync(dst_conn: dict[str, Any], payload: bytes | str,
                  script: str) -> str:
    """payload（内存字节流或**本机文件路径**，按 1MB 分块流式）灌进目标机
    script 的 stdin；返回 stdout，非零退出把 stderr 尾巴带进异常。"""
    key = _tmp_key(dst_conn["private_key_pem"])
    try:
        dp = subprocess.Popen(
            [*_ssh_argv(dst_conn, key), script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if isinstance(payload, (bytes, bytearray)):
                for i in range(0, len(payload), 1 << 20):
                    dp.stdin.write(payload[i:i + (1 << 20)])
            else:
                with open(payload, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        dp.stdin.write(chunk)
            dp.stdin.close()
        finally:
            rc = dp.wait()
        out = dp.stdout.read().decode("utf-8", "replace") if dp.stdout else ""
        if rc != 0:
            err = dp.stderr.read().decode("utf-8", "replace") if dp.stderr else ""
            raise RuntimeError(f"目标机下发脚本失败（exit {rc}）: {err[-300:]}")
        return out
    finally:
        try:
            os.unlink(key)
        except OSError:
            pass


async def install_kit(dst_conn: dict[str, Any], files: dict[str, dict[str, Any]],
                      version: str, kit_dir: str) -> dict[str, Any]:
    payload = await asyncio.to_thread(kit_tar, files, version)
    await asyncio.to_thread(_deliver_sync, dst_conn, payload,
                            _kit_install_script(kit_dir, version))
    return {"bytes": len(payload), "version": version, "files": len(files)}


async def install_fontlib(dst_conn: dict[str, Any], store_file: str,
                          lib: str) -> dict[str, Any]:
    """把制品库里的字体库文件流进目标机落库（解包规则同字体中转：临时目录 +
    cp -a，断线不污染正式库）。"""
    out = await asyncio.to_thread(_deliver_sync, dst_conn, store_file,
                                  _fontlib_install_script(lib))
    m = re.search(r"FILES=(\d+)", out)
    return {"files": int(m.group(1)) if m else -1}


# ------------------------------------------------------------------ 发布

async def publish_kit(db: Any, root: str, *, version: str = "",
                      note: str = "") -> dict[str, Any]:
    version = _check_version(version or default_version("kit"))
    files = await asyncio.to_thread(collect_kit_files, root)
    size = sum(len(base64.b64decode(m["b64"])) for m in files.values())
    # 先校验能力包再动库：任何一包不合（未知字段/超阈值/引用缺失）整次发布
    # 拒掉，一行都不写——「宁可不装，不要半装」，也不会出现 kit 新、目录旧。
    from .integrations import capability_engine
    raw = {rel: base64.b64decode(meta["b64"]) for rel, meta in files.items()}
    packs = capability_engine.packs_from_files(raw)
    async with db.session() as s:
        row = await repo.add_kit_release(s, version=version, files=files,
                                         size_bytes=size, file_count=len(files),
                                         note=note)
        # 同一次发布 = 同一版本的一批货：kit 行落定就顺手刷能力目录
        # （ADR 决策四「目录行由谁写」就此消解），同一 session 内完成。
        if packs:
            await capability_engine.upsert_catalog(s, packs, kit_version=version,
                                                   note=note)
        return {"version": row.version, "files": row.file_count, "size_bytes": size,
                "capabilities": [f"{p.capability_id}@{p.version}" for p in packs]}


def _stream_to_file_with_sha(proc: subprocess.Popen, dest: str) -> tuple[int, str]:
    """proc.stdout 流式写 dest（+.part 落定改名前由调用方负责），边写边算 sha256。"""
    h = hashlib.sha256()
    total = 0
    assert proc.stdout is not None
    with open(dest, "wb") as f:
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            total += len(chunk)
    rc = proc.wait(timeout=60)
    if rc != 0:
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        raise RuntimeError(f"字体库打包流失败（exit {rc}）: {err[-300:]}")
    if total == 0:
        raise RuntimeError("字体库打包出来是空的——检查库目录内容")
    return total, h.hexdigest()


def _publish_fontlib_local_sync(lib: str, dest: str) -> tuple[int, str, int]:
    count = sum(len(fs) for _, _, fs in os.walk(lib))
    p = subprocess.Popen(f"tar -cf - -C {shlex.quote(lib)} . | gzip -n",
                         shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size, sha = _stream_to_file_with_sha(p, dest)
    return size, sha, count


def _publish_fontlib_host_sync(conn: dict[str, Any], lib: str,
                               dest: str) -> tuple[int, str, int]:
    q = shlex.quote(lib)
    key = _tmp_key(conn["private_key_pem"])
    try:
        # 管道退出码是 gzip 的：目录不存在先 [ -d ] 挡下（同 _fontlib_relay_sync 的坑）
        cmd = (f"[ -d {q} ] || {{ echo 'no font library directory on source' >&2; exit 41; }}; "
               f"tar -cf - -C {q} . | gzip -n")
        p = subprocess.Popen([*_ssh_argv(conn, key), cmd],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        size, sha = _stream_to_file_with_sha(p, dest)
        n = subprocess.run([*_ssh_argv(conn, key), f"find {q} -type f | wc -l"],
                           capture_output=True, text=True, timeout=120, check=False)
        m = re.search(r"\d+", n.stdout or "")
        return size, sha, int(m.group()) if m else -1
    finally:
        try:
            os.unlink(key)
        except OSError:
            pass


async def publish_fontlib(db: Any, store_dir: str, *, version: str = "",
                          note: str = "", local_dir: str = "",
                          host_ref: str = "", lib: str = "") -> dict[str, Any]:
    """字体库进制品库：`--dir` 本机目录直接打包，或 `--host <机器> --lib <路径>`
    从来源主机流式拉（控制台机器通常没有 NAS 挂载，后者是常态入口）。

    先写 *.part 再改名再插行——「行存在即文件已完整落库」（0008 约定）。
    """
    version = _check_version(version or default_version("fontlib"))
    if not local_dir and not host_ref:
        raise RuntimeError("publish 要 --dir <本机目录> 或 --host <来源主机> --lib <路径>")
    os.makedirs(store_dir, exist_ok=True)
    dest = os.path.join(store_dir, f"{version}.tar.gz")
    part = dest + ".part"
    origin = ""
    try:
        if host_ref:
            async with db.session() as s:
                h = await find_host(s, host_ref)
                conn = await _host_conn(s, h)
            origin = h.name
            size, sha, count = await asyncio.to_thread(
                _publish_fontlib_host_sync, conn, lib, part)
        else:
            if not os.path.isdir(local_dir):
                raise RuntimeError(f"目录不存在: {local_dir}")
            origin = local_dir
            size, sha, count = await asyncio.to_thread(
                _publish_fontlib_local_sync, local_dir, part)
        os.replace(part, dest)
        async with db.session() as s:
            row = await repo.add_fontlib_release(
                s, version=version, filename=os.path.basename(dest),
                size_bytes=size, sha256=sha, file_count=count,
                origin=origin, note=note)
            return {"version": row.version, "files": count, "size_bytes": size,
                    "sha256": sha[:12], "filename": row.filename}
    finally:
        if os.path.exists(part):
            try:
                os.unlink(part)
            except OSError:
                pass
