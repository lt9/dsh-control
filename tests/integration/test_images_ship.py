"""镜像分发（契约 §6.1.5）：publish 工单全链、设置 API、/dl 公开下载、
第 20 步缺镜像三岔口（download 拉制品 / ssh 流式直传 / 点名报错）。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
from pathlib import Path

from dsh_control import images
from dsh_control.db import repo
from dsh_control.integrations.base import ExecResult
from dsh_control.jobs import publish as publish_mod
from tests.conftest import OP_KEY, XRW, create_instance, wait_job

IMG = "dsh-test:v9"     # = make_settings 的 dsh_image（第 20 步查的就是它）


def install_fake_save(monkeypatch, holder: dict) -> None:
    """docker save 的流替身（照 test_images 的 FakeProc 套路）：stdout 出载荷。

    只截 `docker save` 的命令——Popen 是全局模块属性，mirror 的分卷 split 也走
    它，别的 argv 一律放行真 Popen，不然替身会把邻居误伤成假进程。
    """
    real_popen = images.subprocess.Popen

    class FakeProc:
        def __init__(self, argv, **kw):
            self.stdout = io.BytesIO(holder["payload"])
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

    def popen(argv, **kw):
        last = argv[-1] if isinstance(argv[-1], str) else ""
        if "docker save" in last:
            assert "gzip -n" in last, last
            return FakeProc(argv, **kw)
        return real_popen(argv, **kw)

    monkeypatch.setattr(images.subprocess, "Popen", popen)


async def _job(env, job_id: int) -> dict:
    await wait_job(env.app, job_id)
    return (await env.client.get(f"/api/jobs/{job_id}")).json()


async def _seed_artifact(env, payload: bytes, tag: str = "dsh-x:v1") -> tuple[str, str]:
    """手工投放一个制品（行 + 盘上文件），返回 (token, filename)。"""
    fn = publish_mod.artifact_filename(tag)
    tok = publish_mod.new_download_token()
    (Path(env.settings.image_store) / fn).write_bytes(payload)
    async with env.db.session() as s:
        await repo.add_artifact(s, tag=tag, filename=fn, size_bytes=len(payload),
                                sha256=hashlib.sha256(payload).hexdigest(),
                                token=tok, origin_host="")
    return tok, fn


# ------------------------------------------------------------ publish 工单

async def test_publish_full_chain(ship_env, monkeypatch):
    payload = b"g" * ((2 << 20) + 5)
    monkeypatch.setattr(images, "SHIP_PROGRESS_EVERY", 1 << 20)
    install_fake_save(monkeypatch, {"payload": payload})
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    assert r.status_code == 200
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "succeeded", job["error"]
    assert {"image_save", "image_record"} <= {st["key"] for st in job["steps"]}

    lst = (await ship_env.client.get("/api/images")).json()
    item = next(i for i in lst["items"] if i["tag"] == IMG)
    assert item["size_bytes"] == len(payload)
    assert item["sha256"] == hashlib.sha256(payload).hexdigest()
    assert len(item["token"]) == 26, "门票口径：随机 26 字符 urlsafe"
    assert item["url"] == (f"https://console.example.test/dl/{item['token']}"
                           "/dsh-test_v9.tar.gz")
    assert item["origin_host"] == "nas-1"
    assert lst["store_free_bytes"] and lst["store_free_bytes"] > 0

    store = Path(ship_env.settings.image_store)
    assert (store / "dsh-test_v9.tar.gz").read_bytes() == payload
    assert not list(store.glob("*.tmp")), "半成品不许过夜"
    logs = (await ship_env.client.get(f"/api/jobs/{job['id']}/logs")).json()["items"]
    assert any("制品库已落" in it["line"] for it in logs), "每 ~100MB 一行进度"

    # 公开下载：没会话 cookie 也拿得动——token 即门票
    ship_env.client.cookies.clear()
    r = await ship_env.client.get(item["url"])
    assert r.status_code == 200 and r.content == payload
    assert r.headers["accept-ranges"] == "bytes"


async def test_republish_swaps_row_atomically(ship_env, monkeypatch):
    holder = {"payload": b"old-image-bytes" * 1000}
    install_fake_save(monkeypatch, holder)
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "succeeded", job["error"]
    tok1 = next(i for i in (await ship_env.client.get("/api/images")).json()["items"]
                if i["tag"] == IMG)["token"]

    holder["payload"] = b"new-image-bytes" * 1000
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "succeeded", job["error"]
    items = [i for i in (await ship_env.client.get("/api/images")).json()["items"]
             if i["tag"] == IMG]
    assert len(items) == 1, "同 tag 一行，不攒历史"
    assert items[0]["token"] != tok1, "重发换门票"
    ship_env.client.cookies.clear()
    assert (await ship_env.client.get(f"/dl/{tok1}/x")).status_code == 404, "旧链接立死"
    r = await ship_env.client.get(items[0]["url"])
    assert r.status_code == 200 and r.content == holder["payload"]
    assert not list(Path(ship_env.settings.image_store).glob("*.tmp"))


async def test_publish_failure_leaves_no_junk(ship_env, monkeypatch):
    def bad_save(conn, tag, dest, on_progress=None, bufsize=1 << 20):
        Path(dest).write_bytes(b"half-written-junk")   # 模拟落了半截才炸
        raise RuntimeError("源机 docker save 失败（exit 1）")
    monkeypatch.setattr(publish_mod, "_save_to_store_sync", bad_save)
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "failed"
    assert "docker save 失败" in job["error"]
    store = list(Path(ship_env.settings.image_store).iterdir())
    assert store == [], f"失败不许在制品库留任何文件: {store}"


async def test_publish_without_mounted_store_fails_loudly(ship_env, monkeypatch):
    """DSHC_IMAGE_STORE 没挂/不可写 = 直接报错，不静默降级（契约红线）。"""
    ship_env.settings.image_store = str(Path(ship_env.settings.image_store) / "没挂")
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "failed"
    assert "DSHC_IMAGE_STORE" in job["error"]


async def test_publish_rejects_bad_tag_and_host(ship_env):
    r = await ship_env.client.post("/api/images",
                                   json={"tag": "bad tag", "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    assert r.status_code == 422
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": 9999}, headers=XRW)
    assert r.status_code == 404
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas})
    assert r.status_code == 403, "写操作照旧要 X-Requested-With"


# ------------------------------------------------------------ 设置 API

async def test_settings_get_put_semantics(env):
    cur = (await env.client.get("/api/settings")).json()
    assert cur == {
        "image_ship": {"mode": "ssh", "base_url": "https://console.example.test",
                       "mirror_order": ["gitee", "github", "console"],
                       "auto_mirror": True},
        "mirrors": {"github": {"repo": "", "token_set": False, "state": None,
                               "url": "", "error": "", "updated_at": None},
                    "gitee": {"repo": "", "token_set": False, "state": None,
                              "url": "", "error": "", "updated_at": None}}}

    # 只认已知键：不认识的键不写库也不报错
    r = await env.client.put("/api/settings", json={"whatever": 1}, headers=XRW)
    assert r.status_code == 200 and r.json() == cur

    r = await env.client.put("/api/settings", json={"image_ship": {"mode": "ftp"}},
                             headers=XRW)
    assert r.status_code == 422, "mode 非枚举 422（§4）"

    async with env.db.session() as s:
        await repo.upsert_credential(s, kind="site", name="site",
                                     secret={"public_base": "https://site.example.test",
                                             "dns_zone": "example.test"},
                                     note="seed 镜像")
    r = await env.client.put(
        "/api/settings",
        json={"image_ship": {"mode": "download", "base_url": "https://dl.example.test/"}},
        headers=XRW)
    assert r.status_code == 200
    assert r.json()["image_ship"] == {
        "mode": "download", "base_url": "https://dl.example.test",
        "mirror_order": ["gitee", "github", "console"], "auto_mirror": True}
    async with env.db.session() as s:
        cred = await repo.get_credential_by_kind(s, "site")
        assert cred.secret["image_ship"] == {"mode": "download",
                                             "base_url": "https://dl.example.test"}
        assert cred.secret["dns_zone"] == "example.test", "别抹同行的兄弟键"

    # base_url 空串=回落站点 public_base；局部更新没给的键不动
    r = await env.client.put("/api/settings", json={"image_ship": {"base_url": ""}},
                             headers=XRW)
    got = r.json()["image_ship"]
    assert got["mode"] == "download" and got["base_url"] == "https://site.example.test"
    r = await env.client.put("/api/settings", json={"image_ship": {"mode": "ssh"}})
    assert r.status_code == 403, "PUT 是写操作"


async def test_setting_takes_effect_without_restart(ship_env, monkeypatch):
    """活读口径：settings 改完，下一个 deploy 立刻按新模式走（不重启）。"""
    await _seed_artifact(ship_env, b"goods", tag=IMG)
    r = await ship_env.client.put("/api/settings",
                                  json={"image_ship": {"mode": "download"}}, headers=XRW)
    assert r.status_code == 200

    ex = ship_env.providers.executor
    real = ex.run
    loaded = {"done": False}

    async def run(*, command, **kw):
        if "--max-time 8" in command:      # 探测命令：一律答可达（选路逻辑单测专测）
            ex.commands.append(command)
            return ExecResult(0, "OK\n", "")
        if "docker image inspect" in command and "HostConfig" not in command:
            return (ExecResult(0, "[image] PRESENT\n", "") if loaded["done"]
                    else ExecResult(0, "[image] MISSING\n", ""))
        if "docker load" in command:
            loaded["done"] = True
        return await real(command=command, **kw)
    monkeypatch.setattr(ex, "run", run)
    body = (await create_instance(ship_env.client,
                                  extra={"host_id": ship_env.host_nas})).json()
    job = await _job(ship_env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]


# ------------------------------------------------------------ /dl 公开下载

async def test_dl_public_ranges_and_errors(ship_env):
    payload = bytes(range(256)) * 16     # 4096 字节
    tok, fn = await _seed_artifact(ship_env, payload)
    ship_env.client.cookies.clear()      # 免鉴权：没有会话 cookie 也得能下

    r = await ship_env.client.get(f"/dl/{tok}/{fn}")
    assert r.status_code == 200 and r.content == payload
    assert "attachment" in r.headers["content-disposition"]
    # filename 只做展示名：乱传照发（认 token 不认它）
    r = await ship_env.client.get(f"/dl/{tok}/随便叫什么.tar.gz")
    assert r.status_code == 200 and r.content == payload
    # 路径穿越：编码斜杠解码后凑不出合法单段路径 → 404；就算匹配上，
    # 落盘路径也只认表里的 filename，caller 传的这段永远进不了拼路径。
    r = await ship_env.client.get(f"/dl/{tok}/..%2F..%2Fsecrets")
    assert r.status_code == 404
    r = await ship_env.client.get(f"/dl/{tok}/..%2F..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 404
    # 不认识的 token：404
    assert (await ship_env.client.get(f"/dl/{'t' * 26}/x")).status_code == 404

    r = await ship_env.client.get(f"/dl/{tok}/{fn}", headers={"Range": "bytes=4090-"})
    assert r.status_code == 206 and r.content == payload[4090:]
    assert r.headers["content-range"] == f"bytes 4090-4095/{len(payload)}"
    r = await ship_env.client.get(f"/dl/{tok}/{fn}", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == payload[:10]
    r = await ship_env.client.get(f"/dl/{tok}/{fn}", headers={"Range": "bytes=-4"})
    assert r.status_code == 206 and r.content == payload[-4:]
    r = await ship_env.client.get(f"/dl/{tok}/{fn}",
                                  headers={"Range": f"bytes={len(payload)}-"})
    assert r.status_code == 416

    # 行在文件没了（有人手删了盘）：404，不吐半吊子 200
    (Path(ship_env.settings.image_store) / fn).unlink()
    assert (await ship_env.client.get(f"/dl/{tok}/{fn}")).status_code == 404


async def test_delete_image_revokes_link(ship_env):
    tok, fn = await _seed_artifact(ship_env, b"old-goods" * 100)
    lst = (await ship_env.client.get("/api/images")).json()
    aid = lst["items"][0]["id"]
    ship_env.client.cookies.clear()
    assert (await ship_env.client.get(f"/dl/{tok}/{fn}")).status_code == 200
    # 下架是写操作：没 cookie 401，光有 XRW 也 401
    assert (await ship_env.client.request("DELETE", f"/api/images/{aid}",
                                          headers=XRW)).status_code == 401
    r = await ship_env.client.post("/api/login", json={"key": OP_KEY})
    assert r.status_code == 200
    assert (await ship_env.client.request("DELETE", "/api/images/9999",
                                          headers=XRW)).status_code == 404
    r = await ship_env.client.request("DELETE", f"/api/images/{aid}", headers=XRW)
    assert r.status_code == 200 and r.json()["ok"] is True
    ship_env.client.cookies.clear()
    assert (await ship_env.client.get(f"/dl/{tok}/{fn}")).status_code == 404, "链接立死"
    assert not (Path(ship_env.settings.image_store) / fn).exists(), "文件随下架删掉"


# ------------------------------------------------------------ 第 20 步三岔口

async def test_step20_download_pulls_artifact_with_sha_check(ship_env, monkeypatch):
    payload = b"tarstream"
    tok, _ = await _seed_artifact(ship_env, payload, tag=IMG)
    r = await ship_env.client.put("/api/settings",
                                  json={"image_ship": {"mode": "download"}}, headers=XRW)
    assert r.status_code == 200

    ex = ship_env.providers.executor
    real = ex.run
    loaded = {"done": False}

    async def run(*, command, **kw):
        if "--max-time 8" in command:      # 探测命令：一律答可达（选路逻辑单测专测）
            ex.commands.append(command)
            return ExecResult(0, "OK\n", "")
        if "docker image inspect" in command and "HostConfig" not in command:
            return (ExecResult(0, "[image] PRESENT\n", "") if loaded["done"]
                    else ExecResult(0, "[image] MISSING\ndsh-old:v0 1GB\n", ""))
        if "docker load" in command:
            loaded["done"] = True
        return await real(command=command, **kw)
    monkeypatch.setattr(ex, "run", run)

    body = (await create_instance(ship_env.client,
                                  extra={"host_id": ship_env.host_nas})).json()
    job = await _job(ship_env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    pull = next(c for c in ex.commands if "curl -fL" in c)
    assert f"/dl/{tok}/dsh-test_v9.tar.gz" in pull
    assert hashlib.sha256(payload).hexdigest() in pull, "sha 必须写死在下载命令里"
    assert "sha256sum -c" in pull and "docker load -i" in pull and "rm -f" in pull
    assert "ship-cache" in pull and "/hdd1/dsh-data/ship-cache" in pull, \
        "缓存放数据盘，不赌 /tmp 是不是 tmpfs"
    evs = (await ship_env.client.get(
        f"/api/instances/{body['instance_id']}/events")).json()["items"]
    assert any(e["kind"] == "image_shipped" and e["data"].get("via") == "console"
               for e in evs)
    probes = [c for c in ex.commands if "--max-time 8" in c]
    assert probes and f"/dl/{tok}/" in probes[0], "探测必须在目标机上跑（红线）"
    # 拉完的镜像过了 load 才算就位：整个 deploy 一路走完
    d = (await ship_env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert d["state"] == "running", d["last_error"]


async def test_step20_download_without_artifact_names_settings_page(env, monkeypatch):
    r = await env.client.put("/api/settings",
                             json={"image_ship": {"mode": "download"}}, headers=XRW)
    assert r.status_code == 200
    env.providers.executor.handlers.insert(
        0, ("docker image inspect", ExecResult(0, "[image] MISSING\ndsh-old:v0 1GB\n", "")))
    body = (await create_instance(env.client)).json()
    job = await _job(env, body["job_id"])
    assert job["state"] == "failed"
    assert "制品" in job["error"] and "设置页" in job["error"]
    assert "dsh-old:v0" in job["error"], "附目标机现有镜像"
    assert not any(ln.startswith(("dns.create", "frp.ensure", "ng.ensure"))
                   for ln in env.journal), "第 20 步拦下，外部一个对象都不许建"


async def test_step20_ssh_mode_streams_from_sibling(env, monkeypatch):
    ex = env.providers.executor
    real = ex.run
    shipped = {"done": False}

    async def run(*, host, command, **kw):
        if "docker image inspect" in command and "HostConfig" not in command:
            if host == "192.0.2.20":       # host-2 有货：源机
                return ExecResult(0, "[image] PRESENT\n", "")
            if shipped["done"]:
                return ExecResult(0, "[image] PRESENT\n", "")
            return ExecResult(0, "[image] MISSING\ndsh-old:v0 1GB\n", "")
        return await real(host=host, command=command, **kw)
    monkeypatch.setattr(ex, "run", run)

    calls: list[tuple[str, str, str]] = []

    async def fake_ship(db, providers, *, tag, src_ref, dst_ref):
        calls.append((tag, src_ref, dst_ref))
        shipped["done"] = True
        return {"bytes": 123, "seconds": 0.2, "loaded": "",
                "src": src_ref, "dst": dst_ref, "tag": tag}
    monkeypatch.setattr(images, "ship_image", fake_ship)

    body = (await create_instance(env.client,
                                  extra={"host_id": env.host_nas})).json()
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    assert calls == [(IMG, "host-2", "nas-1")], "默认 ssh：找有货的兄弟机流式直传"
    evs = (await env.client.get(
        f"/api/instances/{body['instance_id']}/events")).json()["items"]
    assert any(e["kind"] == "image_shipped" and e["data"].get("via") == "ssh"
               for e in evs)
    # 全程没碰 /dl：ssh 模式不问制品库
    assert not any("curl -fL" in c for c in ex.commands)


async def test_step20_ssh_mode_no_source_anywhere_fails_with_pointers(env, monkeypatch):
    env.providers.executor.handlers.insert(
        0, ("docker image inspect", ExecResult(0, "[image] MISSING\ndsh-old:v0 1GB\n", "")))
    body = (await create_instance(env.client)).json()
    job = await _job(env, body["job_id"])
    assert job["state"] == "failed"
    err = job["error"] or ""
    assert "cli images ship" in err and "设置页" in err, "点名著手路径"
    assert "dsh-old:v0" in err


async def test_host_image_base_url_wins_over_ship_base(ship_env, monkeypatch):
    """按主机登记的下载基址压过设置页的：入口机拨自家公网域是 hairpin 黑洞
    （§6.1.4 同款），它必须从够得着的门拉——登记值优先，站点值靠边。"""
    payload = b"tarstream"
    tok, _ = await _seed_artifact(ship_env, payload, tag=IMG)
    r = await ship_env.client.put("/api/settings", json={"image_ship": {
        "mode": "download", "base_url": "https://console.example.test/"}}, headers=XRW)
    assert r.status_code == 200
    async with ship_env.db.session() as s:
        h = await repo.get_host(s, ship_env.host_nas)
        h.image_base_url = "http://198.51.100.77:4893/"   # 尾斜杠也得给我吃掉
        await s.commit()

    ex = ship_env.providers.executor
    real = ex.run
    loaded = {"done": False}

    async def run(*, command, **kw):
        if "--max-time 8" in command:      # 探测命令：一律答可达（选路逻辑单测专测）
            ex.commands.append(command)
            return ExecResult(0, "OK\n", "")
        if "docker image inspect" in command and "HostConfig" not in command:
            return (ExecResult(0, "[image] PRESENT\n", "") if loaded["done"]
                    else ExecResult(0, "[image] MISSING\n", ""))
        if "docker load" in command:
            loaded["done"] = True
        return await real(command=command, **kw)
    monkeypatch.setattr(ex, "run", run)

    body = (await create_instance(ship_env.client,
                                  extra={"host_id": ship_env.host_nas})).json()
    job = await _job(ship_env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    pull = next(c for c in ex.commands if "curl -fL" in c)
    assert f"http://198.51.100.77:4893/dl/{tok}/" in pull, "主机登记值必须顶掉站点值"
    assert "console.example.test" not in pull


# ------------------------------------------------ 渠道推送（§6.1.6：30/40 步）

import httpx

from dsh_control.jobs import mirror


def _fake_api(monkeypatch, handler, calls: list) -> None:
    """把 mirror._client 换成挂了 MockTransport 的 AsyncClient：全进 calls。"""

    def factory():
        def wrapped(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url)))
            return handler(request)
        return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    monkeypatch.setattr(mirror, "_client", factory)


def _multipart_files(body: bytes) -> dict[str, bytes]:
    """multipart 体拆成 {filename: 内容}——断言传上去的到底是什么。"""
    head, _, rest = body.partition(b"\r\n")   # 首行就是 --boundary
    out: dict[str, bytes] = {}
    for part in rest.split(head):
        m = re.search(rb'filename="([^"]+)"', part)
        if m:
            out[m.group(1).decode()] = part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")
    return out


async def _seed_channel(env, tag: str, channel: str, *, state: str = "ok",
                        url: str = "https://placeholder/", release_tag: str = ""):
    async with env.db.session() as s:
        art = await repo.get_artifact_by_tag(s, tag)
        assert art is not None
        return await repo.upsert_channel(s, artifact_id=art.id, channel=channel,
                                         state=state, url=url,
                                         release_tag=release_tag)


async def _set_mirror_cfg(env, *, github_repo="someone/dsh-images",
                          gitee_repo="someone/dsh-images") -> None:
    r = await env.client.put("/api/settings", json={"mirrors": {
        "github": {"repo": github_repo}, "gitee": {"repo": gitee_repo}}},
        headers=XRW)
    assert r.status_code == 200


async def test_settings_mirror_validation_and_token_never_echoes(env):
    for bad in ([], ["ftp"], ["gitee", "gitee"]):
        r = await env.client.put("/api/settings",
                                 json={"image_ship": {"mirror_order": bad}}, headers=XRW)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_mirror_order"
    r = await env.client.put("/api/settings",
                             json={"image_ship": {"mirror_order": ["github", "console"]}},
                             headers=XRW)
    assert r.status_code == 200
    assert r.json()["image_ship"]["mirror_order"] == ["github", "console"]

    r = await env.client.put("/api/settings",
                             json={"mirrors": {"gitee": {"repo": "不是这个形"}}},
                             headers=XRW)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_repo"
    r = await env.client.put("/api/settings", json={"mirrors": {
        "github": {"repo": "someone/dsh-images"},
        "gitee": {"repo": "someone/dsh-images"},
        "weibo": {"repo": "who/ever"}}}, headers=XRW)
    assert r.status_code == 200
    got = r.json()["mirrors"]
    assert got["github"]["repo"] == "someone/dsh-images" and "weibo" not in got

    r = await env.client.put("/api/mirror-tokens/ftp", json={"token": "x"}, headers=XRW)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_channel"
    r = await env.client.put("/api/mirror-tokens/github", json={"token": "  "}, headers=XRW)
    assert r.status_code == 422, "空白 token 不收"
    assert (await env.client.put("/api/mirror-tokens/gitee",
                                 json={"token": "abc"})).status_code == 403, "写操作吃 XRW"
    r = await env.client.put("/api/mirror-tokens/github",
                             json={"token": "ghp_super_secret"}, headers=XRW)
    assert r.status_code == 200 and r.json() == {"ok": True, "token_set": True}
    body = (await env.client.get("/api/settings")).json()
    assert "ghp_super_secret" not in str(body), "token 永不出网（§6.1.6）"
    got_gh = body["mirrors"]["github"]
    assert got_gh["repo"] == "someone/dsh-images" and got_gh["token_set"] is True
    assert got_gh["state"] is None and got_gh["url"] == "" and got_gh["error"] == ""
    assert got_gh["updated_at"] is None
    assert body["mirrors"]["gitee"]["token_set"] is False


async def test_publish_github_asset_via_fake_api(ship_env, monkeypatch):
    payload = b"g" * 4096
    install_fake_save(monkeypatch, {"payload": payload})
    await _set_mirror_cfg(ship_env)
    r = await ship_env.client.put("/api/mirror-tokens/github",
                                  json={"token": "ghp_x"}, headers=XRW)
    assert r.status_code == 200
    upload_url = ("https://uploads.github.com/repos/someone/dsh-images"
                  "/releases/9/assets{?name,label}")

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/repos/someone/dsh-images/releases" and request.method == "POST":
            return httpx.Response(422, json={"message": "tag_name already exists"})
        if p == "/repos/someone/dsh-images/releases/tags/image-dsh-test_v9":
            return httpx.Response(200, json={"id": 9, "upload_url": upload_url})
        if p == "/repos/someone/dsh-images/releases/9" and request.method == "PATCH":
            return httpx.Response(200, json={"id": 9, "upload_url": upload_url})
        if p == "/repos/someone/dsh-images/releases/9/assets" and request.method == "GET":
            return httpx.Response(200, json=[{"id": 3, "name": "dsh-test_v9.tar.gz"}])
        if p == "/repos/someone/dsh-images/releases/assets/3" and request.method == "DELETE":
            return httpx.Response(204)
        if p == "/repos/someone/dsh-images/releases/9/assets" and request.method == "POST":
            assert request.headers["content-type"] == "application/octet-stream"
            assert request.headers.get("content-length") == str(len(payload)), \
                "uploads.github.com 不吃 chunked：必须带精确 Content-Length（E2E 400）"
            assert "transfer-encoding" not in request.headers, "不许再走 chunked"
            assert request.content == payload, "asset 必须就是制品本体"
            return httpx.Response(201, json={"id": 10, "name": "dsh-test_v9.tar.gz"})
        raise AssertionError(f"假 API 不该收到 {request.method} {p}")

    calls: list = []
    _fake_api(monkeypatch, handler, calls)
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "succeeded", job["error"]
    steps = {st["key"]: st for st in job["steps"]}
    assert steps["mirror_github"]["state"] == "succeeded"
    assert "https://github.com/someone/dsh-images/releases/download/" \
           "image-dsh-test_v9/dsh-test_v9.tar.gz" in steps["mirror_github"]["detail"]
    assert steps["mirror_gitee"]["state"] == "skipped", "没配 gitee token：跳过不算失败"
    async with ship_env.db.session() as s:
        art = await repo.get_artifact_by_tag(s, IMG)
        row = await repo.get_channel(s, art.id, "github")
        assert row.state == "ok" and row.release_tag == "image-dsh-test_v9"
        assert row.url.endswith("/releases/download/image-dsh-test_v9/dsh-test_v9.tar.gz")
    got = (await ship_env.client.get("/api/settings")).json()["mirrors"]
    assert got["github"]["state"] == "ok"
    assert got["github"]["url"] == (
        "https://github.com/someone/dsh-images/releases/download/"
        "image-dsh-test_v9/dsh-test_v9.tar.gz"), "前端 ok=直链复制吃 url 列"
    assert got["github"]["error"] == "" and got["github"]["updated_at"]
    assert got["gitee"]["state"] is None and got["gitee"]["url"] == ""
    assert not any("gitee.com" in u for _, u in calls), "gitee 未配置就不许碰它"


async def test_publish_github_bad_token_fails_loudly(ship_env, monkeypatch):
    install_fake_save(monkeypatch, {"payload": b"g" * 64})
    await _set_mirror_cfg(ship_env)
    await ship_env.client.put("/api/mirror-tokens/github", json={"token": "nope"},
                              headers=XRW)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    _fake_api(monkeypatch, handler, [])
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "failed"
    assert "token 不能走 API？" in (job["error"] or "")
    assert "mirror_gitee" not in {st["key"] for st in job["steps"]}, "红工单就地停"
    async with ship_env.db.session() as s:
        art = await repo.get_artifact_by_tag(s, IMG)
        row = await repo.get_channel(s, art.id, "github")
        assert row.state == "failed" and "401" in row.error
    got_gh = (await ship_env.client.get("/api/settings")).json()["mirrors"]["github"]
    assert got_gh["state"] == "failed" and "401" in got_gh["error"], "前端失败气泡吃 error 列"
    # 制品本体没受伤：console 链接照旧好使
    lst = (await ship_env.client.get("/api/images")).json()
    assert any(i["tag"] == IMG for i in lst["items"])


async def test_publish_gitee_splits_manifest_and_prunes_old(ship_env, monkeypatch):
    payload = b"p" * 1_500_000          # split -b 1m → aa+ab 两卷，快
    install_fake_save(monkeypatch, {"payload": payload})
    await _set_mirror_cfg(ship_env)
    await ship_env.client.put("/api/mirror-tokens/gitee", json={"token": "gt_x"},
                              headers=XRW)
    monkeypatch.setattr(mirror, "GITEE_PART_SIZE", "1m")
    # 上一版：别的 tag 还占着 gitee 仓库（1G 配额只留最新一版）
    await _seed_artifact(ship_env, b"old", tag="dsh-old:v0")
    await _seed_channel(ship_env, "dsh-old:v0", "gitee",
                        url="https://gitee.example/attach/old.manifest.json",
                        release_tag="image-dsh-old_v0")
    uploaded: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        base = "/api/v5/repos/someone/dsh-images"
        if p == f"{base}/releases/tags/image-dsh-test_v9":
            return httpx.Response(404, json={})
        if p == f"{base}/releases" and request.method == "POST":
            assert "access_token=gt_x" in str(request.url) or \
                   b"access_token" in request.content
            return httpx.Response(201, json={"id": 5})
        if p == f"{base}/releases/5/attach_files" and request.method == "POST":
            files = _multipart_files(request.content)
            assert list(files), "multipart 里必须有 file 字段"
            uploaded.update(files)
            name = next(iter(files))
            return httpx.Response(201, json={
                "id": 1, "name": name,
                "browser_download_url": f"https://gitee.example/attach/{name}"})
        if p == f"{base}/releases/tags/image-dsh-old_v0":
            return httpx.Response(200, json={"id": 77})
        if p == f"{base}/releases/77" and request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"假 API 不该收到 {request.method} {p}")

    calls: list = []
    _fake_api(monkeypatch, handler, calls)
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "succeeded", job["error"]
    assert any(m == "DELETE" and u.split("?")[0].endswith("/releases/77")
               for m, u in calls), "新版落地后必须删旧 release（1G 配额）"
    parts = sorted(k for k in uploaded if k.endswith((".part-aa", ".part-ab")))
    assert parts == ["dsh-test_v9.tar.gz.part-aa", "dsh-test_v9.tar.gz.part-ab"]
    assert uploaded[parts[0]] == payload[:1048576] and uploaded[parts[1]] == payload[1048576:]
    import json as _json
    mani = _json.loads(uploaded["dsh-test_v9.tar.gz.manifest.json"].decode())
    sha = hashlib.sha256(payload).hexdigest()
    assert mani["file"] == "dsh-test_v9.tar.gz" and mani["sha256"] == sha
    assert mani["size"] == len(payload)
    assert [pp["name"] for pp in mani["parts"]] == parts
    assert all(hashlib.sha256(uploaded[n]).hexdigest() == pp["sha256"]
               for n, pp in zip(parts, mani["parts"], strict=True)), \
        "每卷的 sha 必须是真的——拼回全靠它点名"
    assert mani["parts"][0]["url"] == "https://gitee.example/attach/" + parts[0]
    # manifest 单行、无空格：目标机 grep -o 抽直链的口径依赖这一点
    raw = uploaded["dsh-test_v9.tar.gz.manifest.json"]
    assert raw.count(b"\n") == 0 and b'"url":"' in raw and b'"url": "' not in raw
    async with ship_env.db.session() as s:
        art = await repo.get_artifact_by_tag(s, IMG)
        row = await repo.get_channel(s, art.id, "gitee")
        assert row.state == "ok"
        assert row.url == "https://gitee.example/attach/dsh-test_v9.tar.gz.manifest.json"
        old = await repo.get_artifact_by_tag(s, "dsh-old:v0")
        oldrow = await repo.get_channel(s, old.id, "gitee")
        assert oldrow.state == "failed" and "替换" in oldrow.error


async def test_publish_mirror_skipped_when_off(ship_env, monkeypatch):
    install_fake_save(monkeypatch, {"payload": b"g" * 64})
    await _set_mirror_cfg(ship_env)
    await ship_env.client.put("/api/mirror-tokens/gitee", json={"token": "gt"},
                              headers=XRW)
    await ship_env.client.put("/api/mirror-tokens/github", json={"token": "gh"},
                              headers=XRW)
    r = await ship_env.client.put("/api/settings",
                                  json={"image_ship": {"auto_mirror": False}}, headers=XRW)
    assert r.status_code == 200

    def no_client():
        raise AssertionError("auto_mirror=false 一个 HTTP 都不许发")
    monkeypatch.setattr(mirror, "_client", no_client)
    r = await ship_env.client.post("/api/images",
                                   json={"tag": IMG, "from_host_id": ship_env.host_nas},
                                   headers=XRW)
    job = await _job(ship_env, r.json()["job_id"])
    assert job["state"] == "succeeded", job["error"]
    steps = {st["key"]: st for st in job["steps"]}
    assert steps["mirror_github"]["state"] == "skipped"
    assert steps["mirror_gitee"]["state"] == "skipped"
    async with ship_env.db.session() as s:
        art = await repo.get_artifact_by_tag(s, IMG)
        assert await repo.get_channel(s, art.id, "github") is None


# ------------------------------------------- 第 20 步探测选路与 gitee 拼回（§6.1.6）

def _probe_runner(ex, real, loaded: dict, *, fail_url: str = ""):
    """目标机侧的探测/取货替身：--max-time 8 的是探测；fail_url 命中的答不可达。"""

    async def run(*, command, **kw):
        if "--max-time 8" in command:
            ex.commands.append(command)
            if fail_url and fail_url in command:
                return ExecResult(22, "", "curl: (22) The requested URL returned error: 503")
            return ExecResult(0, "OK\n", "")
        if "docker image inspect" in command and "HostConfig" not in command:
            return (ExecResult(0, "[image] PRESENT\n", "") if loaded["done"]
                    else ExecResult(0, "[image] MISSING\n", ""))
        if "docker load" in command:
            loaded["done"] = True
        return await real(command=command, **kw)
    return run


async def _prep_download_mode(env, tok_tag: str) -> None:
    r = await env.client.put("/api/settings",
                             json={"image_ship": {"mode": "download"}}, headers=XRW)
    assert r.status_code == 200


async def test_step20_skips_unreachable_channel_in_order(ship_env, monkeypatch):
    await _seed_artifact(ship_env, b"tarstream", tag=IMG)
    await _prep_download_mode(ship_env, IMG)
    await _seed_channel(ship_env, IMG, "gitee",
                        url="https://gitee.example/dsh-test_v9.tar.gz.manifest.json")
    await _seed_channel(ship_env, IMG, "github",
                        url="https://gt.example/download/image-dsh-test_v9/dsh-test_v9.tar.gz")

    ex = ship_env.providers.executor
    loaded = {"done": False}
    monkeypatch.setattr(ex, "run", _probe_runner(ex, ex.run, loaded,
                                                 fail_url="gitee.example"))
    body = (await create_instance(ship_env.client,
                                  extra={"host_id": ship_env.host_nas})).json()
    job = await _job(ship_env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    probes = [c for c in ex.commands if "--max-time 8" in c]
    assert "gitee.example" in probes[0] and "gt.example" in probes[1], \
        "探测顺序 = mirror_order（默认 gitee 先），不通就滑下一条"
    assert len(probes) == 2, "github 赢了就不许再探 console"
    pull = next(c for c in ex.commands if "curl -fL" in c)
    assert "gt.example" in pull and "sha256sum -c" in pull and "docker load -i" in pull
    evs = (await ship_env.client.get(
        f"/api/instances/{body['instance_id']}/events")).json()["items"]
    assert any(e["kind"] == "image_shipped" and e["data"].get("via") == "github"
               for e in evs), "事件里点名选了哪条渠道"


async def test_step20_gitee_reassembles_with_full_sha_check(ship_env, monkeypatch):
    await _seed_artifact(ship_env, b"tarstream", tag=IMG)
    await _prep_download_mode(ship_env, IMG)
    await _seed_channel(ship_env, IMG, "gitee",
                        url="https://gitee.example/dsh-test_v9.tar.gz.manifest.json")

    ex = ship_env.providers.executor
    loaded = {"done": False}
    monkeypatch.setattr(ex, "run", _probe_runner(ex, ex.run, loaded))
    body = (await create_instance(ship_env.client,
                                  extra={"host_id": ship_env.host_nas})).json()
    job = await _job(ship_env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    pull = next(c for c in ex.commands if "curl -fL" in c)
    sha = hashlib.sha256(b"tarstream").hexdigest()
    assert "https://gitee.example/dsh-test_v9.tar.gz.manifest.json" in pull
    assert """grep -o '"url":"http""" in pull, "POSIX 抽直链：不赌目标机有 jq/python3"
    assert ">> " in pull, "分卷必须 >> 拼进同一文件"
    assert f"echo '{sha}  " in pull and "sha256sum -c" in pull, "整包 sha 强校验必做"
    assert "docker load -i" in pull and "rm -rf" in pull
    assert "/hdd1/dsh-data/ship-cache" in pull
    probes = [c for c in ex.commands if "--max-time 8" in c]
    assert len(probes) == 1 and "gitee.example" in probes[0]


async def test_step20_all_channels_unreachable_names_each(ship_env, monkeypatch):
    await _seed_artifact(ship_env, b"tarstream", tag=IMG)
    await _prep_download_mode(ship_env, IMG)
    await _seed_channel(ship_env, IMG, "gitee", url="https://gitee.example/m.json")
    await _seed_channel(ship_env, IMG, "github", url="https://gt.example/a.tar.gz")

    ex = ship_env.providers.executor
    loaded = {"done": False}
    monkeypatch.setattr(ex, "run", _probe_runner(ex, ex.run, loaded, fail_url="https://"))
    body = (await create_instance(ship_env.client,
                                  extra={"host_id": ship_env.host_nas})).json()
    job = await _job(ship_env, body["job_id"])
    assert job["state"] == "failed"
    err = job["error"] or ""
    for want in ("gitee: 不可达", "github: 不可达", "console: 不可达"):
        assert want in err, f"全败必须逐渠道点名：{err}"
    probes = [c for c in ex.commands if "--max-time 8" in c]
    assert len(probes) == 3
    assert not any("docker load" in c for c in ex.commands), "一条都没通就不许 load"


async def test_github_upload_over_real_h11_has_exact_content_length(tmp_path, monkeypatch):
    """MockTransport 之外的一条：真 h11 框架——裸 socket 服务器上逐字节验
    「头里 Content-Length==size、没有 chunked、实发字节数分毫不差」。
    uploads.github.com 就是被 chunked 打过 400 的，这条测试是它的替身。"""
    payload = b"z" * 300_000
    f = tmp_path / "dsh-test_v9.tar.gz"
    f.write_bytes(payload)
    seen: dict[str, object] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:                                    # 一条连接串多请求（keep-alive）
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await reader.read(4096)
                if not chunk:
                    writer.close()
                    return
                head += chunk
            head_txt, _, rest = head.partition(b"\r\n\r\n")
            lines = head_txt.decode("latin1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            hdrs = {}
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                hdrs[k.strip().lower()] = v.strip()
            body = rest
            need = int(hdrs.get("content-length", 0))
            while len(body) < need:
                body += await reader.read(min(65536, need - len(body)))
            if target.startswith("/upload") and method == "POST":
                seen["cl"] = hdrs.get("content-length")
                seen["te"] = hdrs.get("transfer-encoding")
                seen["sent"] = len(body)
                seen["body_ok"] = body == payload
                reply = b'{"id": 10}'
                code = "201 Created"
            elif target.startswith("/repos/") and method == "POST":
                reply = b'{"message": "tag_name already exists"}'   # 走复用分支
                code = "422 Unprocessable Entity"
            elif "/releases/tags/" in target:
                reply = ('{"id": 9, "upload_url": "'
                         + base_url + '/upload{?name,label}"}').encode()
                code = "200 OK"
            elif method == "PATCH":
                reply = b'{"id": 9}'
                code = "200 OK"
            elif "/assets" in target and method == "GET":
                reply = b"[]"
                code = "200 OK"
            else:
                reply = b'{"message": "unexpected"}'
                code = "500 Server Error"
            resp = (f"HTTP/1.1 {code}\r\nContent-Type: application/json"
                    f"\r\nContent-Length: {len(reply)}\r\n\r\n").encode() + reply
            writer.write(resp)
            await writer.drain()

    async def starter():
        nonlocal base_url
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        return server

    base_url = ""
    server = await starter()
    monkeypatch.setattr(mirror, "GITHUB_API", base_url)
    async with server:
        url, rel_tag = await mirror.github_push(
            "tok", "someone/dsh-images", "dsh-test:v9", "dsh-test_v9.tar.gz",
            f, hashlib.sha256(payload).hexdigest(), len(payload))
    assert rel_tag == "image-dsh-test_v9"
    assert url == ("https://github.com/someone/dsh-images/releases/download/"
                   "image-dsh-test_v9/dsh-test_v9.tar.gz")
    assert seen.get("cl") == str(len(payload)), f"真线上头里必须有 CL：{seen}"
    assert seen.get("te") is None, "真线上不许有 Transfer-Encoding: chunked"
    assert seen.get("sent") == len(payload) and seen.get("body_ok"), "实发字节分毫不差"
