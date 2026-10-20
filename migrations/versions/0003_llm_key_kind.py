"""credentials.kind 增加 llm_key：站点级模型密钥。

kit 的 compose 里 LLM_API_KEY 是必填（`${LLM_API_KEY:?缺少 LLM_API_KEY}`），
开通时必须把密钥写进实例 .env，否则容器直接拒启（2026-10-21 现网第 60 步实测）。
密钥属于机密，按 §2.0 放 credentials，不放站点 TOML。

规矩同 0001/0002：无外键、无联表、每列中文 comment、text+CHECK 不用 PG enum、
迁移里零 INSERT。SQLite 无法就地改 CHECK，测试库每次 create_all 重建，模型已是
新集合；这里只改 PG。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_OLD = ("ssh_key", "ssh_password", "aliyun_ak", "frp_panel",
              "ng_panel", "operator_key", "site")
_KINDS_NEW = _KINDS_OLD + ("llm_key",)
_COMMENT = ("类别：ssh_key/ssh_password/aliyun_ak/frp_panel/ng_panel"
            "/operator_key/site/llm_key")


def _swap(old: tuple[str, ...], new: tuple[str, ...]) -> None:
    op.drop_constraint("credentials_kind_check", "credentials", type_="check")
    quoted = ", ".join(f"'{v}'" for v in new)
    op.create_check_constraint("credentials_kind_check", "credentials",
                               sa.text(f"kind IN ({quoted})"))
    if old != new:
        op.execute(sa.text(f"COMMENT ON COLUMN credentials.kind IS '{_COMMENT}'"))


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    _swap(_KINDS_OLD, _KINDS_NEW)


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    _swap(_KINDS_NEW, _KINDS_OLD)
