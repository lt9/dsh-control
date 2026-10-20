"""镜像在位检查 / DSH_IMAGE 传参 / ship 内存流式搬运 / 站点零硬编码守卫。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from dsh_control.integrations.base import ExecResult
from tests.conftest import XRW, wait_job

IMG_TAG = "dsh-test:v9"


# ---------------------------------------------------------------- deploy 侧

async def test_missing_image_fails_before_any_external_write(env):
    """镜像没备好就必须在 20 步（无副作用阶段）拦下：DNS/隧道/站点一个不建。"""
    env.providers.executor.handlers.append((
        "docker image inspect",
        ExecResult(0, "[image] MISSING\ndsh-other:v1 10MB\n", "")))
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed"
    by_key = {st["key"]: st for st in job["steps"]}
    assert by_key["host_prepare"]["state"] == "failed"
    assert f"cli images ship --tag {IMG_TAG}" in (job["error"] or "")
    assert "dsh-other:v1" in (job["error"] or "")  # 现有可选标签一眼可见
    assert not any(ln.startswith(("dns.create", "frp.ensure", "ng.ensure"))
                   for ln in env.journal)
    # 该机现有 dsh 标签进了任务日志（点对点可见）
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs",
                                 params={"after": 0})).json()["items"]
    assert any("dsh-other:v1" in it["line"] for it in logs)


async def test_missing_dsh_image_config_fails_in_plan(env):
    env.settings.dsh_image = ""
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed"
    assert "DSHC_IMAGE" in (job["error"] or "")
    # 只允许选主机的只读 df 探测存在；任何外部写对象/kit 脚本都不许发生
    assert not any(ln.startswith(("dns.", "frp.", "ng.", "runtime.")) or "new-instance.sh" in ln
                   for ln in env.journal)


async def test_image_and_public_config_land_in_instance_env(env):
    """kit 的真实接口（现网实测纠正）：new-instance.sh 只吃位置参数
    <名字> <宿主机端口> + DATA_ROOT/LAN_IP 两个环境变量；镜像与公网两条走实例自己的
    .env，deploy.sh 只认 .env。旧断言"两个脚本都带 DSH_IMAGE 环境变量"是我们凭空
    想象的接口——脚本根本不读，真机上直接报「用法：<小写名字> <宿主机端口>」。"""
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    cmds = env.providers.executor.commands
    newc = next(c for c in cmds if "./scripts/new-instance.sh" in c)
    assert "new-instance.sh dsh-01 4893" in newc, newc        # 端口走位置参数
    assert "DATA_ROOT=/hdd1/dsh-data" in newc                  # 数据根目录交给 kit
    assert "CONTAINER_PORT=" not in newc                       # 不再发明环境变量
    assert "DOCUMENTS_DIRECTORY=" not in newc
    patch = next(c for c in cmds if "TRUSTED_HOSTS" in c and "sed -i" in c)
    assert f"DSH_IMAGE={IMG_TAG}" in patch, patch              # 镜像落到实例 .env
    assert "PUBLIC_URL=https://dsh-01.example.test" in patch    # 一键登录地址的来源
    assert "dsh-01.example.test" in patch                       # 白名单追加公网域名
    dep = next(c for c in cmds if "./scripts/deploy.sh" in c)
    assert "deploy.sh dsh-01" in dep and "DSH_IMAGE" not in dep
    # plan 步骤日志要说明用哪个准备好的版本
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs",
                                 params={"after": 0})).json()["items"]
    assert any(IMG_TAG in it["line"] for it in logs)
    assert job["state"] == "succeeded"


# ---------------------------------------------------------------- images 侧

async def test_image_ls_filters_dsh_tags(env):
    from dsh_control.images import image_ls

    env.providers.executor.handlers.append((
        "docker images --format",
        ExecResult(0, f"{IMG_TAG}\t1.1GB\ndsh-other:v2\t2GB\nnginx\t180MB\n", "")))
    rows = await image_ls(env.db, env.providers, "nas-1")
    assert [r["tag"] for r in rows] == [IMG_TAG, "dsh-other:v2"]
    assert rows[0]["size"] == "1.1GB"


def test_ship_stream_bytes_and_cleans_keys(monkeypatch, tmp_path):
    from dsh_control import images

    payload = b"x" * ((1 << 20) + 5)  # 跨 bufsize 边界

    class FakeStdin:
        def __init__(self):
            self.buf = bytearray()

        def write(self, b):
            self.buf.extend(b)

        def close(self):
            pass

    class FakeProc:
        def __init__(self, argv, **kw):
            self.argv = argv
            self.returncode = None
            if "docker save" in argv[-1]:
                self.stdout = io.BytesIO(payload)
            else:
                self.stdout = io.BytesIO(b"Loaded image: dsh-test:v9\n")
            self.stdin = FakeStdin()

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    monkeypatch.setattr(images.subprocess, "Popen", FakeProc)
    made: list[str] = []

    def spy_tmp_key(pem: str) -> str:
        path = tmp_path / f"key-{len(made)}"
        path.write_text(pem)
        made.append(str(path))
        return str(path)

    monkeypatch.setattr(images, "_tmp_key", spy_tmp_key)
    out = images._ship_sync(
        {"host": "192.0.2.10", "port": 22, "user": "ops", "private_key_pem": "PEM-A"},
        {"host": "192.0.2.20", "port": 22, "user": "ops", "private_key_pem": "PEM-B"},
        IMG_TAG)
    assert out["bytes"] == len(payload)          # 内存管道一字不差
    assert "Loaded image" in out["loaded"]
    for k in made:
        assert not Path(k).exists()              # 临时私钥用后即删


def test_ship_rejects_bad_tag():
    """标签校验在 ship_image 入口：带空格直接拒绝，跑不到 subprocess。"""
    import asyncio

    from dsh_control.images import ship_image

    async def go():
        with pytest.raises(RuntimeError, match="不合法"):
            await ship_image(None, None, tag="bad tag", src_ref="a", dst_ref="b")

    asyncio.run(go())


def test_no_build_or_hardcoded_tag_anywhere_in_src():
    """守卫（契约 §6.1.2）：开通阶段永不构建镜像——src/ 里不许出现 docker build
    或具体镜像标签字面量（`dsh-family:` 这种前缀都算），标签只能来自配置。"""
    root = Path(__file__).resolve().parents[2] / "src"
    offenders: list[str] = []
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for needle in ("docker build", "dsh-family:"):
            if needle in text:
                offenders.append(f"{f}: {needle}")
    assert not offenders, offenders
