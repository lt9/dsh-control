"""镜像分发（契约 §6.1.5）：publish 工单全链、设置 API、/dl 公开下载、
第 20 步缺镜像三岔口（download 拉制品 / ssh 流式直传 / 点名报错）。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from dsh_control import images
from dsh_control.db import repo
from dsh_control.integrations.base import ExecResult
from dsh_control.jobs import publish as publish_mod
from tests.conftest import OP_KEY, XRW, create_instance, wait_job

IMG = "dsh-test:v9"     # = make_settings 的 dsh_image（第 20 步查的就是它）


def install_fake_save(monkeypatch, holder: dict) -> None:
    """docker save 的流替身（照 test_images 的 FakeProc 套路）：stdout 出载荷。"""

    class FakeProc:
        def __init__(self, argv, **kw):
            assert "docker save" in argv[-1] and "gzip -n" in argv[-1], argv[-1]
            self.stdout = io.BytesIO(holder["payload"])
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(images.subprocess, "Popen", FakeProc)


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
    assert cur == {"image_ship": {"mode": "ssh", "base_url": "https://console.example.test"}}

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
    assert r.json() == {"image_ship": {"mode": "download",
                                       "base_url": "https://dl.example.test"}}
    async with env.db.session() as s:
        cred = await repo.get_credential_by_kind(s, "site")
        assert cred.secret["image_ship"] == {"mode": "download",
                                             "base_url": "https://dl.example.test"}
        assert cred.secret["dns_zone"] == "example.test", "别抹同行的兄弟键"

    # base_url 空串=回落站点 public_base；局部更新没给的键不动
    r = await env.client.put("/api/settings", json={"image_ship": {"base_url": ""}},
                             headers=XRW)
    assert r.json() == {"image_ship": {"mode": "download",
                                       "base_url": "https://site.example.test"}}
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
    assert any(e["kind"] == "image_shipped" and e["data"].get("via") == "download"
               for e in evs)
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
