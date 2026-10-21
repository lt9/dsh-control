"""补 ng_upstream_id 登记列（D1）：开通时登记、释放/回滚随站点一并精确删除。

实测 bug：释放与开通回滚只删 NG 站点、从不删 upstream，面板里每失败/释放一次
攒一条孤儿 DSH_NN。修复要求 upstream id 落库，删完回读确认。

规矩同 0001（ARCHITECTURE §2.0）：无外键、无联表、每列中文 comment、
timestamptz 用 UTC、text+CHECK 不用 PG enum、迁移里零 INSERT。
顺带刷新 frp_remote_port 的列 comment：释放后保留不复用（既有契约），
仅开通失败回滚时清空放回池子——models 注释已同步，这里把现网 PG 的
COMMENT ON COLUMN 也刷一致（SQLite 忽略 comment，无副作用）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("ng_upstream_id", sa.BigInteger(), nullable=True,
                  comment="nginxWebUI upstream id（释放随站点一并删；空=未登记/人工层）"),
    )
    if op.get_context().dialect.name == "postgresql":
        # SQLite 没有列 comment 的 DDL（alembic 直接拒绝编译），只刷 PG。
        op.alter_column(
            "instances", "frp_remote_port",
            existing_type=sa.Integer(), existing_nullable=True,
            comment="frps 公网端口（释放后保留不复用；仅开通失败回滚时清空放回池子）",
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.alter_column(
            "instances", "frp_remote_port",
            existing_type=sa.Integer(), existing_nullable=True,
            comment="frps 公网端口（释放后保留，不复用）",
        )
    op.drop_column("instances", "ng_upstream_id")
