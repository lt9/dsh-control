"""0012 在真 PostgreSQL 上的升/降/再升：CHECK 换约束是这条迁移的全部动作，
sqlite 测试库（create_all 重建）永远测不到它，只能在真 PG 上验一次。

没有 docker 或没有 postgres:17-alpine 镜像的环境自动跳过——make check 在
纯开发机上依然全绿。测试用一次性容器与一次性端口，绝不碰站点库。
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import uuid

import pytest
from alembic import command
from alembic.config import Config

IMAGE = "postgres:17-alpine"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    p = subprocess.run(["docker", "image", "inspect", IMAGE],
                       capture_output=True, check=False)
    return p.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(),
                                reason="需要 docker 与 postgres:17-alpine 镜像")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alembic_cfg() -> Config:
    from dsh_control.cli import _alembic_config
    return _alembic_config()


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["docker", *args], capture_output=True, text=True,
                       check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} 失败：{p.stderr}")
    return p


async def _constraint_clause(url: str, name: str) -> str:
    """查 information_schema 里那条 CHECK 的实际文本（升没升上去，看库不听代码）。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(url)
    try:
        async with eng.connect() as c:
            row = (await c.execute(
                text("SELECT check_clause FROM information_schema"
                     ".check_constraints WHERE constraint_name = :n"),
                {"n": name})).scalar()
            return row or ""
    finally:
        await eng.dispose()


async def test_0012_升得上去降得下来CHECK真的换了我们(monkeypatch):
    port = _free_port()
    name = f"dshc-mig0012-{uuid.uuid4().hex[:8]}"
    pw = uuid.uuid4().hex
    url = f"postgresql+asyncpg://postgres:{pw}@127.0.0.1:{port}/dsh_control"
    monkeypatch.setenv("DSHC_DATABASE_URL", url)
    try:
        _docker("run", "-d", "--name", name, "-e", f"POSTGRES_PASSWORD={pw}",
                "-e", "POSTGRES_DB=dsh_control", "-p", f"127.0.0.1:{port}:5432",
                "--rm", IMAGE)
        # initdb 期间有个临时实例会让 pg_isready 假绿，紧接着还要重启一次：
        # 就绪判据必须是真连上一次，不是探针绿。
        for _ in range(60):
            try:
                await _constraint_clause(url, "no_such_constraint_yet")
                break
            except Exception:  # noqa: BLE001  任何连接失败都继续等，直到超时
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("postgres 容器 30 秒没就绪")

        cfg = _alembic_cfg()
        await asyncio.to_thread(command.upgrade, cfg, "0012")

        creds = await _constraint_clause(url, "credentials_kind_check")
        assert "'memory_user_key'" in creds and "'memory_admin_key'" in creds
        assert "'ssh_key'" in creds and "'mirror_token'" in creds   # 老权值一个没掉
        jobs = await _constraint_clause(url, "deploy_jobs_kind_check")
        assert "'memory'" in jobs and "'deploy'" in jobs

        await asyncio.to_thread(command.downgrade, cfg, "0011")
        creds = await _constraint_clause(url, "credentials_kind_check")
        assert "memory_user_key" not in creds
        jobs = await _constraint_clause(url, "deploy_jobs_kind_check")
        assert "'memory'" not in jobs

        await asyncio.to_thread(command.upgrade, cfg, "0012")
        creds = await _constraint_clause(url, "credentials_kind_check")
        assert "'memory_admin_key'" in creds
    finally:
        _docker("rm", "-f", name, check=False)
