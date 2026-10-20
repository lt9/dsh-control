"""frps 公网端口分配：实例与主控台入口共用同一套逻辑（禁止另写一份）。

规则：起点 = max(DSHC_FRP_PORT_START, 库内已用最大端口+1（released 也计入，
永不复用）, extra_taken+1)；再向面板探测第一个真正空闲的端口。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ..db import repo

Prober = Callable[[str], Awaitable[None]]


async def pick_free_port(
    s: Any,                       # AsyncSession
    frp: Any,                     # FrpProvider（noop 时传 skip_probe=True）
    settings: Any,
    *,
    skip_probe: bool = False,
    extra_taken: Iterable[Any] = (),
    log: Prober | None = None,
) -> int:
    """从 DSHC_FRP_PORT_START 起找第一个未占用的公网端口。

    extra_taken：不在 instances 表里的已用端口（如 console-endpoint 记录里
    登记过的入口端口），同样计入「起点之上、永不复用」。
    """
    port = max(settings.frp_port_start, await repo.max_frp_remote_port(s) + 1)
    port = max([port, *(int(p) + 1 for p in extra_taken if p)])
    if skip_probe:
        # 没有面板可探测：只做库内去重，人工建隧道时按这个端口做
        return port
    for _ in range(200):
        if not await frp.remote_port_taken(port):
            return port
        if log is not None:
            await log(f"frp 端口 {port} 已被占用，探测下一个")
        port += 1
    raise RuntimeError("连续 200 个 frp 端口都被占用")
