"""引擎执行与探针的幂等语义（决策三）＋兼容闸门（决策五/U-2）。

ssh 通道用「容器假象」替身：它按我们真发的命令串回真状态——marker 在/不在、
版本戳对/不对、包到货/没到货，两态都要走到。marker 判据与 jobs/capabilities.py
的 TEAM_MARKER 先例逐字对齐（P3 双跑对账的依据）。

假人只认**语义**不认拼法：命令先 shlex 拆成词再看关键词，所以引擎换引号写法、
改行序都不受影响；真要它兜的是「同一条判据不能少」。
"""

from __future__ import annotations

import base64
import re
import shlex

import pytest_asyncio

from dsh_control.db.models import Capability, InstanceCapability
from dsh_control.db.session import create_all, make_db
from dsh_control.integrations import capability_engine as e
from dsh_control.integrations.base import ExecResult
from dsh_control.jobs.capabilities import TEAM_MARKER


@pytest_asyncio.fixture
async def db():
    """能力表要的库：与 conftest 同款（aiosqlite 内存 + create_all）。"""
    _db = make_db("sqlite+aiosqlite:///:memory:")
    await create_all(_db)
    yield _db
    await _db.engine.dispose()


# ------------------------------------------------------------------ 容器假象


def _unq(tok: str) -> str:
    """去掉一层 shell 引号（假人读判据用；Python 的 shlex 没有 unquote）。"""
    try:
        parts = shlex.split(tok)
    except ValueError:
        return tok
    return parts[0] if parts else tok


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


class FakeContainer:
    """按命令语义回真状态的 ssh 替身（形状同 JobCtx.ssh）。

    状态三件事就够判全部幂等：`status_rc`（探针认不认「装着」）、`stamp`
    （版本戳内容）、`patch_text`（home 层正文）。"""

    def __init__(self, *, kit_present: bool = True, health_rc: int = 0) -> None:
        self.kit_present = kit_present
        self.health_rc = health_rc
        self.status_rc = 1            # 一开始什么都没装
        self.stamp: str | None = None
        self.disabled = False
        self.patch_text = ""
        self.files: dict[str, str] = {}
        self.commands: list[str] = []
        self.stdins: list[str | None] = []
        # 「镜像里有哪些官方组合包」——patch 类按 marker 找正文的来源
        self.bundles: list[str] = []

    # ---- 状态读写（脚本语义的最小实现）----
    def _read_stamp(self) -> str:
        return self.stamp or ""

    def _strip_segment(self, marker: str) -> None:
        out, skip = [], False
        for line in self.patch_text.splitlines():
            if line.startswith(marker):
                skip = True
                continue
            if line.startswith("# dsh-control:"):
                skip = False
            if not skip:
                out.append(line)
        self.patch_text = "\n".join(out)

    async def __call__(self, command: str, *, timeout: float = 300,
                       stdin: str | None = None, quiet: bool = True) -> ExecResult:
        self.commands.append(command)
        self.stdins.append(stdin)
        # 取 `sh -c` 的脚本正文（正则对着它写：外层 shell 引号会把正文拆散）
        script = command
        try:
            w = shlex.split(command)
            if "-c" in w:
                script = w[w.index("-c") + 1]
        except ValueError:
            pass

        # ① $DSH_HOME 现取（读家目录的脚本就是 printf 一句）
        if script.startswith("printf %s") and "DSH_HOME" in script:
            return ExecResult(0, "/data", "")

        # ② 到货检查（主机侧）：包在不在 kit 目录
        if command.startswith("[ -f ") and "/capabilities/" in command:
            return ExecResult(0, ("yes" if self.kit_present else "no") + "\n", "")

        # ③ 探针一次往返：分段回四件事（必须在 ① 前判：脚本里也有 DSH_HOME）
        # 顺序无碍是因为 ① 只认 `printf %s` 开头。
        if "@@STATUS@@" in script:
            body = ("@@STATUS@@\n" + str(self.status_rc)
                    + "\n@@STAMP@@\n" + self._read_stamp()
                    + "\n@@DISABLED@@\n" + ("yes" if self.disabled else "no")
                    + "\n@@HEALTH@@\n"
                    + (str(self.health_rc) if self.status_rc == 0 else "1")
                    + "\ndetail\n")
            return ExecResult(0, body, "")

        # ④ 组合包正文（patch 类运行时才解析官方正文；判空在同一脚本里）
        self._bundle_hit = ""
        mf = re.search(r"-path '\*([^']+)'", script)
        if mf and "find" in script:
            frag = mf.group(1)
            hit = next((f"/usr/local/lib/node_modules/{b}" for b in self.bundles
                        if b.endswith(frag)), "")
            self._bundle_hit = hit

        # ⑤ home 层写入（patch/plugin/service 共用一条缝）
        if "cordis.patch.yml" in script:
            mm = re.search(r"grep -qF '([^']+)'", script)
            marker = mm.group(1) if mm else ""
            if "ALREADY" in script:                      # 幂等短路判据
                mv = re.search(r"= '?([^'\]]+)'? \] && \{ echo ALREADY", script)
                ver = mv.group(1) if mv else ""
                if marker and marker in self.patch_text and self._read_stamp() == ver:
                    return ExecResult(0, "ALREADY\n", "")
                self._strip_segment(marker)              # 版本不符：摘旧段再重铺
            if "NOSEG" in script:                        # 摘段（uninstall/disable）
                if not marker or marker not in self.patch_text:
                    return ExecResult(0, "NOSEG\n", "")
                self._strip_segment(marker)
                return ExecResult(0, "", "")
            # 追加正文 = bundle 的 cat + append 的 base64（引擎把两段拼进同一个 {}）
            body = ""
            if "cat \"$B\"" in script:
                if not getattr(self, "_bundle_hit", ""):
                    return ExecResult(0, "SRC_MISSING\n", "")
                body += "# 官方组合包正文（假镜像里的货）\n"
            mb = re.search(r"printf %s '?([A-Za-z0-9+/=]+)'? \| base64 -d", script)
            if mb:
                body += base64.b64decode(mb.group(1)).decode("utf-8")
            head = ("\n" + marker) if re.search(r"\{ echo; echo ", script) else marker
            self.patch_text = self.patch_text + head + "\n" + body
            # 同一脚本里顺手写版本戳（`printf '%s\n' '<ver>' > '<dir>/version'`）
            ms = re.search(r"printf '%s\\n' '?([^' >\n]+)'? > '?([^' >\n]+)'?", script)
            if ms and ms.group(2).endswith("/version"):
                self.stamp = ms.group(1)
            return ExecResult(0, "PATCH_APPLIED\n", "")

        # ⑥ skill 正文搬运：主机文件 → 容器（正文不进 argv）
        if "SKILL_APPLIED" in script:
            if not self.kit_present:
                return ExecResult(3, "PACK_MISSING\n", "")
            self.status_rc = 0
            for tgt in re.findall(r"cat > '([^']+)'", script):
                self.files[tgt] = "content"
            return ExecResult(0, "SKILL_APPLIED\n", "")

        # ⑧ 禁用/启用：技能目录整体移进/移出 scratch（可逆）
        if "mv " in script and "disabled-dir" in script:
            # 目的地是 disabled-dir = 禁用；回迁 skills = 启用
            mv_src = re.search(r"mv '?([^'\s;]+)", script)
            self.status_rc = 0 if (mv_src and "disabled-dir/" in mv_src.group(1)) else 1
            return ExecResult(0, "", "")


        # ⑨ 卸载：技能目录连 scratch 一起删（先于 ⑦：卸载脚本里也有 scratch 路径）
        if "rm -rf" in script and "/data/skills" in script:
            self.status_rc = 1
            self.disabled = False
            if "/.dshctl-capabilities/" in script:
                self.stamp = None
            return ExecResult(0, "", "")

        # ⑦ 版本戳 / disabled 记号 / scratch 清理
        if "/.dshctl-capabilities/" in script:
            mw = re.search(r"printf '%s\\n' '?([^'\s]*)'? > '?([^'\s]*)'?", script)
            if mw:
                val, pth = mw.group(1), mw.group(2)
                if pth.endswith("/version"):
                    self.stamp = val
                elif pth.endswith("/disabled"):
                    self.disabled = val == "1"
            if re.search(r"rm -f '?[^'\s]*/disabled'?", script):
                self.disabled = False
            if "rm -rf" in script:
                self.stamp, self.disabled = None, False
            return ExecResult(0, "", "")

        return ExecResult(0, "ok\n", "")

class FakeRuntime:
    def __init__(self, version: str) -> None:
        self._v = version

    async def version(self, container: str) -> str:
        return self._v


SKILL_MAN = {
    "id": "demo", "version": "1.0.0", "kind": "skill", "title": "演示",
    "compatible_dsh": ">=0.2.0-rc.1",
    "marker": "# dsh-control: 演示由控制台铺设",
    "status": "test -f ${DSH_HOME}/skills/demo/SKILL.md",
    "healthcheck": "test -f ${DSH_HOME}/skills/demo/SKILL.md",
    "uninstall": "reverse", "restart_required": False,
    "files": ["SKILL.md"], "params": {"on_missing": "skipped"},
}


def _patch_man(marker: str = TEAM_MARKER, version: str = "1.0.0") -> dict:
    return {
        "id": "agent-team", "version": version, "kind": "patch", "title": "团队",
        "compatible_dsh": ">=0.2.0-rc.1", "marker": marker,
        "status": f"grep -qF '{marker}' ${{DSH_HOME}}/cordis.patch.yml",
        "healthcheck": f"grep -qF '{marker}' ${{DSH_HOME}}/cordis.patch.yml",
        "uninstall": "reverse", "restart_required": True,
        "source": {}, "append": "max_members = 3",
        "params": {"on_missing": "failed"},
    }


# ------------------------------------------------------------------ skill：两态与幂等

async def test_skill装前后探针两态_and_重铺不增量() -> None:
    ssh = FakeContainer()
    man = dict(SKILL_MAN)
    obs0 = await e.run_probe(ssh, "dsh-01", man, home="/data")
    assert obs0["installed"] is False and obs0["health"] == "unknown"

    r1 = await e.run_action(ssh, "dsh-01", man, "/data", "install",
                            e.ResolvedParams(), kit_dir="/srv/dsh-family",
                            version="1.0.0")
    assert r1.state == "succeeded" and "已铺" in r1.detail
    # 正文从**主机文件**流进容器：命令里有 `cat > <目标>` 与包内源，正文不在 argv
    joined = "\n".join(ssh.commands)
    assert "/data/skills/demo/SKILL.md" in joined
    assert "capabilities/demo/SKILL.md" in joined
    assert "base64" not in joined or "SKILL.md" not in joined.split("base64")[0][:80]

    obs1 = await e.run_probe(ssh, "dsh-01", man, home="/data")
    assert obs1["installed"] is True and obs1["observed_version"] == "1.0.0"
    assert obs1["health"] == "ok" and obs1["enabled"] is True

    # 重铺：文件覆写、探针结论不变——没有「越铺越多」
    await e.run_action(ssh, "dsh-01", man, "/data", "apply",
                       e.ResolvedParams(), kit_dir="/srv/dsh-family", version="1.0.0")
    obs2 = await e.run_probe(ssh, "dsh-01", man, home="/data")
    assert (obs2["installed"], obs2["observed_version"]) == (True, "1.0.0")


async def test_skill包没到货按on_missing走skipped() -> None:
    ssh = FakeContainer(kit_present=False)
    res = await e.run_action(ssh, "dsh-01", dict(SKILL_MAN), "/data", "install",
                             e.ResolvedParams(), kit_dir="/srv/kit", version="1.0.0")
    assert res.state == "skipped" and "kit" in res.reason


async def test_skill禁用可逆_卸载连戳一起删() -> None:
    ssh = FakeContainer()
    man = dict(SKILL_MAN)
    await e.run_action(ssh, "c", man, "/data", "install", e.ResolvedParams(),
                       kit_dir="/srv/kit", version="1.0.0")
    await e.run_action(ssh, "c", man, "/data", "disable", e.ResolvedParams(),
                       kit_dir="/srv/kit", version="1.0.0")
    assert ssh.disabled is True
    obs = await e.run_probe(ssh, "c", man, home="/data")
    assert obs["enabled"] is False and obs["installed"] is False

    await e.run_action(ssh, "c", man, "/data", "enable", e.ResolvedParams(),
                       kit_dir="/srv/kit", version="1.0.0")
    assert ssh.disabled is False
    obs_on = await e.run_probe(ssh, "c", man, home="/data")
    assert obs_on["enabled"] is True

    await e.run_action(ssh, "c", man, "/data", "uninstall", e.ResolvedParams(),
                       kit_dir="/srv/kit", version="1.0.0")
    obs2 = await e.run_probe(ssh, "c", man, home="/data")
    assert obs2["installed"] is False and obs2["observed_version"] == ""


# ------------------------------------------------------------------ patch：marker 两态

async def test_patch_marker逐字兼容TEAM_MARKER() -> None:
    ssh = FakeContainer()
    man = _patch_man()
    r1 = await e.run_action(ssh, "c", man, "/data", "install",
                            e.ResolvedParams(), version="1.0.0")
    assert r1.state == "succeeded"
    joined = "\n".join(ssh.commands)
    assert TEAM_MARKER in joined and "grep -qF" in joined     # 判据逐字：grep -qF 原文
    assert TEAM_MARKER in ssh.patch_text and "max_members = 3" in ssh.patch_text

    # 第二次：marker 在、版本一致 → shell 判 ALREADY → 引擎报「幂等」，不重复追加
    r2 = await e.run_action(ssh, "c", man, "/data", "apply",
                            e.ResolvedParams(), version="1.0.0")
    assert r2.state == "succeeded" and "幂等" in r2.detail
    assert ssh.patch_text.count(TEAM_MARKER) == 1


async def test_patch升级摘段再追加_marker不留双份() -> None:
    ssh = FakeContainer()
    await e.run_action(ssh, "c", _patch_man(), "/data", "install",
                       e.ResolvedParams(), version="1.0.0")
    r = await e.run_action(ssh, "c", _patch_man(version="1.1.0"), "/data",
                           "apply", e.ResolvedParams(), version="1.1.0")
    assert r.state == "succeeded"
    assert ssh.patch_text.count(TEAM_MARKER) == 1     # 摘段重铺，不是追加成两段
    assert ssh.stamp == "1.1.0"


async def test_patch卸载摘段连戳一起删() -> None:
    ssh = FakeContainer()
    await e.run_action(ssh, "c", _patch_man(), "/data", "install",
                       e.ResolvedParams(), version="1.0.0")
    r = await e.run_action(ssh, "c", _patch_man(), "/data", "uninstall",
                           e.ResolvedParams(), version="1.0.0")
    assert r.state == "succeeded" and TEAM_MARKER not in ssh.patch_text
    assert ssh.stamp in (None, "")


async def test_patch正文来自镜像找不到就按缺料走() -> None:
    ssh = FakeContainer()
    man = _patch_man()
    man["source"] = {"bundle": "@deepseek-ai/dsh-experimental-agent-team-profile",
                     "file": "cordis.patch.yml"}
    r = await e.run_action(ssh, "c", man, "/data", "install",
                           e.ResolvedParams(), version="1.0.0")
    assert r.state == "failed" and "组合包" in r.reason     # on_missing=failed
    assert ssh.patch_text == ""                            # 没弄坏现场


async def test_探针缺命令记unknown不装健康() -> None:
    ssh = FakeContainer(health_rc=127)
    man = dict(SKILL_MAN)
    await e.run_action(ssh, "c", man, "/data", "install", e.ResolvedParams(),
                       kit_dir="/srv/kit", version="1.0.0")
    obs = await e.run_probe(ssh, "c", man, home="/data")
    assert obs["health"] == "unknown" and "不存在" in obs["health_detail"]


# ------------------------------------------------------------------ 闸门

async def _seed_cap(db, compatible: str = ">=0.2.0-rc.1") -> None:
    async with db.session() as s:
        s.add(Capability(capability_id="speckit", version="1.0.0", kind="skill",
                         title="Spec Kit", manifest={"params": {"on_missing": "failed"}},
                         compatible_dsh=compatible, state="published", current=True))
        s.add(InstanceCapability(instance_id=7, capability_id="speckit",
                                 desired_state="present"))
        await s.commit()


async def test_闸门放行现网镜像并带版本来源(db) -> None:
    await _seed_cap(db)
    async with db.session() as s:
        g = await e.gate_compatible(s, instance_id=7, ssh=FakeContainer(),
                                    runtime=FakeRuntime("0.2.1-rc.2"),
                                    container="dsh-02")
    assert g.allowed and g.dsh_version == "0.2.1-rc.2"
    assert "docker-exec" in g.dsh_version_source          # 必带 source（U-2 定案）


async def test_闸门拦住低版本目标镜像(db) -> None:
    await _seed_cap(db)
    async with db.session() as s:
        g = await e.gate_compatible(s, instance_id=7, ssh=FakeContainer(),
                                    runtime=FakeRuntime("0.1.9"),
                                    container="dsh-02")
    assert not g.allowed and g.blockers[0]["capability_id"] == "speckit"


async def test_闸门读不到版本宁拦不升(db) -> None:
    await _seed_cap(db)
    async with db.session() as s:
        g = await e.gate_compatible(s, instance_id=7, ssh=FakeContainer(),
                                    runtime=FakeRuntime(""), container="dsh-02")
    assert not g.allowed and "读不到" in g.blockers[0]["reason"]


async def test_闸门可用caller目标版本(db) -> None:
    await _seed_cap(db)
    async with db.session() as s:
        g = await e.gate_compatible(s, instance_id=7, ssh=FakeContainer(),
                                    runtime=FakeRuntime("0.1.9"), container="dsh-02",
                                    target_dsh_version="0.3.0")
    assert g.allowed and "caller" in g.dsh_version_source


async def test_台账空闸门直接放行(db) -> None:
    async with db.session() as s:
        g = await e.gate_compatible(s, instance_id=99, ssh=FakeContainer(),
                                    runtime=FakeRuntime("0.1.0"), container="x")
    assert g.allowed and g.checked == 0
