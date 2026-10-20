"""shell.py：SSH 执行器 + DshRuntime 解析。全部离线，不碰网络。"""

from __future__ import annotations

import asyncio
import re
import stat
from collections import deque
from pathlib import Path

import pytest

from dsh_control.integrations.base import ExecResult, IntegrationError
from dsh_control.integrations.shell import SshDshRuntime, SshExecutor

SHELL_SRC = Path(__file__).resolve().parents[2] / "src" / "dsh_control" / "integrations" / "shell.py"


class FakeExecutor:
    """记录命令、按队列回放结果。"""

    def __init__(self, results=()):
        self.calls: list[dict] = []
        self.results = deque(results)

    async def run(self, *, host, port, user, private_key_pem, command,
                  timeout=300, line_sink=None, stdin=None):
        self.calls.append({"host": host, "command": command, "user": user,
                           "port": port, "stdin": stdin})
        if self.results:
            return self.results.popleft()
        return ExecResult(code=0, stdout="", stderr="")


def runtime(fake: FakeExecutor) -> SshDshRuntime:
    return SshDshRuntime(fake, host="192.0.2.10", user="ops", port=2222,
                         private_key_pem="PEM")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ Runtime 解析

def test_inspect_parses_pipe_fields():
    fake = FakeExecutor([ExecResult(0,
        "running|healthy|dsh-family:local|sha256:5023c780deadbeef1234567890abcdef12345678|2|172.27.0.3",
        "")])
    st = run(runtime(fake).inspect("dsh-alpha"))
    assert st is not None
    assert (st.name, st.state, st.health) == ("dsh-alpha", "running", "healthy")
    assert st.image == "dsh-family:local"
    assert st.image_id == "5023c780dead"          # 去掉 sha256: 前缀并截短
    assert st.restart_count == 2
    assert st.ip == "172.27.0.3"


def test_inspect_tolerates_continuation_backslashes():
    # base.DOCKER_INSPECT_SNIPPET 的续行反斜杠可能原样混进输出行
    fake = FakeExecutor([ExecResult(0, "running|none|\\dsh-img|sha256:aaaa1111bbbb2222|0|10.0.0.9", "")])
    st = run(runtime(fake).inspect("c"))
    assert st is not None
    assert st.health == "none"
    assert st.image == "dsh-img"
    assert st.image_id == "aaaa1111bbbb"


def test_inspect_absent_returns_none():
    fake = FakeExecutor([ExecResult(0, "absent", "")])
    assert run(runtime(fake).inspect("nope")) is None


def test_inspect_command_keeps_go_template_braces_intact():
    """Go 模板的 `{{…}}` 必须原样到 docker 手里。

    曾经用 str.format 渲染这条命令，`{{` 被收成 `{`，docker 拿到残缺模板不报错，
    把原文当值打出来——页面上镜像/IP/状态就成了 `{.Config.Image}`，好容器被误判
    成 error。这里把命令文本钉住，别让"顺手改回 format"再犯一次。
    """
    fake = FakeExecutor([ExecResult(0, "absent", "")])
    run(runtime(fake).inspect("dsh-01"))
    cmd = fake.calls[0]["command"]
    assert "{{.State.Status}}" in cmd
    assert "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" in cmd
    assert "{.State.Status}" not in cmd.replace("{{.State.Status}}", "")
    assert "__NAME__" not in cmd
    assert " dsh-01 " in cmd                      # 名字落在命令里（shlex 认为安全就不加引号）
    run(runtime(fake).inspect("dsh 01;rm -rf /"))
    assert "'dsh 01;rm -rf /'" in fake.calls[1]["command"], "带空格/分号的名字必须整体引起来"


def test_inspect_refuses_unrendered_template_output():
    """模板原文一律按"看不到"处理：写进库就是把好容器判成死容器。"""
    fake = FakeExecutor([ExecResult(0, "{.State.Status}|none|{.Config.Image}"
                                       "|{.Image}|{.RestartCount}|", "")])
    assert run(runtime(fake).inspect("dsh-01")) is None


def test_inspect_garbage_returns_none():
    fake = FakeExecutor([ExecResult(0, "total garbage here", "")])
    assert run(runtime(fake).inspect("nope")) is None


def test_stop_and_restart_commands():
    fake = FakeExecutor()
    run(runtime(fake).stop("dsh-alpha"))
    run(runtime(fake).restart("it's-a trap"))
    assert fake.calls[0]["command"] == "docker stop dsh-alpha"
    assert fake.calls[1]["command"].startswith("docker restart ")
    # 引号安全：带怪名字必须整体被 quote
    assert "'it'\"'\"'s-a trap'" in fake.calls[1]["command"]


def test_module_never_emits_destructive_docker_commands():
    src = SHELL_SRC.read_text(encoding="utf-8")
    assert not re.search(r"\bdocker\s+[^\n]*\brm\b", src), "shell.py 里不许出现删容器命令"
    assert not re.search(r"\brm\b", src), "shell.py 任何位置都不该出现独立的 rm 词"
    assert "volume" not in src.lower().replace("volume检查", "")  # 也不碰卷


def test_latest_token_takes_last_match_and_extracts_token():
    # 远端命令是 `grep -Eo <url-token 形态> | tail -n 1`，stdout 里已是纯 URL；
    # 这里仍喂两行验证"取最后一行"，并带 & 后缀验证 token 截断
    out = ("https://old.example.com/?token=OLD111\n"
           "https://dsh-a.example.com/?token=NEW222&exp=2")
    fake = FakeExecutor([ExecResult(0, out, "")])
    tok = run(runtime(fake).latest_token("dsh-a"))
    assert tok == ("NEW222", "https://dsh-a.example.com/?token=NEW222&exp=2")
    cmd = fake.calls[0]["command"]
    assert "docker logs" in cmd and "token=" in cmd


def test_latest_token_none_when_absent():
    fake = FakeExecutor([ExecResult(0, "", "")])
    assert run(runtime(fake).latest_token("dsh-a")) is None


def test_runtime_passes_connection_params():
    fake = FakeExecutor()
    run(runtime(fake).stop("c"))
    call = fake.calls[0]
    assert (call["host"], call["port"], call["user"]) == ("192.0.2.10", 2222, "ops")


# ------------------------------------------------------------------ Executor 进程层

@pytest.fixture()
def stub_ssh(tmp_path):
    script = tmp_path / "fake-ssh"
    script.write_text("#!/bin/sh\necho out-line-1\necho err-line-1 >&2\nexit 3\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_executor_streams_lines_exit_code_and_cleans_key(tmp_path, stub_ssh, monkeypatch):
    import os as _os

    from dsh_control.integrations import shell as shell_mod

    seen: list[tuple[str, str]] = []
    chmod_modes: list[int] = []
    unlinked: list[str] = []

    real_chmod, real_unlink = _os.chmod, _os.unlink
    monkeypatch.setattr(shell_mod.os, "chmod", lambda p, m: (chmod_modes.append(m), real_chmod(p, m))[1])
    monkeypatch.setattr(shell_mod.os, "unlink", lambda p: (unlinked.append(p), real_unlink(p))[1])

    ex = SshExecutor(ssh_binary=stub_ssh)
    res = run(ex.run(host="192.0.2.10", port=22, user="ops",
                     private_key_pem="-----PEM-----", command="true",
                     line_sink=lambda lv, ln: seen.append((lv, ln))))
    assert res.code == 3 and not res.ok
    assert "out-line-1" in res.stdout and "err-line-1" in res.stderr
    assert ("stdout", "out-line-1") in seen and ("stderr", "err-line-1") in seen
    assert 0o600 in chmod_modes
    assert len(unlinked) == 1
    assert not Path(unlinked[0]).exists()          # 私钥文件用后即删


def test_executor_timeout_kills_and_raises(tmp_path):
    script = tmp_path / "slow-ssh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    ex = SshExecutor(ssh_binary=str(script))

    async def go():
        return await ex.run(host="192.0.2.10", port=22, user="ops",
                            private_key_pem="PEM", command="sleep", timeout=0.5)

    with pytest.raises(IntegrationError) as ei:
        run(go())
    assert "超时" in str(ei.value)


def test_executor_requires_pem():
    ex = SshExecutor()
    with pytest.raises(IntegrationError):
        run(ex.run(host="h", port=22, user="u", private_key_pem="", command="true"))


def test_executor_dry_run_records_only():
    from dsh_control.integrations.dry_run import DryRunRecorder
    rec = DryRunRecorder()
    ex = SshExecutor(dry_run=True, recorder=rec)
    res = run(ex.run(host="192.0.2.10", port=22, user="ops",
                     private_key_pem="PEM", command="docker stop c"))
    assert res.ok and res.stdout == ""
    assert len(rec.entries) == 1
    assert rec.entries[0].payload == {"command": "docker stop c"}
