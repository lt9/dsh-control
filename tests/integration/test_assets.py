"""资产分发（kit 脚本入库/下发 + 字体库制品账目）。

「镜像+配置+字体+脚本都在控制台，开通时统一下发」的回归：kit 小文本进 DB、
字体库大文件进制品库目录，两条下发都走控制台内存 → ssh stdin → 目标机解包。
"""

from __future__ import annotations

import io
import os
import tarfile

from dsh_control import assets
from dsh_control.db import repo
from dsh_control.integrations.base import ExecResult
from tests.conftest import create_instance, wait_job

KIT_MARK = ".dshctl-kit-version"


def _kit_tree(tmp_path) -> str:
    """一棵最小 kit 仓库树：该带的（scripts/ 两个脚本 + compose.yaml +
    .env.example）和不该带的（instances/ 运行时状态、Dockerfile、垃圾文件）."""
    root = tmp_path / "kitrepo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "new-instance.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(scripts / "new-instance.sh", 0o755)
    (scripts / "deploy.sh").write_text("#!/bin/sh\nexit 0\n")
    (scripts / "junk.pyc").write_text("skip me")
    pyc = scripts / "__pycache__"
    pyc.mkdir(exist_ok=True)
    (pyc / "x.py").write_text("y")
    inst = root / "instances" / "dsh-01"
    inst.mkdir(parents=True, exist_ok=True)
    (inst / ".env").write_text("SECRET=runtime-state\n")
    (root / "compose.yaml").write_text("services: {}\n")
    (root / ".env.example").write_text("DATA_DIR=\n")
    (root / "Dockerfile").write_text("FROM x\n")
    return str(root)


async def _deploy(env) -> dict:
    body = (await create_instance(env.client)).json()
    await wait_job(env.app, body["job_id"])
    return body


async def _job(env, job_id: int) -> dict:
    return (await env.client.get(f"/api/jobs/{job_id}")).json()


async def _log_lines(env, job_id: int) -> list[str]:
    items = (await env.client.get(f"/api/jobs/{job_id}/logs",
                                  params={"after": 0})).json()["items"]
    return [it["line"] for it in items]


# ---------------------------------------------------------------- 发布

async def test_publish_kit_roundtrip(env, tmp_path):
    """发布=清单正确（带 scripts/compose/.env.example，甩掉 instances 与杂物）、
    可回读、tar 里权限位与版本标记都在。"""
    out = await assets.publish_kit(env.db, _kit_tree(tmp_path),
                                   version="kit-test-1", note="unit")
    assert out["files"] == 4, out          # 2 scripts + compose.yaml + .env.example
    async with env.db.session() as s:
        rel = await repo.current_kit_release(s)
    assert rel is not None and rel.version == "kit-test-1" and rel.current
    assert not [p for p in rel.files if p.startswith("instances/")]

    blob = assets.kit_tar(rel.files, rel.version)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        modes = {ti.name: ti.mode for ti in tf.getmembers()}
        mark = tf.extractfile(assets.KIT_VERSION_FILE).read().decode()
    assert modes["scripts/new-instance.sh"] == 0o755, "可执行位不能在路上丢"
    assert modes["compose.yaml"] == 0o644
    assert "Dockerfile" not in modes and "instances/dsh-01/.env" not in modes
    assert mark.strip() == "kit-test-1"


async def test_publish_kit_twice_moves_current(env, tmp_path):
    await assets.publish_kit(env.db, _kit_tree(tmp_path), version="kit-a")
    await assets.publish_kit(env.db, _kit_tree(tmp_path), version="kit-b")
    async with env.db.session() as s:
        rel = await repo.current_kit_release(s)
        rows = await repo.list_kit_releases(s)
    assert rel.version == "kit-b"
    assert sum(1 for r in rows if r.current) == 1, "current 必须恰好一行"


async def test_publish_kit_rejects_bogus_root(env, tmp_path):
    (tmp_path / "empty").mkdir()
    try:
        await assets.publish_kit(env.db, str(tmp_path / "empty"))
    except RuntimeError as e:
        assert "scripts/" in str(e)
    else:
        raise AssertionError("不是 kit 仓库根居然没报错")


# ---------------------------------------------------------------- 第 20 步下发

async def test_host_prepare_ships_kit_when_stale(env, tmp_path, monkeypatch):
    """机器标记是旧版 → 整包下发，带全连接参数与现行版本；日志记账。"""
    await assets.publish_kit(env.db, _kit_tree(tmp_path), version="kit-9")
    calls: list[dict] = []

    async def fake_install(conn, files, version, kit_dir):
        calls.append({"conn": dict(conn), "version": version, "kit": kit_dir})
        return {"bytes": 20480, "version": version, "files": len(files)}

    monkeypatch.setattr("dsh_control.jobs.deploy.install_kit", fake_install)
    env.providers.executor.handlers.insert(
        0, (KIT_MARK, ExecResult(0, "kit-OLD\n", "")))

    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    assert len(calls) == 1
    assert calls[0]["version"] == "kit-9"
    assert calls[0]["kit"] == "/srv/dsh-family"
    conn = calls[0]["conn"]
    assert (conn["host"], conn["port"], conn["user"]) == ("192.0.2.10", 2333, "ops")
    assert conn["private_key_pem"]
    lines = await _log_lines(env, body["job_id"])
    assert any("整包下发" in ln and "kit-9" in ln for ln in lines), lines
    assert any("就位" in ln for ln in lines), lines


async def test_host_prepare_keeps_current_kit(env, tmp_path, monkeypatch):
    """标记=现行版：一次安装都不发起，只记一行最新。"""
    await assets.publish_kit(env.db, _kit_tree(tmp_path), version="kit-9")
    calls: list = []
    monkeypatch.setattr("dsh_control.jobs.deploy.install_kit",
                        lambda *a, **k: calls.append(a))
    env.providers.executor.handlers.insert(
        0, (KIT_MARK, ExecResult(0, "kit-9\n", "")))
    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    assert not calls
    lines = await _log_lines(env, body["job_id"])
    assert any("已是最新" in ln for ln in lines), lines


async def test_host_prepare_without_kit_release_probes_nothing(env):
    """库里没发布过 kit：行为与存量完全一致——连版本标记都不探。"""
    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    assert not [c for c in env.providers.executor.commands if KIT_MARK in c]
    lines = await _log_lines(env, body["job_id"])
    assert any("沿用机器预装" in ln for ln in lines), lines


async def test_host_prepare_install_failure_blocks_provision(env, tmp_path,
                                                             monkeypatch):
    """下发失败=预检失败：宁可不开通，也不让旧脚本继续跑新版本的世界。"""
    await assets.publish_kit(env.db, _kit_tree(tmp_path), version="kit-9")

    async def broken(conn, files, version, kit_dir):
        raise RuntimeError("目标机下发脚本失败（exit 2）")

    monkeypatch.setattr("dsh_control.jobs.deploy.install_kit", broken)
    env.providers.executor.handlers.insert(
        0, (KIT_MARK, ExecResult(0, "MISSING\n", "")))
    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "failed", job
    st = next(s for s in job["steps"] if s["key"] == "host_prepare")
    assert st["state"] == "failed" and "kit 下发失败" in (st["detail"] or "")
