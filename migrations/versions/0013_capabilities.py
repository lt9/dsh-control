"""能力机制落库（ADR-0001 决策四）：新表 capabilities + instance_capabilities。

「货 + 清单」的台账侧：能力知识住在 kit 包的 manifest 里，控制台只留目录与对账——
- `capabilities`：一行一个「能力 + 版本」，manifest 解析全量进 JSONB（未知字段在
  发布时就拒，不等到铺到一半）；current 同一 capability_id 最多一行为真（同 0011
  kit_releases 的换行法）。
- `instance_capabilities`：每实例每能力一行，desired ↔ observed；observed_* 只认
  引擎按 manifest 探针经 SSH docker exec 采回的结果，对账在程序里做，两张表不联。

deploy_jobs.kind 增加 capability（能力工单：升级/重试/禁用/卸载都走工单，享受
SSE 日志、失败续跑、events 留痕）。**_KINDS_OLD 从 0012 的 _KINDS_NEW 抄起**
（含 memory）——0004 的教训：OLD 漏一项现网 ADD CONSTRAINT 直接 violated。
0012 已把 credentials.kind 加宽到 memory_* 两项，本篇不再动它。

规矩同 0001–0012：无外键、无联表、每列中文 comment、迁移里零 INSERT。
SQLite 无法就地改 CHECK，测试库每次 create_all 重建，模型已是新集合；这里只改 PG。

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigIntPk = sa.BigInteger().with_variant(sa.Integer, "sqlite")
JsonT = sa.JSON().with_variant(postgresql.JSONB, "postgresql")

_JOB_KINDS_OLD = ("deploy", "restart", "release", "purge", "publish", "memory")
_JOB_KINDS_NEW = _JOB_KINDS_OLD + ("capability",)
_JOB_KIND_COMMENT = "任务类型：deploy/restart/release/purge/publish/memory/capability"


def _swap_job_kinds(old: tuple[str, ...], new: tuple[str, ...]) -> None:
    op.drop_constraint("deploy_jobs_kind_check", "deploy_jobs", type_="check")
    quoted = ", ".join(f"'{v}'" for v in new)
    op.create_check_constraint("deploy_jobs_kind_check", "deploy_jobs",
                               sa.text(f"kind IN ({quoted})"))
    if old != new:
        op.execute(sa.text(f"COMMENT ON COLUMN deploy_jobs.kind IS '{_JOB_KIND_COMMENT}'"))


def upgrade() -> None:
    op.create_table(
        "capabilities",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("capability_id", sa.String(64), nullable=False,
                  comment="能力 id，等于 kit 里 capabilities/<id> 目录名"),
        sa.Column("version", sa.String(64), nullable=False,
                  comment="包版本，取 manifest 的 version"),
        sa.Column("kind", sa.String(16), nullable=False,
                  comment="能力类别：skill/patch/plugin/service，asset 为第五类"
                          "（U-5，P2 只留解析位）；决定装法与卸法"),
        sa.Column("title", sa.String(128), nullable=False, server_default="",
                  comment="详情页展示名"),
        sa.Column("manifest", JsonT, nullable=False,
                  comment="manifest 解析结果全量（参数 schema 与 kind 专属字段都在里面）；"
                          "kit publish 时读包内文件解析，有未知字段就拒发"),
        sa.Column("manifest_sha256", sa.String(64), nullable=False, server_default="",
                  comment="manifest 正文 sha256：与目标机上换装落地的文件比对，发现货与台账不符"),
        sa.Column("kit_version", sa.String(64), nullable=False, server_default="",
                  comment="这份 manifest 随哪个 kit 版本发货，逻辑指向 kit_releases.version，无外键"),
        sa.Column("compatible_dsh", sa.String(64), nullable=False, server_default="",
                  comment="兼容的 DSH 版本区间原文，切镜像前的闸门读它"),
        sa.Column("on_missing", sa.String(16), nullable=False, server_default="failed",
                  comment="缺料策略：skipped=步骤记 skipped（不挡开通）；failed=步骤失败"),
        sa.Column("state", sa.String(16), nullable=False, server_default="published",
                  comment="发布状态：draft/published/retired——只有 published 可铺"),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.false(),
                  comment="是否现行版：同一 capability_id 最多一行为真，发布时代码换行（同 kit_releases）"),
        sa.Column("note", sa.String(512), nullable=False, server_default="",
                  comment="发布说明"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), comment="更新时间（UTC）"),
        sa.CheckConstraint("kind IN ('skill', 'patch', 'plugin', 'service', 'asset')"),
        sa.CheckConstraint("state IN ('draft', 'published', 'retired')"),
        sa.UniqueConstraint("capability_id", "version"),
        comment="能力目录：id/版本/kind/manifest/兼容区间/发布状态（发货台账的一类）",
    )
    op.create_index("ix_capabilities_capability_id", "capabilities", ["capability_id"])
    op.create_index("ix_capabilities_state", "capabilities", ["state"])
    op.create_table(
        "instance_capabilities",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("instance_id", sa.BigInteger(), nullable=False,
                  comment="逻辑指向 instances.id，无外键"),
        sa.Column("capability_id", sa.String(64), nullable=False,
                  comment="逻辑指向 capabilities.capability_id，无外键"),
        sa.Column("desired_state", sa.String(16), nullable=False, server_default="present",
                  comment="期望：present=要装；absent=要卸（卸载工单先翻这列再发命令）"),
        sa.Column("desired_version", sa.String(64), nullable=False, server_default="",
                  comment="期望版本；空串=跟随目录的 current 行"),
        sa.Column("params", JsonT, nullable=False,
                  comment="下发给引擎的非敏感参数；secret 参数不进这列（密钥只住 credentials，"
                          "工单执行时现取现塞 stdin）"),
        sa.Column("installed", sa.Boolean(), nullable=False, server_default=sa.false(),
                  comment="观测：装没装（status 探针的结果）"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
                  comment="观测：启没启；disable 之后资产还在，只是不生效"),
        sa.Column("observed_version", sa.String(64), nullable=False, server_default="",
                  comment="观测版本；空串=版本未知（不按漂移算，按黄标提示），与期望不等即漂移"),
        sa.Column("marker_present", sa.Boolean(), nullable=False, server_default=sa.false(),
                  comment="观测：铺设记号在不在——文件被人手删过的证据"),
        sa.Column("health", sa.String(16), nullable=False, server_default="unknown",
                  comment="健康探针结果：unknown/ok/failed（failed 只黄标，不自动重启实例）"),
        sa.Column("health_detail", sa.Text(), nullable=False, server_default="",
                  comment="健康探针输出摘要（已脱敏）"),
        sa.Column("reason", sa.Text(), nullable=False, server_default="",
                  comment="上次动作的说明：skipped 原因 / 失败尾日志（已脱敏），详情页直接显示"),
        sa.Column("last_job_id", sa.BigInteger(), nullable=False, server_default="0",
                  comment="最近一次动作的工单 id，逻辑指向 deploy_jobs.id，无外键；0=没走过工单"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True,
                  comment="本行观测态采集时间（UTC）；空=从没采过"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), comment="更新时间（UTC）"),
        sa.CheckConstraint("desired_state IN ('present', 'absent')"),
        sa.CheckConstraint("health IN ('unknown', 'ok', 'failed')"),
        sa.UniqueConstraint("instance_id", "capability_id"),
        comment="能力台账：期望版本 ↔ 引擎探针采回的观测态",
    )
    op.create_index("ix_instance_capabilities_instance_id", "instance_capabilities",
                    ["instance_id"])
    op.create_index("ix_instance_capabilities_capability_id", "instance_capabilities",
                    ["capability_id"])
    op.create_index("ix_instance_capabilities_health", "instance_capabilities", ["health"])
    if op.get_context().dialect.name == "postgresql":
        _swap_job_kinds(_JOB_KINDS_OLD, _JOB_KINDS_NEW)


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        _swap_job_kinds(_JOB_KINDS_NEW, _JOB_KINDS_OLD)
    op.drop_table("instance_capabilities")
    op.drop_table("capabilities")
