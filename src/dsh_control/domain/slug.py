"""域名标签（slug）与派生命名。契约 §6.1.1（2026-10-20 二改）：

- slug = `dsh-NN`（两位零填充，服务端顺序生成，**任何接口不接受用户传入的名字**），
  同时是释放的二次确认词。
- 只能一级子域：现成通配证书只覆盖 zone 这一层（`*.zone`）。
- 派生名一律从 slug 出：容器 `dsh-NN`、隧道 `dsh_NN`、NG upstream `DSH_NN`、
  站点 server_name 用完整域名。
- 历史遗留的非 `dsh-NN` 命名实例不参与推导（由人工处理），这里不为它写任何代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 一级子域标签：小写字母/数字/连字符，不以连字符开头结尾
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")


def slug_number(slug: str, prefix: str) -> int | None:
    """dsh-07 + dsh- → 7；不是该前缀的数字化 slug 返回 None。"""
    if not slug or not slug.startswith(prefix):
        return None
    suffix = slug[len(prefix):]
    if not suffix.isdigit():
        return None
    return int(suffix)


def existing_max(slugs: list[str], prefix: str) -> int:
    nums = [n for n in (slug_number(s, prefix) for s in slugs) if n is not None]
    return max(nums) if nums else 0


def next_slug(slugs: list[str], prefix: str) -> str:
    """`dsh-` + 现有实例最大序号 + 1，两位零填充。调用方还要拿它去 DNS 查重。"""
    return f"{prefix}{existing_max(slugs, prefix) + 1:02d}"


def is_valid_label(slug: str) -> bool:
    """一级子域标签校验（不含点）。"""
    return bool(_LABEL_RE.match(slug or ""))


def dns_rr(slug: str) -> str:
    return slug


@dataclass(frozen=True, slots=True)
class DerivedNames:
    container: str
    tunnel: str
    upstream: str
    server_name: str  # 完整域名


def derive(slug: str, zone: str) -> DerivedNames:
    tunnel = slug.replace(".", "_").replace("-", "_")
    return DerivedNames(
        # kit 的 compose 写的是 container_name: dsh-${INSTANCE_NAME}，INSTANCE_NAME
        # 就是 slug，所以容器名是 dsh-<slug>（现网实测 dsh-dsh-01）。名字错一个字符，
        # 看日志、抓 token、重启、停止全都指向不存在的容器（2026-10-21 第 70 步）。
        container=f"dsh-{slug}",
        tunnel=tunnel,
        upstream=tunnel.upper(),
        server_name=f"{slug}.{zone}",
    )


def token_mask(token: str) -> str:
    """打码样式与契约示例一致：M1jq…IV0（前 4 … 后 3）。"""
    if not token:
        return ""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}…{token[-3:]}"
