"""直连主机（契约 §2/§6.1.4）：hosts 加 upstream_mode / mem_limit / cpu_limit 三列。

- `upstream_mode`：公网回源方式，'frp'（默认，NAT 后的机器走隧道）或 'direct'
  （本机有公网 IP，开通第 40 步不建隧道、第 50 步 NG 直接回源 ssh_host）。
  text+CHECK，不用 PG enum（规矩同 §2.0）；默认 'frp' 让既有主机行为零变化。
- `mem_limit` / `cpu_limit`：该机实例的资源配额（如 "1g"/"2"），开通第 60 步写进
  实例 .env 的 MEM_LIMIT/CPU_LIMIT；留空=kit 默认 4g/4 核。共享机必填防 OOM。

CHECK 交换只在 PG 做（SQLite 测试库每次 create_all、模型已带 CHECK，照 0004/0005
的显式判方言做法）；三列 add_column 双方言都执行，comment 在不支持方言上被忽略。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODE_COMMENT = ("公网回源方式：frp=走隧道（默认，NAT 后的机器）；"
                 "direct=本机有公网 IP 直连，不建隧道，NG 直接回源 ssh_host（§6.1.4）")
_MEM_COMMENT = ("该机实例内存上限（如 \"1g\"/\"512m\"），开通第 60 步写进实例 .env 的 "
                "MEM_LIMIT；留空=kit 默认 4g。共享机必填，防 OOM 伤及同机服务（§2）")
_CPU_COMMENT = ("该机实例 CPU 上限（如 \"2\"），开通第 60 步写进实例 .env 的 "
                "CPU_LIMIT；留空=kit 默认 4 核。共享机必填（§2）")
_TABLE_COMMENT = ("被管主机：SSH + docker，dsh-family kit 所在；"
                  "upstream_mode 决定实例公网回源走隧道还是本机直连（§6.1.4）")


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column("upstream_mode", sa.String(length=16), nullable=False,
                  server_default="frp", comment=_MODE_COMMENT),
    )
    op.add_column(
        "hosts",
        sa.Column("mem_limit", sa.String(length=32), nullable=True, comment=_MEM_COMMENT),
    )
    op.add_column(
        "hosts",
        sa.Column("cpu_limit", sa.String(length=32), nullable=True, comment=_CPU_COMMENT),
    )
    if op.get_context().dialect.name != "postgresql":
        return
    op.create_check_constraint(
        "hosts_upstream_mode_check", "hosts",
        sa.text("upstream_mode IN ('frp', 'direct')"))
    op.execute(sa.text(f"COMMENT ON COLUMN hosts.upstream_mode IS '{_MODE_COMMENT}'"))
    op.execute(sa.text(f"COMMENT ON COLUMN hosts.mem_limit IS '{_MEM_COMMENT}'"))
    op.execute(sa.text(f"COMMENT ON COLUMN hosts.cpu_limit IS '{_CPU_COMMENT}'"))
    op.execute(sa.text(f"COMMENT ON TABLE hosts IS '{_TABLE_COMMENT}'"))


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(sa.text("COMMENT ON TABLE hosts IS "
                           "'被管主机：SSH + docker，dsh-family kit 所在'"))
        op.drop_constraint("hosts_upstream_mode_check", "hosts", type_="check")
    op.drop_column("hosts", "cpu_limit")
    op.drop_column("hosts", "mem_limit")
    op.drop_column("hosts", "upstream_mode")
