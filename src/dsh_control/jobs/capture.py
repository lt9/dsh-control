"""token 抓取的唯一入口。契约的硬要求：任何重启（手动、自动、docker 策略自拉起）
之后都必须重新从容器日志抓 token——详情页 access 永远取最新一条。

§3 去重（落库与事件一律去重）：任何路径抓到一个 token 后，先与该实例库里
最新一条比对——**值没变就什么都不写**（不落 instance_tokens、不记事件，reveal
响应照常返回现取值）；值变了才落库并返回 changed=True，`token_captured`
事件由各调用方在 changed=True 时记（事件 kind 清单不变）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repo
from ..db.models import Instance, InstanceToken
from ..integrations.base import DshRuntime


class TokenNotFound(RuntimeError):
    """容器日志里还没有一键地址（容器没起来 / 还没打印）。"""


@dataclass(slots=True)
class CaptureResult:
    """一次抓取的结果。changed=False 表示库里最新一条与抓到的值相同：
    本次没有写 instance_tokens，调用方也不许记事件；token/url 仍是现取的值，
    source/captured_at 用库里那条的（这条密钥是什么时候抓的，说库里的真话）。"""

    token: str
    url: str
    source: str
    changed: bool
    captured_at: datetime
    row: InstanceToken | None


async def capture_token(
    s: AsyncSession, runtime: DshRuntime, instance: Instance, source: str
) -> CaptureResult:
    """抓最新 token 并按 §3 去重落库。source: bootstrap|logs|restart|manual。

    比对口径是**登录密钥值本身**（与 repo.latest_token 的最新一条比）：
    相同 → 不落新行、原样带回现取值；不同或库里一条都没有 → 插新行。
    """
    got = await runtime.latest_token(instance.container_name or instance.slug)
    if not got or not got[0]:
        raise TokenNotFound(f"容器 {instance.container_name or instance.slug} 日志中未找到 ?token= 地址")
    token, url = got[0], got[1] if len(got) > 1 else ""
    prev = await repo.latest_token(s, instance.id)
    if prev is not None and prev.token == token:
        return CaptureResult(token=token, url=url, source=prev.source, changed=False,
                             captured_at=prev.captured_at, row=prev)
    row = await repo.add_token(s, instance.id, token, url, source)
    return CaptureResult(token=token, url=url, source=source, changed=True,
                         captured_at=row.captured_at, row=row)
