"""按主机的制品下载基址覆盖（契约 §6.1.5）：hosts 加 image_base_url。

站点的下载基址是控制台公网域；入口机=目标机本身时，公网域解析回自家 EIP 是
hairpin 黑洞（§6.1.4 同款陷阱，2026-10-22 dsh-05 实测），这台机必须从够得着
的入口拉（登记 frps 隧道口一类的本机地址）。空=用设置页/站点的生效值。

列可空、无 CHECK，add_column 双方言通用；comment 只在 PG 补。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE_COMMENT = ("该机拉镜像制品的下载基址覆盖（如 http://198.51.100.77:4893）："
                 "公网域解析回自家 EIP 的入口机（hairpin 黑洞，§6.1.4）登记一个"
                 "够得着的入口；空=用设置页/站点的生效值（§6.1.5）")


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column("image_base_url", sa.String(length=255), nullable=True,
                  comment=_BASE_COMMENT),
    )
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(sa.text(f"COMMENT ON COLUMN hosts.image_base_url IS '{_BASE_COMMENT}'"))


def downgrade() -> None:
    op.drop_column("hosts", "image_base_url")
