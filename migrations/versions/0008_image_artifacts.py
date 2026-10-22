"""镜像分发落地（契约 §6.1.5/§2/§8）：新表 image_artifacts + deploy_jobs.kind 加 'publish'。

- `image_artifacts`：制品库登记表，一行一个 tag；「行存在即文件已完整落库」由
  发布工单的执行顺序保证（文件落定才插行，换行成功才删旧行旧文件）。
  token UNIQUE 是公开下载链接的门票——下载端点按 token 单表精确查，永远不
  接受调用方传路径段。
- `deploy_jobs.kind` 追加 'publish'：发布是全站工单，不挂实例（instance_id=0），
  照 0005 的 _swap_check 模式交换 CHECK（只许追加，防把既有权值删掉现网必炸）。

规矩同 0001–0007（ARCHITECTURE §2.0）：无外键、无联表、每列中文 comment、
text+CHECK 不用 PG enum、迁移里零 INSERT。SQLite 无法就地改 CHECK，测试库每次
create_all 重建、模型已是新集合，所以 CHECK 交换只在 PG 做；建表双方言都执行，
comment 在非支持方言上被 SQLAlchemy 忽略。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigIntPk = sa.BigInteger().with_variant(sa.Integer, "sqlite")

_KINDS_OLD = ("deploy", "restart", "release", "purge")
_KINDS_NEW = _KINDS_OLD + ("publish",)
_KIND_COMMENT = "任务类型：deploy/restart/release/purge/publish"
_JOBS_TABLE_COMMENT = "一次性任务：deploy/restart/release/purge/publish 状态机"
_ARTIFACTS_TABLE_COMMENT = "镜像制品库：一行一个 tag，行存在即文件已完整落库（§6.1.5）"


def _swap_check(table: str, col: str, old: tuple[str, ...], new: tuple[str, ...],
                comment: str) -> None:
    """CHECK 白名单交换（照 0005）：只准追加，防把既有权值删掉现网必炸。"""
    assert set(old) < set(new), f"{table}.{col} 的 CHECK 只许追加取值"
    op.drop_constraint(f"{table}_{col}_check", table, type_="check")
    quoted = ", ".join(f"'{v}'" for v in new)
    op.create_check_constraint(f"{table}_{col}_check", table,
                               sa.text(f"{col} IN ({quoted})"))
    op.execute(sa.text(f"COMMENT ON COLUMN {table}.{col} IS '{comment}'"))


def upgrade() -> None:
    op.create_table(
        "image_artifacts",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("tag", sa.String(255), nullable=False,
                  comment="镜像标签（docker save 的那个 tag）"),
        sa.Column("filename", sa.String(255), nullable=False,
                  comment="store 目录内的文件名（tag 安全化 + .tar.gz），"
                          "下载路径只认 token、这列只做落盘与展示"),
        sa.Column("size_bytes", sa.BigInteger, nullable=False, comment="文件字节数"),
        sa.Column("sha256", sa.String(64), nullable=False,
                  comment="文件 sha256（发布时算好；目标机拉完强制 sha256sum -c）"),
        sa.Column("token", sa.String(32), nullable=False,
                  comment="公开下载链接的凭证（随机 26 字符 urlsafe）；"
                          "免鉴权，token 即门票；撤销=删行删文件链接立死"),
        sa.Column("origin_host", sa.String(128), nullable=False,
                  comment="从哪台主机 save 出来的；空串=手工投放"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  comment="发布时间（UTC）"),
        sa.UniqueConstraint("tag"),
        sa.UniqueConstraint("token"),
        comment=_ARTIFACTS_TABLE_COMMENT,
    )
    op.create_index("ix_image_artifacts_token", "image_artifacts", ["token"])
    if op.get_context().dialect.name != "postgresql":
        return
    _swap_check("deploy_jobs", "kind", _KINDS_OLD, _KINDS_NEW, _KIND_COMMENT)
    op.execute(sa.text(f"COMMENT ON TABLE deploy_jobs IS '{_JOBS_TABLE_COMMENT}'"))
    op.execute(sa.text(f"COMMENT ON TABLE image_artifacts IS '{_ARTIFACTS_TABLE_COMMENT}'"))


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(sa.text("COMMENT ON TABLE deploy_jobs IS "
                           "'一次性任务：deploy/restart/release/purge 状态机'"))
        _swap_check("deploy_jobs", "kind", _KINDS_NEW, _KINDS_OLD,
                    "任务类型：deploy/restart/release/purge")
    op.drop_index("ix_image_artifacts_token", table_name="image_artifacts")
    op.drop_table("image_artifacts")
