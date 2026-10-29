"""记忆中枢（memory-hub）P1：credentials.kind 与 deploy_jobs.kind 各加宽两项。

- `credentials.kind` 追加 **memory_user_key / memory_admin_key**：
  - memory_user_key：每台实例的业务用户 key（name=dshctl-<slug>），开通第 68 步
    写入，容器里的 refs 只是它的副本；
  - memory_admin_key：Hub 的 admin 身份（建议 name=memory-admin），唯一能过
    core `assertCanManageUsers` 的凭据（MEMORY-PANEL-EMBED.md ②），seed 灌入。
- `deploy_jobs.kind` 追加 **memory**：running 实例上的记忆启用/停用工单
  （params {"action": "enable"|"disable"}，可重跑）。
- purge 的记忆处置**不加列不加参数列**：工单附加参数列 params（JSON）自 0005
  就有，memory_disposal 走 params（jobs/purge.py 15 步），缺省 = keep。

向后兼容（现网可直接跑）：两个 CHECK 都只做**追加**交换（照 0005/0008/0010 的
_swap_check），老行全部满足新约束，ADD CONSTRAINT 不会被存量行违反；正在跑的
老应用不认识新权值、也只会写老权值，升级窗口内不炸。downgrade 相反——库里
已有 memory_* 行时会 violated by some row，属预期（先清行再降）。

规矩同 0001–0011（ARCHITECTURE §2.0）：无外键、无联表、每列中文 comment、
text+CHECK 不用 PG enum、迁移里零 INSERT。SQLite 无法就地改 CHECK，测试库每次
create_all 重建、模型已是新集合，所以 CHECK 交换只在 PG 做（显式判方言）。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# credentials.kind：OLD 必须逐字接上 0010 的 NEW（含 mirror_token）——
# 0004 的教训（OLD 漏一项 = 把该项从白名单里删了，现网 ADD CONSTRAINT 必炸），
# tests/unit/test_unit_migration_kinds.py 逐字钉死这条链。
_KINDS_OLD = ("ssh_key", "ssh_password", "aliyun_ak", "frp_panel",
              "ng_panel", "operator_key", "site", "llm_key", "image_key",
              "mirror_token")
_KINDS_NEW = _KINDS_OLD + ("memory_user_key", "memory_admin_key")
_KIND_COMMENT = ("类别：ssh_key/ssh_password/aliyun_ak/frp_panel/ng_panel"
                 "/operator_key/site/llm_key/image_key/mirror_token"
                 "/memory_user_key/memory_admin_key")

# deploy_jobs.kind：OLD 接 0008 的 NEW（含 publish）。
_JOBS_OLD = ("deploy", "restart", "release", "purge", "publish")
_JOBS_NEW = _JOBS_OLD + ("memory",)
_JOBS_KIND_COMMENT = "任务类型：deploy/restart/release/purge/publish/memory"
_JOBS_TABLE_COMMENT = ("一次性任务：deploy/restart/release/purge/publish/memory "
                       "状态机（memory=记忆启用/停用，可重跑）")


def _swap_check(table: str, col: str, old: tuple[str, ...], new: tuple[str, ...],
                comment: str, *, growing: bool = True) -> None:
    """CHECK 白名单交换（照 0005/0008/0010）：upgrade 只准追加，防把既有权值
    删掉现网必炸；downgrade 反过来——必须确实在收窄，且只能收到 old 的子集，
    否则回滚会把没登记过的值放进白名单。"""
    if growing:
        assert set(old) < set(new), f"{table}.{col} 的 CHECK 只许追加取值"
    else:
        assert set(new) < set(old), f"{table}.{col} 回滚必须收窄且不越出旧白名单"
    op.drop_constraint(f"{table}_{col}_check", table, type_="check")
    quoted = ", ".join(f"'{v}'" for v in new)
    op.create_check_constraint(f"{table}_{col}_check", table,
                               sa.text(f"{col} IN ({quoted})"))
    op.execute(sa.text(f"COMMENT ON COLUMN {table}.{col} IS '{comment}'"))


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    _swap_check("credentials", "kind", _KINDS_OLD, _KINDS_NEW, _KIND_COMMENT)
    _swap_check("deploy_jobs", "kind", _JOBS_OLD, _JOBS_NEW, _JOBS_KIND_COMMENT)
    op.execute(sa.text(f"COMMENT ON TABLE deploy_jobs IS '{_JOBS_TABLE_COMMENT}'"))


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    _swap_check("credentials", "kind", _KINDS_NEW, _KINDS_OLD,
                "类别：ssh_key/ssh_password/aliyun_ak/frp_panel/ng_panel"
                "/operator_key/site/llm_key/image_key/mirror_token",
                growing=False)
    _swap_check("deploy_jobs", "kind", _JOBS_NEW, _JOBS_OLD,
                "任务类型：deploy/restart/release/purge/publish", growing=False)
    op.execute(sa.text("COMMENT ON TABLE deploy_jobs IS "
                       "'一次性任务：deploy/restart/release/purge/publish 状态机'"))
