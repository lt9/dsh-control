"""purge 落地（契约 §2/§3/§6.4）：instances.state 加 'purged' + purged_at 列；
deploy_jobs.kind 加 'purge' + params 附加参数列。

- `released` 不再是终态：保留期满（purge_after_days，默认 15 天）或手动确认后
  purge 工单把它终态化为 `purged`（记录与历史保留、只读）。
- `purged_at`：彻底释放时间（UTC），列表/详情显示「已彻底释放」的时间口径。
- `deploy_jobs.params`：工单附加参数——升级的 {"image": "目标tag"}、
  purge 的 {"source": "auto|manual", ...}。可为空，默认 '{}'。

规矩同 0001–0004（ARCHITECTURE §2.0）：无外键、无联表、每列中文 comment、
text+CHECK 不用 PG enum、迁移里零 INSERT。SQLite 无法就地改 CHECK，测试库每次
create_all 重建、模型已是新集合，所以 CHECK 交换只在 PG 做（照 0004 的显式判方言
做法）；两列 add_column 双方言都执行，comment 在非支持方言上被 SQLAlchemy 忽略。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonType = sa.JSON().with_variant(postgresql.JSONB, "postgresql")

_STATES_OLD = ("pending", "deploying", "running", "unhealthy", "restarting",
               "released", "error")
_STATES_NEW = _STATES_OLD[:6] + ("purged",) + _STATES_OLD[6:]   # purged 排在 error 前
_STATE_COMMENT = ("运行状态：pending/deploying/running/unhealthy/restarting/"
                  "released/purged/error")
_KINDS_OLD = ("deploy", "restart", "release")
_KINDS_NEW = _KINDS_OLD + ("purge",)
_KIND_COMMENT = "任务类型：deploy/restart/release/purge"
_JOBS_TABLE_COMMENT = "一次性任务：deploy/restart/release/purge 状态机"


def _swap_check(table: str, col: str, old: tuple[str, ...], new: tuple[str, ...],
                comment: str) -> None:
    """CHECK 白名单交换（照 0004 的 _swap）：只准追加，防把既有权值删掉现网必炸。"""
    assert set(old) < set(new), f"{table}.{col} 的 CHECK 只许追加取值"
    op.drop_constraint(f"{table}_{col}_check", table, type_="check")
    quoted = ", ".join(f"'{v}'" for v in new)
    op.create_check_constraint(f"{table}_{col}_check", table,
                               sa.text(f"{col} IN ({quoted})"))
    op.execute(sa.text(f"COMMENT ON COLUMN {table}.{col} IS '{comment}'"))


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True,
                  comment="彻底释放时间（UTC）；purged 终态落这一刻，保留审计记录的日期口径"),
    )
    op.add_column(
        "deploy_jobs",
        sa.Column("params", JsonType, nullable=True, server_default=sa.text("'{}'"),
                  comment="工单附加参数（如切镜像升级的 {\"image\": \"目标tag\"}、"
                          "purge 的 {\"source\": \"auto|manual\"}），可为空"),
    )
    if op.get_context().dialect.name != "postgresql":
        return
    _swap_check("instances", "state", _STATES_OLD, _STATES_NEW, _STATE_COMMENT)
    _swap_check("deploy_jobs", "kind", _KINDS_OLD, _KINDS_NEW, _KIND_COMMENT)
    op.execute(sa.text(f"COMMENT ON TABLE deploy_jobs IS '{_JOBS_TABLE_COMMENT}'"))


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(sa.text("COMMENT ON TABLE deploy_jobs IS "
                           "'一次性任务：deploy/restart/release 状态机'"))
        _swap_check("deploy_jobs", "kind", _KINDS_NEW, _KINDS_OLD,
                    "任务类型：deploy/restart/release")
        _swap_check("instances", "state", _STATES_NEW, _STATES_OLD,
                    "运行状态：pending/deploying/running/unhealthy/restarting/released/error")
    op.drop_column("deploy_jobs", "params")
    op.drop_column("instances", "purged_at")
