"""资产入库（SITE-CONFIG.md「资产分发」）：新表 kit_releases + fontlib_releases。

「镜像+配置+字体+脚本都在控制台、开通时统一下发」的落地：
- `kit_releases`：kit 脚本（scripts/** + compose.yaml + .env.example，约 100KB
  文本）整包进 JSONB——小文本资产进库的意义就在这：发布是事务、取用一条查询、
  没有文件生命周期要管。current 最多一行为真（发布时代码换行）。
- `fontlib_releases`：字体库（约 370MB 二进制）的**账本**——文件在制品库目录
  （image_store，和镜像制品同盘同生命周期），这行记 filename/sha256/账目。
  「行存在即文件已完整落库」沿用 image_artifacts（0008）的硬约定：文件写完、
  改好名，才插行。

规矩同 0001–0010：无外键、无联表、每列中文 comment、迁移里零 INSERT。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigIntPk = sa.BigInteger().with_variant(sa.Integer, "sqlite")
JsonT = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "kit_releases",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("version", sa.String(64), nullable=False, unique=True,
                  comment="版本号（发布时定，安全字符集 [A-Za-z0-9._-]）；"
                          "目标机 kit/.dshctl-kit-version 就是拿它比对"),
        sa.Column("files", JsonT, nullable=False,
                  comment="kit 全量文件：路径 → {b64: base64 正文, mode: 八进制权限}"
                          "；内容=scripts/**+compose.yaml+.env.example，不含 instances/"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False,
                  comment="解码后字节总数（发布时算，页面展示用）"),
        sa.Column("file_count", sa.Integer(), nullable=False,
                  comment="文件数（发布时算）"),
        sa.Column("note", sa.String(512), nullable=False, server_default="",
                  comment="发布说明（如 git sha、改了哪个脚本）"),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.false(),
                  comment="是否现行版：最多一行为真，发布时代码换行（先插后 demote 同事务）"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), comment="发布时间（UTC）"),
        comment="kit 脚本版本库：一行一个整包，文件正文就在库里（资产分发）",
    )
    op.create_table(
        "fontlib_releases",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("version", sa.String(64), nullable=False, unique=True,
                  comment="字体库版本号（安全字符集），如 fontlib-20261027-1430"),
        sa.Column("filename", sa.String(255), nullable=False, unique=True,
                  comment="制品库目录内的文件名（version.tar.gz）；行存在即文件已完整落库"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False,
                  comment="文件字节数（gzip 后）"),
        sa.Column("sha256", sa.String(64), nullable=False,
                  comment="文件 sha256（发布落盘时流式算好）"),
        sa.Column("file_count", sa.Integer(), nullable=False,
                  comment="库内字体文件数（发布时清点；97 款这种账目就靠它核对）"),
        sa.Column("origin", sa.String(128), nullable=False, server_default="",
                  comment="从哪来的：来源主机名或本机路径；空串=手工投放"),
        sa.Column("note", sa.String(512), nullable=False, server_default="",
                  comment="发布说明（收了哪些新字体、别名替换等）"),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.false(),
                  comment="是否现行版：最多一行为真；开通 67 步只认现行版"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), comment="发布时间（UTC）"),
        comment="字体库制品账本：文件在 image_store 目录，这里一行一个版本（资产分发）",
    )


def downgrade() -> None:
    op.drop_table("fontlib_releases")
    op.drop_table("kit_releases")
