"""CLI 冒烟：`capability publish / ls` 走真子进程 + 临时 sqlite。

只验「命令在、参数在、错误出口在」这三件事——执行语义在引擎测试里逐条抠过。
子进程跑还有个便宜：settings 从零读，不踩本测试进程里 env  fixture 的缓存。
migrate 顺带把 0001→0013 整条链在临时库上走一遍（PG 独有的 kind 换绑自己跳过）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _cli(db: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "DSHC_DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    return subprocess.run(
        [sys.executable, "-m", "dsh_control.cli", *args],
        capture_output=True, text=True, timeout=180, env=env,
        cwd=str(cwd or ROOT), check=False)


def test_发布与列表冒烟(tmp_path: Path) -> None:
    db = tmp_path / "cap.db"
    r = _cli(db, "migrate")
    assert r.returncode == 0, r.stderr[-500:]

    r = _cli(db, "capability", "ls")
    assert r.returncode == 0, r.stderr[-500:]
    assert "能力目录为空" in r.stdout

    r = _cli(db, "capability", "publish", "--dir", "kit/capabilities/speckit",
             "--kit-version", "0.0.0-smoke", "--note", "冒烟")
    assert r.returncode == 0, r.stderr[-500:]
    assert "speckit@1.0.0" in r.stdout and "入目录" in r.stdout

    r = _cli(db, "capability", "ls")
    assert r.returncode == 0 and "speckit" in r.stdout
    assert "skill" in r.stdout and "0.0.0-smoke" in r.stdout


def test_坏包发布当场拒(tmp_path: Path) -> None:
    db = tmp_path / "cap.db"
    assert _cli(db, "migrate").returncode == 0
    bad = tmp_path / "kitrepo" / "capabilities" / "bad"
    bad.mkdir(parents=True)
    (bad / "manifest.toml").write_text(
        'id = "bad"\nversion = "1.0.0"\nkind = "skill"\ntitle = "坏"\n'
        'author = "多出来的字段"\n'
        'compatible_dsh = ""\nstatus = "true"\nhealthcheck = "true"\n'
        'uninstall = "reverse"\nfiles = []\n', encoding="utf-8")
    r = _cli(db, "capability", "publish", "--dir", str(tmp_path / "kitrepo"))
    assert r.returncode != 0
    assert "未知字段" in (r.stderr + r.stdout)
    # 拒了就不许有半件入库
    assert "bad" not in _cli(db, "capability", "ls").stdout


def test_没到货的目录发布报错退出(tmp_path: Path) -> None:
    db = tmp_path / "cap.db"
    assert _cli(db, "migrate").returncode == 0
    r = _cli(db, "capability", "publish", "--dir", str(tmp_path / "nowhere"))
    assert r.returncode != 0
    assert "没找到能力包" in (r.stderr + r.stdout)


def test_实例不存在时status当场拒(tmp_path: Path) -> None:
    db = tmp_path / "cap.db"
    assert _cli(db, "migrate").returncode == 0
    r = _cli(db, "capability", "status", "no-such-slug")
    assert r.returncode != 0 and "不存在" in (r.stderr + r.stdout)


def test_动作子命令在位(tmp_path: Path) -> None:
    """help 里五个动作 + dry-run/wait 旗子都在（真正的执行路在 API/引擎测试里验）。"""
    db = tmp_path / "cap.db"
    r = _cli(db, "capability", "--help")
    assert r.returncode == 0
    for word in ("install", "apply", "enable", "disable", "uninstall",
                 "publish", "status", "ls"):
        assert word in r.stdout
