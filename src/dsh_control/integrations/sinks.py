"""按 job 绑定的行日志通道。

providers 是进程级共享的（registry 只建一次），而一次开通的每一步日志必须落到它
自己的 job 里。面板插件的回调是同步的 `LineSink(level, line)`，签名里塞不进"当前
是哪个 job"，所以用 contextvar：runner 起 job 任务时 bind，插件回调时按当前任务
查表转发。两个 job 并发跑各看各的，日志不会串门——把 sink 直接挂到共享的 NgPanel
实例上就会串，那是错的。

没绑定时静默丢弃：CLI 直连面板（expose / drift_scan）本来就没有 job 落地处，
NgPanel 自己会退回 recorder 通道，不会丢审计。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_LINE: ContextVar[Callable[[str, str], None] | None] = ContextVar(
    "dshc_line_sink", default=None)


def emit_line(level: str, line: str) -> None:
    """插件侧唯一入口：转给当前 job 的行缓冲，没绑定就什么也不做。"""
    sink = _LINE.get()
    if sink is not None:
        sink(level, line)


@contextmanager
def bind_line_sink(sink: Callable[[str, str], None] | None) -> Iterator[None]:
    """把 sink 绑到当前执行上下文（asyncio 任务各自独立）。"""
    token = _LINE.set(sink)
    try:
        yield
    finally:
        _LINE.reset(token)


def current_line_sink() -> Callable[[str, str], None] | None:
    return _LINE.get()


Token = Any


def set_line_sink(sink: Callable[[str, str], None] | None) -> Token:
    """给没有 try/finally 结构的调用点用：自己配对 reset_line_sink。"""
    return _LINE.set(sink)


def reset_line_sink(token: Token) -> None:
    _LINE.reset(token)
