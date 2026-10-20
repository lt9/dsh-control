"""token 抓取的唯一入口。契约的硬要求：任何重启（手动、自动、docker 策略自拉起）
之后都必须重新从容器日志抓 token 落库——详情页 access 永远取最新一条。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repo
from ..db.models import Instance, InstanceToken
from ..integrations.base import DshRuntime


class TokenNotFound(RuntimeError):
    """容器日志里还没有一键地址（容器没起来 / 还没打印）。"""


async def capture_token(
    s: AsyncSession, runtime: DshRuntime, instance: Instance, source: str
) -> InstanceToken:
    """抓最新 token 并落库。source: bootstrap|logs|restart|manual。

    即使 token 与上一条相同也插入新行：语义就是「最近一次抓到的时间」，
    且天然保证 access 取到的永远是最新一条。
    """
    got = await runtime.latest_token(instance.container_name or instance.slug)
    if not got or not got[0]:
        raise TokenNotFound(f"容器 {instance.container_name or instance.slug} 日志中未找到 ?token= 地址")
    token, url = got[0], got[1] if len(got) > 1 else ""
    return await repo.add_token(s, instance.id, token, url, source)
