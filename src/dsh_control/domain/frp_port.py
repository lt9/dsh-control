"""frps 公网端口分配：实例与主控台入口共用同一套逻辑（禁止另写一份）。

规则：起点 = max(DSHC_FRP_PORT_START, 库内已用最大端口+1（released 也计入，
永不复用）, extra_taken+1)；再向面板探测第一个真正空闲的端口。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ..db import repo

Prober = Callable[[str], Awaitable[None]]
Taken = Callable[[int], Awaitable[bool]]


async def port_floor(s: Any, settings: Any, extra_taken: Iterable[Any] = ()) -> int:
    """端口池的起点：max(站点起点, 库内已用最大+1, extra_taken+1)。

    frp 与 direct 共用同一个池、同一个起点（§6.1.4）——列名 frp_remote_port 沿用不改，
    direct 实例发布的本机公网端口也写进这里，所以 max_frp_remote_port 全局计入。
    """
    port = max(settings.frp_port_start, await repo.max_frp_remote_port(s) + 1)
    return max([port, *(int(p) + 1 for p in extra_taken if p)])


async def pick_free_port(
    s: Any,                       # AsyncSession
    frp: Any,                     # FrpProvider（noop 时传 skip_probe=True）
    settings: Any,
    *,
    skip_probe: bool = False,
    extra_taken: Iterable[Any] = (),
    log: Prober | None = None,
) -> int:
    """从 DSHC_FRP_PORT_START 起找第一个未被 frp-panel 占用的公网端口。

    extra_taken：不在 instances 表里的已用端口（如 console-endpoint 记录里
    登记过的入口端口），同样计入「起点之上、永不复用」。
    """
    port = await port_floor(s, settings, extra_taken)
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


async def pick_direct_port(
    s: Any,                       # AsyncSession
    settings: Any,
    taken: Taken,                 # async (port) -> 本机是否已占（ss 在听 ∪ 同机库内在用）
    *,
    extra_taken: Iterable[Any] = (),
    log: Prober | None = None,
) -> int:
    """direct 主机的公网端口分配（§6.1.4）：与 frp 同一个池、同一个起点，
    差别只在**占用探测在目标机上做**——问本机 ss 实听与库里同机实例，不问 frp-panel。
    """
    port = await port_floor(s, settings, extra_taken)
    for _ in range(200):
        if not await taken(port):
            return port
        if log is not None:
            await log(f"本机端口 {port} 已被占用，探测下一个")
        port += 1
    raise RuntimeError("连续 200 个本机端口都被占用")
