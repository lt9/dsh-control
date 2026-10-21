"""按 job 绑定的行日志通道：providers 是进程级共享的，sink 不能挂在实例上。"""

from __future__ import annotations

import asyncio

from dsh_control.integrations.sinks import bind_line_sink, emit_line


def test_emit_without_binding_is_silent():
    emit_line("info", "没人接着也不许炸")


async def test_routing_is_per_task_not_per_instance():
    got_a: list[str] = []
    got_b: list[str] = []

    async def job(name: str, buf: list[str]) -> None:
        with bind_line_sink(lambda level, line: buf.append(f"{name}:{line}")):
            await asyncio.sleep(0.01)      # 让两个 job 真正交错
            emit_line("info", "面板重试中")

    await asyncio.gather(job("A", got_a), job("B", got_b))
    # 并发两个 job 各看各的：日志串门就是详情页张冠李戴
    assert got_a == ["A:面板重试中"], got_a
    assert got_b == ["B:面板重试中"], got_b


async def test_binding_is_restored_after_exception():
    try:
        with bind_line_sink(lambda *_: None):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    emit_line("info", "出异常后不该还有接收方")   # 未绑定 → 静默，不抛
