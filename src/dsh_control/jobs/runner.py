"""任务引擎：deploy / restart / release 三个状态机共用。

- 步骤与日志全部落库（deploy_steps / deploy_logs），前端按 seq 点对点渲染、
  按 id 增量拉日志。
- 所有往日志/事件里写的文本先过 redact_text()（token/AK/SK/密码不落日志）；
  往 events.data 里写的 dict 先过 integrations.base.redact()。
- 外部调用一律走 integrations/base.py 的 Protocol；实例从
  integrations/registry.build_providers(settings) -> Providers 拿。
  本模块只按下面的 ProvidersLike 形状取属性，不 import 任何具体实现。
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from ..db import repo
from ..db.models import DeployJob, Host, Instance, utcnow
from ..integrations.base import (
    DnsProvider,
    DshRuntime,
    ExecResult,
    Executor,
    FrpProvider,
    NgProvider,
    PublicProbe,
    redact,
)
from ..integrations.sinks import reset_line_sink, set_line_sink
from ..settings import Settings, get_settings

# ---------------------------------------------------------------- 日志安全

_TOKEN_URL_RE = re.compile(r'(https?://[^\s\'"]*[?&]token=)[^\s\'"&]+')
_SECRET_KV_RE = re.compile(
    r"((?:token|secret|password|passwd|pass|access[_-]?key(?:[_-]?(?:id|secret))?|ak|sk"
    r"|authorization|cookie)\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s'\"]+)",
    re.IGNORECASE,
)
_HEADER_RE = re.compile(r"(Authorization|Cookie)\s*:\s*[^\r\n]+", re.IGNORECASE)


def redact_text(text: str) -> str:
    """字符串版的 redact()：token URL、k=v 形式的密钥、请求头一律打码。

    base.py 的 redact() 只处理 dict；本函数是它的字符串补角，
    所有日志行 / 错误信息 / step detail 入库与出网前都必须经过。
    """
    if not text:
        return text
    text = _TOKEN_URL_RE.sub(r"\1***", text)
    text = _HEADER_RE.sub(r"\1: ***", text)
    text = _SECRET_KV_RE.sub(r"\1***", text)
    return text


def _scrub(obj: Any) -> Any:
    """对 redact() 结果再扫一遍：任何嵌套字符串里的 token 形态也打码。"""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(x) for x in obj]
    return obj


class StepFailed(RuntimeError):
    """步骤失败：中止 job，把 message 记进 job.error。"""


class ProvidersLike(Protocol):
    """integrations/registry.build_providers(settings) 返回的 Providers 需满足的形状。

    - dns / frp / ng / probe 四个属性直接是 Protocol 实例；
    - SSH 执行器属性名 executor 或 exec（registry 用 exec，两者都认）；
    - runtime 按主机绑定：DshRuntime 的方法签名里只有 container（base.py 冻结），
      所以 registry 提供 bind(host=,port=,user=,private_key_pem=) 工厂
      （RuntimeBinder），或 runtime_for/…；测试替身可直接给裸 runtime 实例。
    - noop provider 带 manual_required=True 属性（jobs 层据此把步骤标 skipped）。
    """

    dns: DnsProvider
    frp: FrpProvider
    ng: NgProvider
    probe: PublicProbe


def get_executor(providers: Any) -> Executor:
    ex = getattr(providers, "executor", None)
    if ex is None:
        ex = getattr(providers, "exec", None)
    if ex is None or not hasattr(ex, "run"):
        raise RuntimeError("providers 缺少 executor/exec（需有 run 方法）")
    return ex


# ---------------------------------------------------------------- 上下文

@dataclass(slots=True)
class HostEndpoint:
    host: str
    port: int
    user: str
    private_key_pem: str


def make_runtime(providers: Any, ep: HostEndpoint) -> DshRuntime:
    """从 Providers 拿绑定到具体主机的 DshRuntime。

    按优先级找下面任一形状（registry.RuntimeBinder 命中 bind）：
    1. runtime_for/runtime_factory/dsh_runtime_for(host=,port=,user=,private_key_pem=)
    2. providers.runtime.bind(host=,port=,user=,private_key_pem=)（RuntimeBinder）
    3. 裸 runtime 实例（DshRuntime，只适用单主机 / 测试替身）
    """
    for name in ("runtime_for", "runtime_factory", "dsh_runtime_for"):
        factory = getattr(providers, name, None)
        if callable(factory):
            try:
                return factory(host=ep.host, port=ep.port, user=ep.user,
                               private_key_pem=ep.private_key_pem)
            except TypeError:
                return factory(ep.host, ep.port, ep.user, ep.private_key_pem)
    binder = getattr(providers, "runtime", None)
    bind = getattr(binder, "bind", None)
    if callable(bind):
        return bind(host=ep.host, port=ep.port, user=ep.user,
                    private_key_pem=ep.private_key_pem)
    if binder is not None and hasattr(binder, "inspect"):
        return binder
    raise RuntimeError(
        "providers 未提供 DshRuntime：需要 runtime_for(host=,port=,user=,private_key_pem=) "
        "工厂或 runtime 实例（见 jobs/runner.ProvidersLike）"
    )


class JobCtx:
    """一次 job 执行的全部依赖。flows 只碰这个对象。"""

    def __init__(self, db, providers: Any, settings: Settings | None,
                 job: DeployJob, instance: Instance, host: Host,
                 ssh_key_pem: str) -> None:
        self.db = db
        self.s = db.session()
        self.providers = providers
        self.settings = settings or get_settings()
        self.job = job
        self.instance = instance
        self.host = host
        self.ssh_key_pem = ssh_key_pem
        self._seq = 0
        self.rollback_actions: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        # 本次 job **挂回去**的既有对象字段（续建时按名字找回上次建的记录）。
        # 回滚清登记 id 时这些字段必须跳过：对象是上一次尝试建的、还活着，
        # 把它 id 抹掉就成了孤儿（页面看不见、释放删不掉）。
        self.kept_fields: set[str] = set()
        # 面板/插件同步回调写进来的行，步骤边界批量落库（见 integrations/sinks.py）
        self._line_buf: list[tuple[str, str]] = []

    # ---- 日志

    async def log(self, level: str, line: str) -> None:
        await repo.add_log(self.s, self.job.id, self._seq, level, redact_text(line))

    def buffered_sink(self, buf: list[tuple[str, str]]) -> Callable[[str, str], None]:
        """base.LineSink 实现：行先缓冲（回调是同步的），步骤边界批量落库。"""
        def sink(level: str, line: str) -> None:
            line = line.rstrip("\r\n")
            if line:
                buf.append((level, line))
        return sink

    def line_sink(self) -> Callable[[str, str], None]:
        """本 job 的行日志入口：面板插件（NG 重试提示等）经 contextvar 写到这里，
        在步骤边界刷进 job 日志，前端点对点看得到。"""
        return self.buffered_sink(self._line_buf)

    async def flush_lines(self, buf: list[tuple[str, str]]) -> None:
        for level, line in buf:
            await self.log(level, line)
        buf.clear()

    async def flush_pending_lines(self) -> None:
        """收尾时用：把最后一个步骤之后才回来的插件行落库，别丢。"""
        await self.flush_lines(self._line_buf)

    # ---- providers

    def endpoint(self) -> HostEndpoint:
        return HostEndpoint(self.host.ssh_host, self.host.ssh_port,
                            self.host.ssh_user, self.ssh_key_pem)

    def executor(self) -> Executor:
        return get_executor(self.providers)

    def runtime(self) -> DshRuntime:
        return make_runtime(self.providers, self.endpoint())

    def dns(self) -> DnsProvider:
        return self.providers.dns

    def frp(self) -> FrpProvider:
        return self.providers.frp

    def ng(self) -> NgProvider:
        return self.providers.ng

    def probe(self) -> PublicProbe:
        return self.providers.probe

    # ---- SSH

    async def ssh(self, command: str, *, timeout: float = 300,
                  stdin: str | None = None, quiet: bool = False) -> ExecResult:
        """stdin 是给密钥用的：不进 command，因此不进日志、不进目标机的 ps。"""
        ep = self.endpoint()
        buf: list[tuple[str, str]] = []
        # quiet：这条命令的 stdout 不进任务日志。自检类命令（读整份配置来判定）
        # 输出上千行，灌进日志就把真正的部署过程埋了——页面要"点对点看进度"，
        # 日志就得只讲发生了什么（判定结果自己写一行结论）。
        result = await self.executor().run(
            host=ep.host, port=ep.port, user=ep.user,
            private_key_pem=ep.private_key_pem, command=command,
            timeout=timeout, stdin=stdin,
            line_sink=None if quiet else self.buffered_sink(buf),
        )
        await self.flush_lines(buf)
        await self.log("info", f"$ {redact_text(command)}  -> exit {result.code}")
        return result

    # ---- 事件（dict 先过 base.redact，再对嵌套字符串过 redact_text——
    #      base.redact 只按 key 名打码，url 里夹带的 token 值要二次清洗）

    async def event(self, kind: str, message: str = "", data: dict[str, Any] | None = None) -> None:
        await repo.add_event(
            self.s, kind, instance_id=self.instance.id, job_id=self.job.id,
            message=redact_text(message), data=_scrub(redact(data or {})),
        )

    # ---- 步骤

    @asynccontextmanager
    async def step(self, seq: int, key: str, title: str):
        # 遗留的插件行先按上一步的 seq 归位，再切到本步（顺序反了会挂错步骤）
        await self.flush_lines(self._line_buf)
        self._seq = seq
        step = await repo.add_step(self.s, self.job.id, seq, key, title)
        step.state = "running"
        step.started_at = utcnow()
        await self.s.commit()
        started = time.monotonic()
        try:
            yield step
        except Exception as exc:
            await self.flush_lines(self._line_buf)
            step.state = "failed"
            step.finished_at = utcnow()
            msg = redact_text(str(exc) or exc.__class__.__name__)
            step.detail = (step.detail + "\n" if step.detail else "") + \
                f"耗时 {time.monotonic() - started:.1f}s\n失败: {msg}"
            await self.s.commit()
            raise StepFailed(f"[{seq}:{key}] {msg}") from exc
        step.state = "succeeded" if step.state == "running" else step.state
        step.finished_at = utcnow()
        step.detail = (step.detail + "\n" if step.detail else "") + \
            f"耗时 {time.monotonic() - started:.1f}s"
        await self.s.commit()
        await self.flush_lines(self._line_buf)

    async def finish_rollback(self, error: str) -> None:
        """失败时按契约「失败怎么办」列做回滚；回滚本身的异常只记日志不回抛。"""
        for name, action in reversed(self.rollback_actions):
            try:
                await self.log("info", f"回滚: {name}")
                await action()
            except Exception as exc:  # noqa: BLE001
                await self.log("error", f"回滚 {name} 失败: {redact_text(str(exc))}")


# ---------------------------------------------------------------- 执行器

FlowFn = Callable[[JobCtx], Awaitable[None]]


class JobRunner:
    """进程内 asyncio 任务表。同一实例同时只允许一个在途 job。"""

    def __init__(self, db, providers: Any, settings: Settings | None = None) -> None:
        self.db = db
        self.providers = providers
        self.settings = settings or get_settings()
        self._tasks: dict[int, asyncio.Task] = {}
        self._flows: dict[str, FlowFn] = {}

    def register(self, kind: str, flow: FlowFn) -> None:
        self._flows[kind] = flow

    def register_flows(self, flows: dict[str, FlowFn]) -> None:
        self._flows.update(flows)

    async def has_active_job(self, instance_id: int) -> bool:
        async with self.db.session() as s:
            for task_id, task in list(self._tasks.items()):
                if task.done():
                    continue
                job = await repo.get_job(s, task_id)
                if job and job.instance_id == instance_id and job.state in ("pending", "running"):
                    return True
        return False

    def spawn(self, job_id: int) -> asyncio.Task:
        task = self._tasks.get(job_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self._run(job_id), name=f"dshc-job-{job_id}")
        self._tasks[job_id] = task
        return task

    async def wait(self, job_id: int, timeout: float = 30) -> None:
        task = self._tasks.get(job_id)
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout)

    async def cancel_all(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()

    async def _run(self, job_id: int) -> None:
        async with self.db.session() as s:
            job = await repo.get_job(s, job_id)
            if job is None or job.state not in ("pending",):
                return
            flow = self._flows.get(job.kind)
            if flow is None:
                job.state = "failed"
                job.error = f"未知任务类型 {job.kind}"
                job.finished_at = utcnow()
                await s.commit()
                return
            instance = await repo.get_instance(s, job.instance_id)
            host = await repo.get_host(s, instance.host_id) if instance else None
            ssh_key_pem = ""
            if host and host.ssh_credential_id:
                from ..db.models import Credential
                cred = await s.get(Credential, host.ssh_credential_id)
                ssh_key_pem = (cred.secret or {}).get("private_key_pem", "") if cred else ""
            if job is None or instance is None or host is None:
                job.state = "failed"
                job.error = "实例或主机不存在"
                job.finished_at = utcnow()
                await s.commit()
                return
            job.state = "running"
            job.started_at = utcnow()
            await s.commit()
            if job.kind == "deploy":
                await repo.set_instance_state(s, instance, "deploying")
            elif job.kind == "restart":
                await repo.set_instance_state(s, instance, "restarting")
            ctx = JobCtx(self.db, self.providers, self.settings, job, instance,
                         host, ssh_key_pem)
            # 换绑到 ctx 自己的 session：flow 里的字段改动必须能被 ctx.s 刷出，
            # 用外层 session 的实例对象会静默丢失 UPDATE。
            ctx.job = await repo.get_job(ctx.s, job.id)
            ctx.instance = await repo.get_instance(ctx.s, job.instance_id)
            ctx.host = await repo.get_host(ctx.s, ctx.instance.host_id)

        error: str | None = None
        # 面板插件（NG 重试提示这类）拿到的是进程级共享实例，回调签名里塞不进
        # "当前哪个 job"，所以按执行上下文绑定；两个 job 并发也不串日志。
        _sink_token = set_line_sink(ctx.line_sink())
        try:
            await flow(ctx)
        except StepFailed as exc:
            error = str(exc)
            try:
                await ctx.finish_rollback(error)
            finally:
                await ctx.event("job_failed", error, {"kind": job.kind})
        except asyncio.CancelledError:
            error = "任务被取消（服务重启？）"
            async with self.db.session() as s2:
                j2 = await repo.get_job(s2, job_id)
                if j2:
                    j2.state = "failed"
                    j2.error = error
                    j2.finished_at = utcnow()
                    await s2.commit()
            raise
        except Exception as exc:  # noqa: BLE001
            error = redact_text("".join(traceback.format_exception_only(exc)).strip())
        finally:
            try:
                await ctx.flush_pending_lines()
            except Exception:  # noqa: BLE001, S110 —— 刷不动日志不能掩盖真正的失败
                pass
            reset_line_sink(_sink_token)
            try:
                await ctx.s.close()
            except Exception:  # noqa: BLE001, S110 —— 收尾关不上也不能掩盖真正的失败
                pass

        async with self.db.session() as s:
            j = await repo.get_job(s, job_id)
            inst = await repo.get_instance(s, j.instance_id)
            if error is None:
                j.state = "succeeded"
                # deploy/restart 成功回到 running；release 由 mark_released 步骤收尾
                if inst and j.kind in ("deploy", "restart"):
                    await repo.set_instance_state(s, inst, "running")
            else:
                j.state = "failed"
                j.error = error[:4000]
                # release/purge 失败不许把实例改判 error：它们的前态（released）
                # 本身就是可停留状态，purge 还要能重跑或改走恢复（契约 §6.4
                # 「记 error，可重跑」说的是工单）。deploy/restart 才落 error。
                if inst and j.kind not in ("release", "purge"):
                    await repo.set_instance_state(s, inst, "error", last_error=error[:2000])
                elif inst:
                    inst.last_error = error[:2000]
            j.finished_at = utcnow()
            await s.commit()
            await repo.add_event(
                s, f"job_{j.kind}_{j.state}", instance_id=j.instance_id, job_id=j.id,
                message=redact_text(error or "完成"),
            )
