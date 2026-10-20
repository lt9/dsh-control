"""SSH 执行器与被管主机上的 DSH 容器运行时（ARCHITECTURE §5.1）。

执行方式固定为：``ssh -i <临时私钥文件> -p <port> -o BatchMode=yes -o
StrictHostKeyChecking=accept-new user@host '<command>'``，逐行回调 ``LineSink``，
超时杀进程。私钥来自入参 PEM，写 0600 临时文件用后即删，绝不落日志。

这里没有任何站点专属值：主机、端口、用户、密钥全部由调用方（编排层从 ``hosts`` 表
与 ``credentials`` 取）逐个传入。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import tempfile
from contextlib import suppress

from .base import (
    DOCKER_INSPECT_SNIPPET,
    ContainerState,
    ExecResult,
    IntegrationError,
    LineSink,
)
from .dry_run import DryRunRecorder

# 容器日志里的一键地址（ARCHITECTURE §5.1 原样）。[?] 与 \? 在 ERE 里等价，
# 放在 shell 双引号/单引号里都免转义。
_TOKEN_PATTERN = r"https?://[^[:space:]]+[?]token=[^[:space:]]+"
# 抓 token 时最多翻这么多行日志，防大容器日志把 SSH 拖死
_LOG_TAIL_LINES = 5000


def _feed(line_sink: LineSink | None, level: str, line: str) -> None:
    if line_sink is None:
        return
    try:
        line_sink(level, line)
    except Exception:  # noqa: BLE001, S110 — 日志回调不许把主流程带崩
        pass


class SshExecutor:
    """实现 base.Executor。无状态；每台主机的连接参数逐次传入。"""

    def __init__(
        self,
        *,
        ssh_binary: str = "ssh",
        connect_timeout: int = 15,
        dry_run: bool = False,
        recorder: DryRunRecorder | None = None,
    ) -> None:
        self.ssh_binary = ssh_binary
        self.connect_timeout = connect_timeout
        self.dry_run = dry_run
        self.recorder = recorder

    async def run(
        self,
        *,
        host: str,
        port: int,
        user: str,
        private_key_pem: str,
        command: str,
        timeout: float = 300,
        line_sink: LineSink | None = None,
    ) -> ExecResult:
        if self.dry_run:
            if self.recorder is not None:
                self.recorder.record(
                    "ssh", "exec", method="SSH", target=f"{user}@{host}:{port}",
                    payload={"command": command}, note="dry-run: 命令未执行",
                )
            return ExecResult(code=0, stdout="", stderr="[dry-run] ssh 命令未执行")

        if not private_key_pem:
            raise IntegrationError(
                f"缺少 {user}@{host} 的 SSH 私钥（credentials kind=ssh_key 未登记？）",
                code="no_ssh_key",
            )

        fd, key_path = tempfile.mkstemp(prefix="dshc-key-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(private_key_pem if private_key_pem.endswith("\n")
                        else private_key_pem + "\n")
            os.chmod(key_path, 0o600)
            return await self._spawn(
                host=host, port=port, user=user, key_path=key_path,
                command=command, timeout=timeout, line_sink=line_sink,
            )
        finally:
            with suppress(OSError):
                os.unlink(key_path)

    async def _spawn(
        self,
        *,
        host: str,
        port: int,
        user: str,
        key_path: str,
        command: str,
        timeout: float,
        line_sink: LineSink | None,
    ) -> ExecResult:
        argv = [
            self.ssh_binary,
            "-i", key_path,
            "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "IdentitiesOnly=yes",
            f"{user}@{host}",
            command,
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out_lines: list[str] = []
        err_lines: list[str] = []
        pumps = [
            asyncio.create_task(self._pump(proc.stdout, "stdout", out_lines, line_sink)),
            asyncio.create_task(self._pump(proc.stderr, "stderr", err_lines, line_sink)),
        ]
        try:
            code = await asyncio.wait_for(proc.wait(), timeout)
            await asyncio.gather(*pumps)
        except TimeoutError:
            self._kill(proc)
            with suppress(Exception):
                await proc.wait()
            await asyncio.gather(*pumps, return_exceptions=True)
            result = ExecResult(code=-1, stdout="\n".join(out_lines),
                                stderr="\n".join(err_lines))
            raise IntegrationError(
                f"SSH 命令超时（>{timeout:.0f}s），已终止远端会话",
                code="timeout", detail=result.tail(),
            ) from None
        return ExecResult(code=code if code is not None else (proc.returncode or 0),
                          stdout="\n".join(out_lines),
                          stderr="\n".join(err_lines))

    @staticmethod
    async def _pump(
        stream: asyncio.StreamReader | None,
        level: str,
        bucket: list[str],
        line_sink: LineSink | None,
    ) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            bucket.append(line)
            _feed(line_sink, level, line)

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()


class SshDshRuntime:
    """实现 base.DshRuntime：绑定一台被管主机的容器操作。

    只发 ``docker inspect / restart / stop / logs / exec``；实现里绝不出现删容器、
    删卷、删数据目录的任何命令（ARCHITECTURE §0.3）。
    """

    def __init__(
        self,
        executor: SshExecutor,
        *,
        host: str,
        user: str,
        port: int = 22,
        private_key_pem: str,
        dry_run: bool = False,
    ) -> None:
        self.executor = executor
        self.host = host
        self.user = user
        self.port = port
        self.private_key_pem = private_key_pem
        self.dry_run = dry_run

    async def _run(
        self, command: str, line_sink: LineSink | None = None, timeout: float = 120,
    ) -> ExecResult:
        return await self.executor.run(
            host=self.host, port=self.port, user=self.user,
            private_key_pem=self.private_key_pem,
            command=command, timeout=timeout, line_sink=line_sink,
        )

    async def inspect(self, container: str) -> ContainerState | None:
        if self.dry_run:
            return None
        command = DOCKER_INSPECT_SNIPPET.format(name=shlex.quote(container))
        result = await self._run(command, timeout=60)
        if not result.ok:
            return None
        return self._parse_inspect(container, result.stdout)

    @staticmethod
    def _parse_inspect(container: str, stdout: str) -> ContainerState | None:
        # 注意：base.DOCKER_INSPECT_SNIPPET 里的续行反斜杠会原样落在输出行里，
        # 解析前先去掉反斜杠再按 | 拆。
        for line in reversed(stdout.splitlines()):
            line = line.strip().replace("\\", "")
            if not line:
                continue
            if line == "absent":
                return None
            parts = line.split("|")
            if len(parts) < 6:
                return None
            status, health, image, image_id, restart_count, ip = parts[:6]
            image_id = image_id.removeprefix("sha256:")[:12]
            health = health if health in ("healthy", "unhealthy", "starting") else "none"
            try:
                rc = int(restart_count)
            except ValueError:
                rc = 0
            return ContainerState(
                name=container, state=status, health=health,  # type: ignore[arg-type]
                image=image, image_id=image_id, restart_count=rc, ip=ip,
            )
        return None

    async def restart(self, container: str, line_sink: LineSink | None = None) -> ExecResult:
        return await self._run(f"docker restart {shlex.quote(container)}",
                               line_sink=line_sink, timeout=120)

    async def stop(self, container: str, line_sink: LineSink | None = None) -> ExecResult:
        # 只停，不删：容器、数据目录、镜像一律保留（§0.3）。
        return await self._run(f"docker stop {shlex.quote(container)}",
                               line_sink=line_sink, timeout=120)

    async def latest_token(self, container: str) -> tuple[str, str] | None:
        if self.dry_run:
            return None
        q = shlex.quote(container)
        command = (
            f"docker logs --tail {_LOG_TAIL_LINES} {q} 2>&1"
            f" | grep -Eo {shlex.quote(_TOKEN_PATTERN)} | tail -n 1"
        )
        result = await self._run(command, timeout=60)
        url = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if not url or "?" not in url:
            return None
        query = url.split("?", 1)[1]
        for piece in query.split("&"):
            if piece.startswith("token="):
                return piece[len("token="):], url
        return None

    async def version(self, container: str) -> str:
        if self.dry_run:
            return ""
        q = shlex.quote(container)
        result = await self._run(f"docker exec {q} dsh --version", timeout=60)
        if result.ok and result.stdout.strip():
            return result.stdout.strip().splitlines()[0].strip()
        # 退而求其次：镜像 tag 至少能把「跑的是哪个构建」对上（image_tag 列的镜像名）
        result = await self._run(
            f"docker inspect --format '{{{{.Config.Image}}}}' {q} 2>/dev/null",
            timeout=60,
        )
        return result.stdout.strip() if result.ok else ""
