"""直连主机的两块云环境补丁（契约 §2/§6.1.4）：hosts 加 direct_backend / llm_base_url。

- `direct_backend`：direct 主机 NG 回源实际拨的地址；留空=ssh_host（§6.1.4 原口径，
  适用于 EIP 真绑在网卡上的普通公网机）。云轻量服务器（阿里云 SWAS 这类）的公网 IP
  是云端 NAT 映射，机器网卡上根本没有它：入口机恰好就是这台机时，NG 回源拨自家 EIP
  会出网卡绕云端 NAT 发卡拐（hairpin 黑洞，2026-10-22 现网 dsh-05 实测超时），必须
  登记网卡实址（登记网卡实址）。第 60 步同时把它写进实例 .env 的 HOST_BIND_IP：
  端口只绑这个地址，公网够不到，云防火墙一行都不用放行。
- `llm_base_url`：该机实例的模型入口覆盖。站点的 llm_base_url 是局域网网关地址，
  NAT 后家里的机器（frp 主机）拨得到，云上机器拨不到——第 60 步写了它就用它替换
  LLM_BASE_URL，密钥不变（同一个网关的两个门）。

两列都可空、无 CHECK，add_column 双方言通用；comment 只在 PG 补。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKEND_COMMENT = ("direct 主机 NG 回源实际拨的地址；空=ssh_host。云轻量服务器"
                    "（SWAS 等）公网 EIP 是云端 NAT 映射不在网卡上，入口机=本机时"
                    "回源拨 EIP 走云端 hairpin 黑洞，须登记网卡实址；同时第 60 步"
                    "写入实例 .env HOST_BIND_IP，端口只绑该址（§6.1.4）")
_LLM_COMMENT = ("该机实例的模型入口覆盖（值见 docs/site/SEED.md）：站点"
                "llm_base_url 是局域网网关，云上机器拨不到时用这里覆盖；空=用站点"
                "值。只换地址不换密钥（§2/§6.1.4）")


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column("direct_backend", sa.String(length=64), nullable=True,
                  comment=_BACKEND_COMMENT),
    )
    op.add_column(
        "hosts",
        sa.Column("llm_base_url", sa.String(length=255), nullable=True,
                  comment=_LLM_COMMENT),
    )
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(sa.text(f"COMMENT ON COLUMN hosts.direct_backend IS '{_BACKEND_COMMENT}'"))
    op.execute(sa.text(f"COMMENT ON COLUMN hosts.llm_base_url IS '{_LLM_COMMENT}'"))


def downgrade() -> None:
    op.drop_column("hosts", "llm_base_url")
    op.drop_column("hosts", "direct_backend")
