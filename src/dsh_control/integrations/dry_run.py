"""dry-run 记录器：适配器在 dry_run 模式下只把「将要发出的请求」记到这里，不真发。

约定：
- 读操作（列/查/探活）不受 dry_run 影响，永远真发——幂等判断需要先读后写。
- 写操作（创建/更新/删除/重载/远端命令）在 dry_run 下调用 ``recorder.record(...)``
  后直接返回合成结果。
- 落进记录器的 payload 一律先过 ``base.redact``（密钥类键换成 ***）。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .base import redact

# sink: (一行人类可读文本) -> None，编排层可以接到 deploy_steps 日志里
LogSink = Callable[[str], None]


@dataclass(slots=True)
class DryRunEntry:
    """一条「本来要发出但被拦下」的请求。"""

    adapter: str          # frp / dns / ng / ssh
    action: str           # update_config / AddDomainRecord / docker stop ...
    method: str           # POST / GET / SSH
    target: str           # URL 或 host
    payload: dict[str, Any]   # 已过 redact 的请求体 / 命令
    note: str = ""

    def line(self) -> str:
        extra = f" note={self.note}" if self.note else ""
        return f"[dry-run] {self.adapter} {self.method} {self.target} {self.action}{extra}"


@dataclass(slots=True)
class DryRunRecorder:
    """所有适配器共享一个实例；registry 建 Providers 时创建并注入。"""

    entries: list[DryRunEntry] = field(default_factory=list)
    on_record: LogSink | None = None

    def record(
        self,
        adapter: str,
        action: str,
        *,
        method: str,
        target: str,
        payload: dict[str, Any] | None = None,
        note: str = "",
    ) -> DryRunEntry:
        entry = DryRunEntry(
            adapter=adapter,
            action=action,
            method=method,
            target=target,
            payload=dict(redact(payload or {})),  # type: ignore[arg-type]
            note=note,
        )
        self.entries.append(entry)
        if self.on_record is not None:
            self.on_record(entry.line())
        return entry

    def reset(self) -> None:
        self.entries.clear()


def utc_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
