"""P3 迁移信号第 3 条：双跑对账（ADR-0001 迁移路径节）。

同一台「假实例」上——用真实 /bin/sh + PATH 替身（dsh/docker 两个 shim）模拟
容器——先按 **git HEAD 里的 heredoc 脚本**铺一遍，再让 **capability_engine
按 manifest** 铺一遍，然后逐条比对两者的结论：

- home 层补丁：追加段**字节级相同**（marker 逐字、官方正文来源解析、成员数覆盖）；
- 技能目录：文件集合、config.env 的键值、config.env 权限一致；
- 幂等重跑：都短路、文件不二次变化；
- 缺料（镜像里没有官方组合包 / 缺 token）：两边都 skipped 不挡、现场无痕；
- 交叉探针：引擎探针打在 heredoc 铺出的旧现场上，结论（installed/
  marker_present/health）必须与旧旁证的结论一致——「旧 heredoc 铺设一次、
  引擎探针采一次，结论一致」的机器可执行版。

旧脚本不作废不重写：测试从 `git show HEAD:src/dsh_control/jobs/capabilities.py`
取原文执行（stdout 断言即钉桩）；技能正文指到收编后的 kit 包（本文件第一条
测试断言其与 HEAD 字节一致）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dsh_control.integrations import capability_engine as engine
from dsh_control.integrations.base import ExecResult
from dsh_control.jobs.capabilities import TEAM_MARKER

REPO = Path(__file__).resolve().parents[2]
BUNDLE_REL = "node_modules/@deepseek-ai/dsh-experimental-agent-team-profile/cordis.patch.yml"
FAKE_BUNDLE_BODY = ("- id: agent-team\n"
                    "  name: '@deepseek-ai/dsh-experimental-agent-team'\n"
                    "  config:\n"
                    "    maxMembers: 32\n")
MATERIAL = {"url": "http://comfy.test:8188", "backup_url": "http://backup.test:8188",
            "token": "tok-secret-123"}

_DOCKER_SHIM = """#!/bin/sh
# docker exec [-i] <container> sh -c "<cmd>" → 在本机把 <cmd> 当容器内命令跑
while [ "$1" = exec ] || [ "$1" = -i ] || [ "$1" = -t ]; do shift; done
shift; shift; shift
exec /bin/sh -c "$1"
"""


class FakeContainer:
    """一棵临时目录当容器文件系统；ssh() 把 docker exec/sh -c 都落到真 /bin/sh。"""

    def __init__(self, root: Path, *, with_bundle: bool = True) -> None:
        self.root = root
        self.home = root / "data"                       # 容器里就是 ${DSH_HOME}
        self.home.mkdir(parents=True)
        bindir = root / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(_DOCKER_SHIM)
        (bindir / "docker").chmod(0o755)
        lib = root / "fakepkg" / "lib"
        lib.mkdir(parents=True)
        (lib / "bin.js").write_text("#!/bin/sh\necho dsh\n")
        (lib / "bin.js").chmod(0o755)          # 不可执行的 shim 会被 command -v 跳过
        (bindir / "dsh").symlink_to(lib / "bin.js")
        if with_bundle:
            bundle = root / "fakepkg" / BUNDLE_REL
            bundle.parent.mkdir(parents=True)
            bundle.write_text(FAKE_BUNDLE_BODY)
        self.env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                    "DSH_HOME": str(self.home)}
        self.commands: list[str] = []

    async def ssh(self, command: str, *, timeout: float = 300,
                  stdin: str | None = None, quiet: bool = True) -> ExecResult:
        self.commands.append(command)
        argv = shlex.split(command)
        if argv[0] == "docker":
            script = argv[argv.index("-c") + 1]
        elif argv[0] == "sh" and argv[1] == "-c":
            script = argv[2]
        else:
            script = command
        proc = await asyncio.to_thread(
            subprocess.run, ["/bin/sh", "-c", script], input=stdin or "",
            capture_output=True, text=True, env=self.env,
            cwd=self.root, timeout=timeout, check=False)
        return ExecResult(proc.returncode, proc.stdout, proc.stderr)

    def read(self, rel: str) -> bytes:
        return (self.home / rel).read_bytes()

    def marker_count(self) -> int:
        f = self.home / "cordis.patch.yml"
        return f.read_text().count(TEAM_MARKER) if f.is_file() else 0

    def copy_kit(self, dest: Path) -> Path:
        """把 kit 包拷到「被管机的 kit 目录」形状（第 20 步换装的产物）。"""
        pack = dest / "capabilities" / "comfy-image"
        pack.mkdir(parents=True)
        for f in (REPO / "kit/capabilities/comfy-image").iterdir():
            (pack / f.name).write_bytes(f.read_bytes())
        return dest


@pytest.fixture(scope="module")
def old_caps():
    """git HEAD 里的 heredoc 时代模块——对账基线，不作废不重写。"""
    try:
        # 对账基线钉死在收编前的版本（P3 收编提交 5e22172 的父 2da807e）。
        # 用 HEAD 会随提交漂移失效——2026-10-29 v0.2.1 当天就被 HEAD 翻篇坑过一次。
        src = subprocess.run(
            ["git", "show",
             "2da807e:src/dsh_control/jobs/capabilities.py"],
            capture_output=True, text=True, check=True, cwd=REPO).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("需要 git 取 HEAD 里的 heredoc 作对账基线")
    assert "_team_patch_script" in src and "_push_skill_script" in src
    spec = importlib.util.spec_from_loader("dsh_control.jobs._caps_head_baseline",
                                           loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(REPO / "src" / "dsh_control" / "jobs" / "capabilities.py")
    sys.modules[spec.name] = mod
    try:
        exec(compile(src, "<git HEAD:jobs/capabilities.py>", "exec"),  # noqa: S102
             mod.__dict__)  # 跑我们自己 git HEAD 里的基线源码，不是外部输入
    finally:
        sys.modules.pop(spec.name, None)
    assert mod.TEAM_MARKER == TEAM_MARKER          # 信号第 4 条：marker 逐字
    # 正文已收编进 kit 包（test_kit_资产与git_HEAD正文一致 断言字节相同），
    # 旧脚本按原样读它
    mod._SKILL_DIR = REPO / "kit" / "capabilities" / "comfy-image"
    return mod


@pytest.fixture(scope="module")
def manifests() -> dict:
    return {p.capability_id: p.manifest for p in engine.packs_from_dir(str(REPO / "kit"))}


def _git_show(rev_path: str) -> str:
    return subprocess.run(["git", "show", rev_path], capture_output=True,
                          text=True, check=True, cwd=REPO).stdout


def test_kit_资产与git_HEAD正文一致():
    """收编不是重写：kit 包里的技能正文必须与 heredoc 时代内嵌文件逐字节相同。"""
    for name in ("SKILL.md", "comfy_generate.py"):
        head = _git_show(f"2da807e:src/dsh_control/skills/comfy-image/{name}")
        assert (REPO / "kit" / "capabilities" / "comfy-image" / name).read_bytes() \
            == head.encode("utf-8")


# ---------------------------------------------------------------- patch 双跑（agent-team）

def patch_conclusion(c: FakeContainer) -> dict:
    """对账用的「结论」：marker 在/不在、home 层在不在——旧旁证同款判据。"""
    f = c.home / "cordis.patch.yml"
    if not f.is_file():
        return {"home_layer": False, "marker": False, "max_members": None,
                "bundle_body": False}
    text = f.read_text()
    m = re.search(r"maxMembers: (\d+)", text)
    return {"home_layer": True, "marker": TEAM_MARKER in text,
            "max_members": int(m.group(1)) if m else None,
            "bundle_body": FAKE_BUNDLE_BODY.strip() in text}


async def test_patch_双跑_结论一致且追加段字节相同(tmp_path, old_caps, manifests):
    man = manifests["agent-team"]
    a, b = FakeContainer(tmp_path / "old"), FakeContainer(tmp_path / "new")
    v = await a.ssh("docker exec ctr sh -c 'command -v dsh'")
    assert str(a.root) in v.stdout             # 假容器里的 dsh 必须是我们架的，不是宿主机真身

    # 旧：heredoc 一遍（stdout 断言 = 基准钉桩）
    r_old = await a.ssh("docker exec ctr sh -c "
                        + shlex.quote(old_caps._team_patch_script(str(a.home), 3)))
    assert r_old.ok and "已写入 home 层" in r_old.stdout
    assert "成员上限 3" in r_old.stdout
    # 新：引擎按 manifest 一遍
    res = await engine.run_action(b.ssh, "ctr", man, str(b.home), "apply",
                                  engine.resolve_params(man, {"max_members": 3}, {}),
                                  version="1.0.0")
    assert res.state == "succeeded", res.reason

    assert patch_conclusion(a) == patch_conclusion(b)                # 结论一致
    assert patch_conclusion(a)["bundle_body"]                        # 铺的是假组合包正文，不是宿主机真身
    assert a.read("cordis.patch.yml") == b.read("cordis.patch.yml")  # 更强：字节相同

    # 交叉探针：引擎探针打在旧 heredoc 铺出的现场上（同一实例双跑口径）
    obs_old = await engine.run_probe(a.ssh, "ctr", man, home=str(a.home))
    assert obs_old["installed"] and obs_old["marker_present"] and obs_old["health"] == "ok"

    # 幂等二跑：旧短路、新短路、现场字节不动、marker 不翻倍
    r2 = await a.ssh("docker exec ctr sh -c "
                     + shlex.quote(old_caps._team_patch_script(str(a.home), 3)))
    assert r2.ok and "已铺过，跳过" in r2.stdout
    before = b.read("cordis.patch.yml")
    res2 = await engine.run_action(b.ssh, "ctr", man, str(b.home), "apply",
                                   engine.resolve_params(man, {"max_members": 3}, {}),
                                   version="1.0.0")
    assert res2.state == "succeeded" and "幂等" in res2.detail
    assert b.read("cordis.patch.yml") == before
    assert a.marker_count() == b.marker_count() == 1


async def test_patch_缺官方组合包_两边都skipped现场无痕(tmp_path, old_caps, manifests):
    man = manifests["agent-team"]
    a = FakeContainer(tmp_path / "old", with_bundle=False)
    b = FakeContainer(tmp_path / "new", with_bundle=False)

    r_old = await a.ssh("docker exec ctr sh -c "
                        + shlex.quote(old_caps._team_patch_script(str(a.home), 3)))
    assert r_old.ok and "找不到官方组合包" in r_old.stdout      # 旧 skipped 语义钉桩
    res = await engine.run_action(b.ssh, "ctr", man, str(b.home), "apply",
                                  engine.resolve_params(man, {"max_members": 3}, {}),
                                  version="1.0.0")
    assert res.state == "skipped" and "on_missing=skipped" in res.reason
    assert patch_conclusion(a) == patch_conclusion(b)            # 两边都没有 home 层
    assert not (a.home / "cordis.patch.yml").exists()
    assert not (b.home / "cordis.patch.yml").exists()


# ---------------------------------------------------------------- skill 双跑（comfy-image）

def env_map(data: bytes) -> dict[str, str]:
    """config.env → 键值表；空值键删掉（旧脚本空 backup 不写行、引擎写空行，
    comfy_generate._bases 两边都按真值过滤——语义等价，归一后必须等值）。"""
    out = {}
    for line in data.decode("utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if v:
            out[k] = v
    return out


async def test_skill_双跑_文件集合_config与权限一致(tmp_path, old_caps, manifests):
    man = manifests["comfy-image"]
    a, b = FakeContainer(tmp_path / "old"), FakeContainer(tmp_path / "new")
    stdin = f"{MATERIAL['url']}\n{MATERIAL['backup_url']}\n{MATERIAL['token']}\n"

    r_old = await a.ssh("docker exec -i ctr sh -c "
                        + shlex.quote(old_caps._push_skill_script(str(a.home))),
                        stdin=stdin, quiet=True)
    assert r_old.ok
    assert sorted(r_old.stdout.split()) == ["SKILL.md", "comfy_generate.py",
                                            "config.env"]        # 旧回显清单钉桩

    kit = b.copy_kit(tmp_path / "kit")
    resolved = engine.resolve_params(man, {}, dict(MATERIAL))
    res = await engine.run_action(b.ssh, "ctr", man, str(b.home), "apply", resolved,
                                  kit_dir=str(kit), version="1.0.0")
    assert res.state == "succeeded", res.reason

    d, e = a.home / "skills/comfy-image", b.home / "skills/comfy-image"
    names = sorted(p.name for p in d.iterdir())
    assert names == sorted(p.name for p in e.iterdir()) \
        == ["SKILL.md", "comfy_generate.py", "config.env"]
    for name in ("SKILL.md", "comfy_generate.py"):
        assert (d / name).read_bytes() == (e / name).read_bytes()
    assert env_map((d / "config.env").read_bytes()) \
        == env_map((e / "config.env").read_bytes()) \
        == {"COMFY_URL": MATERIAL["url"], "COMFY_BACKUP_URL": MATERIAL["backup_url"],
            "COMFY_TOKEN": MATERIAL["token"], "COMFY_TIMEOUT": "180"}
    for p in (d, e):
        assert stat.S_IMODE((p / "config.env").stat().st_mode) == 0o600

    # 交叉探针：引擎探针打在旧现场与新技术场上，观测结论逐键一致
    obs_a = await engine.run_probe(a.ssh, "ctr", man, home=str(a.home))
    obs_b = await engine.run_probe(b.ssh, "ctr", man, home=str(b.home))
    keys = ("installed", "marker_present", "enabled", "health")
    assert {k: obs_a[k] for k in keys} == {k: obs_b[k] for k in keys}
    assert obs_a["installed"] and obs_a["health"] == "ok"


async def test_skill_缺token_两边都判不铺且守卫逐字在场(tmp_path, manifests):
    """缺图 token 的 skipped 住在编排层（apply_capabilities 的空值守卫），
    引擎的 on_missing 兜住「参数压根没给」这一支——两条都对账。"""
    man = manifests["comfy-image"]
    b = FakeContainer(tmp_path / "new")
    kit = b.copy_kit(tmp_path / "kit")
    resolved = engine.resolve_params(man, {}, {"url": MATERIAL["url"]})
    assert resolved.missing == ["token"]
    res = await engine.run_action(b.ssh, "ctr", man, str(b.home), "apply", resolved,
                                  kit_dir=str(kit), version="1.0.0")
    assert res.state == "skipped" and "on_missing=skipped" in res.reason
    assert not (b.home / "skills/comfy-image").exists()

    # 编排层守卫：旧（git HEAD）与新（现文件）逐字同判——空 url/token 不进引擎
    old_src = _git_show("HEAD:src/dsh_control/jobs/capabilities.py")
    new_src = (REPO / "src" / "dsh_control" / "jobs" / "capabilities.py").read_text()
    guard = 'if not material["url"] or not material["token"]:'
    assert old_src.count(guard) == 1 and new_src.count(guard) == 1
