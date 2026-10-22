"""制品镜像到公共 Releases（契约 §6.1.6）：github 整包 asset / gitee 分卷 + manifest。

两个平台都走 httpx.AsyncClient，统一从 _client() 拿——测试 monkeypatch 它塞
MockTransport，不碰真实网络。所有失败都收敛成 MirrorError（带人话原因），
由 publish 的 30/40 步落渠道行 failed 并红工单。

gitee 硬限制单附件 ≤100M、仓库附件 ≤1G：`split -b 95m` 分卷 + 一个
`<file>.manifest.json`（单行 JSON、parts 按序、url 全是可 curl 直取的直链），
仓库永远只留最新一版——新版发布成功后删旧 release，删失败只 warn（配额是
软约束，别为它卡主流程）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

GITHUB_API = "https://api.github.com"
GITEE_API = "https://gitee.com/api/v5"
GITEE_PART_SIZE = "95m"        # 官方口径 ≤100M，取 95m 留头（测试 monkeypatch 调小）
_UPLOAD_CHUNK = 1 << 20


class MirrorError(RuntimeError):
    """渠道推送失败：message 直接进渠道行 error 与工单，先过调用方的 redact。"""


def _js(resp) -> dict:
    """响应体一律过成 dict 再 .get——gitee 真机实证：查不存在的 release 回
    **HTTP 200 + 正文 null**（不是 404），`json().get` 当场 NoneType 崩（task-7）。
    空 body / 非 JSON 同样按「没这个键」处理，不许在解析层炸。"""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _ja(resp) -> list:
    """列表版：200 + null / 空 body / 非列表一律当空。"""
    try:
        data = resp.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _client() -> httpx.AsyncClient:
    """唯一出口：测试 monkeypatch 本函数注入 MockTransport handler。"""
    return httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0),
                             follow_redirects=True)


def release_tag_for(tag: str) -> str:
    """release tag = `image-<tag 安全化>`（契约）：与制品文件同一套安全化。"""
    return "image-" + re.sub(r"[^A-Za-z0-9._-]", "_", tag)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 23)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _release_body(tag: str, sha: str, size: int) -> str:
    return ("面板自动发布的镜像制品（dsh-control §6.1.6），勿手改。\n\n"
            f"- image tag: `{tag}`\n- sha256: `{sha}`\n- size: {size} bytes")


# ---------------------------------------------------------------- github

async def github_push(token: str, repo: str, tag: str, filename: str,
                      path: Path, sha: str, size: int) -> tuple[str, str]:
    """建/复用 release → 传整包 asset。返回 (asset 直链, release_tag)。"""
    tag_name = release_tag_for(tag)
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}

    def denied(resp, what: str) -> MirrorError:
        return MirrorError(
            f"{what}：GitHub 拒了 token（HTTP {resp.status_code}）——token 不能走 API？"
            "classic token 要 repo scope，fine-grained 要对该仓库 Contents:write")

    async with _client() as c:
        payload = {"tag_name": tag_name, "name": f"image {tag}",
                   "body": _release_body(tag, sha, size)}
        r = await c.post(f"{GITHUB_API}/repos/{repo}/releases",
                         headers=headers, json=payload)
        if r.status_code in (401, 403):
            raise denied(r, "建 release")
        if r.status_code == 422:
            # 同 tag 已存在：拿 id 复用（幂等重跑的正路）
            g = await c.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag_name}",
                            headers=headers)
            if g.status_code in (401, 403):
                raise denied(g, "查 release")
            if g.status_code != 200:
                raise MirrorError(
                    f"GitHub release {tag_name} 说存在但查不回来"
                    f"（HTTP {g.status_code}）：{g.text[:200]}")
            rel = g.json()
            p = await c.patch(f"{GITHUB_API}/repos/{repo}/releases/{rel['id']}",
                              headers=headers,
                              json={"name": payload["name"], "body": payload["body"]})
            if p.status_code in (401, 403):
                raise denied(p, "改 release")
        elif r.status_code not in (200, 201):
            raise MirrorError(f"GitHub 建 release 失败 HTTP {r.status_code}: "
                              f"{r.text[:200]}")
        else:
            rel = r.json()
        upload_url = str(rel.get("upload_url") or "").split("{")[0]
        if not upload_url:
            raise MirrorError("GitHub release 响应里没有 upload_url")
        # 幂等：同名 asset 先删再传（重跑不许攒重复 asset）
        a = await c.get(f"{GITHUB_API}/repos/{repo}/releases/{rel['id']}/assets",
                        headers=headers)
        for old in (a.json() if a.status_code == 200 else []):
            if old.get("name") == filename:
                await c.delete(f"{GITHUB_API}/repos/{repo}/releases/assets/{old['id']}",
                               headers=headers)

        # uploads.github.com 不吃 chunked——E2E 真机 400 Bad Content-Length（跑了
        # 177s 才被拒）。流式 body 也必须带精确的 Content-Length：httpx 见手设的
        # CL 就不再自动塞 Transfer-Encoding: chunked，h11 还会校验实发字节数与头
        # 一致（对不上是传输层翻车，下面的计数守卫先一步给人话错误）。
        file_size = await asyncio.to_thread(lambda: path.stat().st_size)

        def _guarded_stream():
            sent = 0

            async def _astream():   # 流式读文件：GB 级整包不许进内存；读盘进线程
                nonlocal sent
                with path.open("rb") as f:
                    while True:
                        b = await asyncio.to_thread(f.read, _UPLOAD_CHUNK)
                        if not b:
                            break
                        sent += len(b)
                        if sent > file_size:
                            raise MirrorError(
                                f"实际文件比 {file_size} 字节大：制品行与盘上文件漂移，"
                                "拒传——撒谎的 Content-Length 比 chunked 更糟")
                        yield b
                if sent != file_size:
                    raise MirrorError(
                        f"只发出 {sent}/{file_size} 字节，文件读到一半没了：拒传")

            return _astream()

        up = await c.post(f"{upload_url}?name={quote(filename)}",
                          headers={**headers,
                                   "Content-Type": "application/octet-stream",
                                   "Content-Length": str(file_size)},
                          content=_guarded_stream())
        if up.status_code in (401, 403):
            raise denied(up, "传 asset")
        if up.status_code not in (200, 201):
            raise MirrorError(f"GitHub 传 asset 失败 HTTP {up.status_code}: "
                              f"{up.text[:200]}")
    return (f"https://github.com/{repo}/releases/download/{tag_name}/{filename}",
            tag_name)


# ---------------------------------------------------------------- gitee

def _split_parts(path: Path, tmpdir: Path) -> list[Path]:
    """`split -b 95m` 出 .part-aa…（同步函数，放 to_thread 里跑）。

    前缀就写成 `<file>.part-`：split 把 aa/ab 直接缀在 prefix 尾巴上，
    得到契约点名的 `<file>.part-aa` 形状。
    """
    subprocess.run(["split", "-b", GITEE_PART_SIZE, str(path),
                    str(tmpdir / (path.name + ".part-"))],
                   check=True)
    return sorted(tmpdir.glob(path.name + ".part-*"))


async def gitee_push(token: str, repo: str, tag: str, filename: str,
                     path: Path, sha: str, size: int,
                     log=None) -> tuple[str, str]:
    """分卷传 release + manifest。返回 (manifest 直链, release_tag)。"""
    tag_name = release_tag_for(tag)
    tok = {"access_token": token}    # gitee 的 token 走表单参数，不是头

    def denied(what: str, code: int) -> MirrorError:
        return MirrorError(f"{what}：Gitee 拒了 token（HTTP {code}）——"
                           "token 不能走 API？检查私人令牌有没有 projects 权限")

    async def attach(c, base: str, rel_id, file_path: Path) -> str:
        """multipart 传单附件，字段名 file（官方口径），回 browser_download_url。

        自查结论（task-6）：httpx files= 会把整卷读进内存再发（multipart 没有
        流式口径），所以**内存上限 = 一卷 ≈95MB**，不是制品全量——这正是分卷
        切 95m 的另一层意义。手拼流式 multipart 要自己管 boundary 偏移和长度，
        超出必要复杂度，不动。
        """
        with file_path.open("rb") as fh:
            up = await c.post(f"{base}/releases/{rel_id}/attach_files", data=tok,
                              files={"file": (file_path.name, fh,
                                              "application/octet-stream")})
        if up.status_code in (401, 403):
            raise denied("传附件", up.status_code)
        if up.status_code not in (200, 201):
            raise MirrorError(f"Gitee 传 {file_path.name} 失败 HTTP "
                              f"{up.status_code}: {up.text[:200]}")
        url = str(_js(up).get("browser_download_url") or "")
        if not url:
            raise MirrorError(f"{file_path.name} 传上去但响应里没有 "
                              "browser_download_url——直链口径变了，得核对 API")
        return url

    parts_meta: list[dict] = []
    async with _client() as c:
        base = f"{GITEE_API}/repos/{repo}"
        g = await c.get(f"{base}/releases/tags/{tag_name}", params=tok)
        if g.status_code in (401, 403):
            raise denied("查 release", g.status_code)
        gj = _js(g)          # 200 + null（不存在）是 gitee 的正常回法，全线先过守卫
        if g.status_code == 200 and gj.get("id"):
            rel_id = gj["id"]
            # 同 tag 复用：旧附件清空重传（幂等重跑不许攒重复卷）
            assets = await c.get(f"{base}/releases/{rel_id}/assets", params=tok)
            for old in (_ja(assets) if assets.status_code == 200 else []):
                if old.get("id"):
                    await c.delete(f"{base}/releases/{rel_id}/assets/{old['id']}",
                                   params=tok)
        else:
            r = await c.post(f"{base}/releases",
                             data={**tok, "tag_name": tag_name,
                                   # gitee 建版 name 与 target_commitish 皆必填
                                   # （400 实测，官方文档口径一致）；快照链恒推
                                   # main，tag 就从它起
                                   "name": f"image {tag}",
                                   "target_commitish": "main",
                                   "body": _release_body(tag, sha, size)})
            if r.status_code in (401, 403):
                raise denied("建 release", r.status_code)
            rj = _js(r)
            if r.status_code not in (200, 201) or not rj.get("id"):
                raise MirrorError(f"Gitee 建 release 失败 HTTP {r.status_code}: "
                                  f"{r.text[:200]}")
            rel_id = rj["id"]

        tmp = Path(tempfile.mkdtemp(prefix="dshctl-mirror-"))
        try:
            parts = await asyncio.to_thread(_split_parts, path, tmp)
            if not parts:
                raise MirrorError("split 没切出任何分卷（文件是空的？）")
            for part in parts:
                psha = await asyncio.to_thread(_sha256_file, part)
                psize = part.stat().st_size
                url = await attach(c, base, rel_id, part)
                parts_meta.append({"name": part.name, "size": psize,
                                   "sha256": psha, "url": url})
                if log:
                    await log(f"gitee 卷 {part.name} 已传（{psize >> 20} MB）")
            # manifest：单行 JSON、无嵌套转义、parts 按序——目标机纯 POSIX
            # （grep -o '"url":"http…"'）就能按序抽直链，不赌它装没装 jq/python3。
            manifest = json.dumps(
                {"file": filename, "sha256": sha, "size": size, "parts": parts_meta},
                separators=(",", ":"), ensure_ascii=False)
            mpath = tmp / (filename + ".manifest.json")
            mpath.write_text(manifest, encoding="utf-8")
            manifest_url = await attach(c, base, rel_id, mpath)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return manifest_url, tag_name


async def gitee_delete_release(token: str, repo: str, tag_name: str) -> None:
    """按 tag 删 release（仓库只留最新一版的收尾）。不存在=已删；其它失败抛
    MirrorError，由调用方决定 warn 还是 fail（配额是软约束，调用方只 warn）。"""
    tok = {"access_token": token}
    async with _client() as c:
        base = f"{GITEE_API}/repos/{repo}"
        g = await c.get(f"{base}/releases/tags/{tag_name}", params=tok)
        if g.status_code == 404:
            return
        if g.status_code in (401, 403):
            raise MirrorError(f"删旧 release 前查 {tag_name} 就被拒"
                              f"（HTTP {g.status_code}）")
        gj = _js(g)          # 同样防 200 + null
        if g.status_code != 200 or not gj.get("id"):
            return   # 查不回来：当它已经不在了，别猜
        d = await c.delete(f"{base}/releases/{gj['id']}", params=tok)
        if d.status_code not in (200, 202, 204):
            raise MirrorError(f"删 Gitee release {tag_name} 失败 HTTP "
                              f"{d.status_code}: {d.text[:200]}")
