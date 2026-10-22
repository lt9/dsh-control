"""制品多路分发落地（契约 §6.1.6/§2/§4）：新表 image_channels + credentials.kind 加 'mirror_token'。

- `image_channels`：制品的外部分发渠道，一制品一渠道一行（UNIQUE(artifact_id,
  channel)），没有行=没推过。url 存目标机可直连入口（github=asset 直链、
  gitee=manifest 直链）；第 20 步选路只认 state='ok' 的行，且仍在目标机实测探测。
- `credentials.kind` 追加 'mirror_token'：渠道令牌（name=github/gitee），
  **只写不回显**——照 0008 的 _swap_check 模式交换 CHECK（只许追加）。

规矩同 0001–0009（ARCHITECTURE §2.0）：无外键、无联表、每列中文 comment、
text+CHECK 不用 PG enum、迁移里零 INSERT。SQLite 无法就地改 CHECK，测试库每次
create_all 重建、模型已是新集合，所以 CHECK 交换只在 PG 做；建表双方言都执行。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigIntPk = sa.BigInteger().with_variant(sa.Integer, "sqlite")

_KINDS_OLD = ("ssh_key", "ssh_password", "aliyun_ak", "frp_panel",
              "ng_panel", "operator_key", "site", "llm_key", "image_key")
_KINDS_NEW = _KINDS_OLD + ("mirror_token",)
_KIND_COMMENT = ("类别：ssh_key/ssh_password/aliyun_ak/frp_panel/ng_panel"
                 "/operator_key/site/llm_key/image_key/mirror_token")
_CREDS_TABLE_COMMENT = ("外部密钥仓库：明文存储（用户要求可读回），只在 reveal 接口出网"
                        "；mirror_token 例外——只写不回显（§6.1.6）")
_CHANNELS_TABLE_COMMENT = ("制品的外部分发渠道：一制品一渠道一行，没有行=没推过（§6.1.6）")


def _swap_check(table: str, col: str, old: tuple[str, ...], new: tuple[str, ...],
                comment: str) -> None:
    """CHECK 白名单交换（照 0005/0008）：只准追加，防把既有权值删掉现网必炸。"""
    assert set(old) < set(new), f"{table}.{col} 的 CHECK 只许追加取值"
    op.drop_constraint(f"{table}_{col}_check", table, type_="check")
    quoted = ", ".join(f"'{v}'" for v in new)
    op.create_check_constraint(f"{table}_{col}_check", table,
                               sa.text(f"{col} IN ({quoted})"))
    op.execute(sa.text(f"COMMENT ON COLUMN {table}.{col} IS '{comment}'"))


def upgrade() -> None:
    op.create_table(
        "image_channels",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("artifact_id", sa.BigInteger, nullable=False,
                  comment="逻辑指向 image_artifacts.id，无外键"),
        sa.Column("channel", sa.String(16), nullable=False,
                  comment="渠道：github=整包 asset 直链；gitee=分卷 manifest 直链"),
        sa.Column("state", sa.String(16), nullable=False,
                  comment="渠道状态：pending/ok/failed——只有 ok 才进第 20 步候选"),
        sa.Column("url", sa.String(1024), nullable=False,
                  comment="目标机可直连入口：github=asset 直链；gitee=manifest 直链"),
        sa.Column("release_tag", sa.String(255), nullable=False,
                  comment="发布用的 release tag（image-<tag安全化>）；删旧版按它定位"),
        sa.Column("error", sa.Text, nullable=False,
                  comment="failed 时的原因（已脱敏）；ok 后留空或留历史说明"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  comment="最后推送时间（UTC）"),
        sa.UniqueConstraint("artifact_id", "channel"),
        sa.CheckConstraint("channel IN ('github', 'gitee')",
                           name="image_channels_channel_check"),
        sa.CheckConstraint("state IN ('pending', 'ok', 'failed')",
                           name="image_channels_state_check"),
        comment=_CHANNELS_TABLE_COMMENT,
    )
    op.create_index("ix_image_channels_artifact_id", "image_channels", ["artifact_id"])
    if op.get_context().dialect.name != "postgresql":
        return
    _swap_check("credentials", "kind", _KINDS_OLD, _KINDS_NEW, _KIND_COMMENT)
    op.execute(sa.text(f"COMMENT ON TABLE credentials IS '{_CREDS_TABLE_COMMENT}'"))
    op.execute(sa.text(f"COMMENT ON TABLE image_channels IS '{_CHANNELS_TABLE_COMMENT}'"))


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(sa.text("COMMENT ON TABLE credentials IS "
                           "'外部密钥仓库：明文存储（用户要求可读回），只在 reveal 接口出网'"))
        _swap_check("credentials", "kind", _KINDS_NEW, _KINDS_OLD,
                    "类别：ssh_key/ssh_password/aliyun_ak/frp_panel/ng_panel"
                    "/operator_key/site/llm_key/image_key")
    op.drop_index("ix_image_channels_artifact_id", table_name="image_channels")
    op.drop_table("image_channels")
