"""初始建表：零站点数据，只有 DDL（docs/SITE-CONFIG.md：迁移里不许有 INSERT）。

规矩（ARCHITECTURE §2.0）：无外键；每列中文 comment；状态列 text + CHECK；
时间列 timestamptz 存 UTC；字节 bigint。

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JsonType = sa.JSON().with_variant(postgresql.JSONB, "postgresql")
BigIntPk = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def _in(col: str, values: list[str]) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} IN ({', '.join(repr(v) for v in values)})")


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("kind", sa.String(32), nullable=False,
                  comment="类别：ssh_key/ssh_password/aliyun_ak/frp_panel/ng_panel/operator_key/site"),
        sa.Column("name", sa.String(128), nullable=False, comment="唯一名称，如 console-ed25519"),
        sa.Column("secret", JsonType, nullable=False, comment="密钥内容 JSON（明文）"),
        sa.Column("note", sa.Text, nullable=False, comment="备注"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间（UTC）"),
        sa.UniqueConstraint("name"),
        _in("kind", ["ssh_key", "ssh_password", "aliyun_ak", "frp_panel",
                     "ng_panel", "operator_key", "site"]),
        comment="外部密钥仓库：明文存储（用户要求可读回），只在 reveal 接口出网",
    )

    op.create_table(
        "hosts",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("name", sa.String(128), nullable=False, comment="主机名，如 nas-1"),
        sa.Column("ssh_host", sa.String(255), nullable=False, comment="SSH 地址"),
        sa.Column("ssh_port", sa.Integer, nullable=False, comment="SSH 端口（NAS 只有 2333）"),
        sa.Column("ssh_user", sa.String(128), nullable=False, comment="SSH 用户（各主机可不同）"),
        sa.Column("ssh_credential_id", sa.BigInteger, nullable=True,
                  comment="逻辑指向 credentials.id（ssh_key），无外键"),
        sa.Column("docker_context", sa.String(128), nullable=False, comment="docker context 名"),
        sa.Column("kit_path", sa.String(512), nullable=False, comment="dsh-family 仓库在被管机上的路径"),
        sa.Column("data_root", sa.String(512), nullable=False, comment="实例数据根目录（必须落在机械盘）"),
        sa.Column("frpc_client_id", sa.String(255), nullable=False, comment="frp-panel 里的客户端 id"),
        sa.Column("enabled", sa.Boolean, nullable=False, comment="是否参与新实例分配"),
        sa.Column("note", sa.Text, nullable=False, comment="备注"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间（UTC）"),
        sa.UniqueConstraint("name"),
        comment="被管主机：SSH + docker，dsh-family kit 所在",
    )

    op.create_table(
        "instances",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("slug", sa.String(64), nullable=False,
                  comment="域名标签 dsh-NN，服务端生成不可改，也是释放确认词"),
        sa.Column("owner_label", sa.String(255), nullable=False, comment="给谁用的（事后可改的备注名）"),
        sa.Column("note", sa.Text, nullable=False, comment="备注（PATCH 可改）"),
        sa.Column("host_id", sa.BigInteger, nullable=False, comment="逻辑指向 hosts.id，无外键"),
        sa.Column("container_name", sa.String(128), nullable=False, comment="容器名（= slug）"),
        sa.Column("config_version", sa.String(32), nullable=False, comment="部署时使用的配置版本，如 v1"),
        sa.Column("image_tag", sa.String(255), nullable=False, comment="镜像标签"),
        sa.Column("dsh_version", sa.String(64), nullable=False, comment="DSH 版本号"),
        sa.Column("dns_name", sa.String(255), nullable=False, comment="完整域名 dsh-NN.zone"),
        sa.Column("dns_record_id", sa.String(64), nullable=False, comment="阿里云解析 RecordId（释放按它精确删）"),
        sa.Column("frp_proxy_id", sa.BigInteger, nullable=True, comment="frp-panel proxy_config id（释放按它精确删）"),
        sa.Column("frp_remote_port", sa.Integer, nullable=True, comment="frps 公网端口（释放后保留，不复用）"),
        sa.Column("ng_upstream_name", sa.String(128), nullable=False, comment="nginxWebUI upstream 名 DSH_NN"),
        sa.Column("ng_server_id", sa.BigInteger, nullable=True, comment="nginxWebUI 站点 id（释放按它精确删）"),
        sa.Column("ng_location_id", sa.BigInteger, nullable=True, comment="nginxWebUI location id"),
        sa.Column("public_url", sa.String(512), nullable=False, comment="公网访问地址"),
        sa.Column("container_port", sa.Integer, nullable=True, comment="容器发布端口"),
        sa.Column("state", sa.String(32), nullable=False,
                  comment="运行状态：pending/deploying/running/unhealthy/restarting/released/error"),
        sa.Column("desired_state", sa.String(32), nullable=False, comment="期望状态：running/released（终态）"),
        sa.Column("last_error", sa.Text, nullable=True, comment="最近一次错误（已脱敏）"),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True, comment="释放时间（UTC）"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间（UTC）"),
        sa.UniqueConstraint("slug"),
        _in("state", ["pending", "deploying", "running", "unhealthy", "restarting",
                      "released", "error"]),
        _in("desired_state", ["running", "released"]),
        comment="DSH 实例：一个容器 = 一个人；slug 同时是释放二次确认词",
    )
    op.create_index("ix_instances_state", "instances", ["state"])
    op.create_index("ix_instances_host_id", "instances", ["host_id"])

    op.create_table(
        "instance_tokens",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("instance_id", sa.BigInteger, nullable=False, comment="逻辑指向 instances.id，无外键"),
        sa.Column("token", sa.Text, nullable=False, comment="登录密钥明文（reveal 才出网）"),
        sa.Column("url", sa.Text, nullable=False, comment="一键登录完整 URL"),
        sa.Column("source", sa.String(16), nullable=False, comment="来源：bootstrap/logs/restart/manual"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, comment="抓取时间（UTC）"),
        _in("source", ["bootstrap", "logs", "restart", "manual"]),
        comment="登录密钥历史：详情 access 永远取最新一条；token 只在 reveal 接口出网",
    )
    op.create_index("ix_instance_tokens_instance_id", "instance_tokens", ["instance_id"])
    op.create_index("ix_instance_tokens_instance_captured", "instance_tokens",
                    ["instance_id", "captured_at"])

    op.create_table(
        "instance_configs",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("instance_id", sa.BigInteger, nullable=False, comment="逻辑指向 instances.id，无外键"),
        sa.Column("version", sa.String(32), nullable=False, comment="配置版本，如 v1"),
        sa.Column("rendered", JsonType, nullable=False, comment="实际下发的键值快照"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间（UTC）"),
        sa.UniqueConstraint("instance_id", "version"),
        comment="部署时实际下发的配置快照，详情配置弹窗数据源",
    )
    op.create_index("ix_instance_configs_instance_id", "instance_configs", ["instance_id"])

    op.create_table(
        "instance_snapshots",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("instance_id", sa.BigInteger, nullable=False, comment="逻辑指向 instances.id，无外键"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, comment="采集时间（UTC）"),
        sa.Column("docker_state", sa.String(32), nullable=False, comment="docker 状态 running/exited/absent…"),
        sa.Column("health", sa.String(16), nullable=False, comment="健康：healthy/unhealthy/starting/none"),
        sa.Column("restart_count", sa.Integer, nullable=False, comment="docker RestartCount（自动拉起检测）"),
        sa.Column("cpu_pct", sa.Float, nullable=True, comment="CPU 百分比"),
        sa.Column("mem_usage_bytes", sa.BigInteger, nullable=True, comment="内存使用字节"),
        sa.Column("mem_limit_bytes", sa.BigInteger, nullable=True, comment="内存上限字节"),
        sa.Column("raw", JsonType, nullable=False, comment="原始字段（image_id、ip 等，已脱敏）"),
        comment="watcher 周期采集的容器运行态",
    )
    op.create_index("ix_instance_snapshots_instance_id", "instance_snapshots", ["instance_id"])
    op.create_index("ix_instance_snapshots_instance_captured", "instance_snapshots",
                    ["instance_id", "captured_at"])

    op.create_table(
        "deploy_jobs",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("instance_id", sa.BigInteger, nullable=False, comment="逻辑指向 instances.id，无外键"),
        sa.Column("kind", sa.String(16), nullable=False, comment="任务类型：deploy/restart/release"),
        sa.Column("state", sa.String(16), nullable=False, comment="任务状态：pending/running/succeeded/failed"),
        sa.Column("error", sa.Text, nullable=True, comment="失败原因（已脱敏）"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True, comment="开始时间（UTC）"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间（UTC）"),
        _in("kind", ["deploy", "restart", "release"]),
        _in("state", ["pending", "running", "succeeded", "failed"]),
        comment="一次性任务：deploy/restart/release 状态机",
    )
    op.create_index("ix_deploy_jobs_instance_id", "deploy_jobs", ["instance_id"])
    op.create_index("ix_deploy_jobs_state", "deploy_jobs", ["state"])

    op.create_table(
        "deploy_steps",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("job_id", sa.BigInteger, nullable=False, comment="逻辑指向 deploy_jobs.id，无外键"),
        sa.Column("seq", sa.Integer, nullable=False, comment="步骤序号（契约固定）"),
        sa.Column("key", sa.String(64), nullable=False, comment="步骤键，如 dns_create"),
        sa.Column("title", sa.String(255), nullable=False, comment="中文标题"),
        sa.Column("state", sa.String(16), nullable=False,
                  comment="pending/running/succeeded/failed/skipped"),
        sa.Column("detail", sa.Text, nullable=False, comment="命令、耗时、stdout 摘要（已脱敏）"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True, comment="开始时间（UTC）"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间（UTC）"),
        sa.UniqueConstraint("job_id", "seq"),
        _in("state", ["pending", "running", "succeeded", "failed", "skipped"]),
        comment="任务步骤：前端按 seq 逐条点亮",
    )
    op.create_index("ix_deploy_steps_job_id", "deploy_steps", ["job_id"])

    op.create_table(
        "deploy_logs",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键，即日志游标（after= 参数）"),
        sa.Column("job_id", sa.BigInteger, nullable=False, comment="逻辑指向 deploy_jobs.id，无外键"),
        sa.Column("seq", sa.Integer, nullable=False, comment="所属步骤 seq"),
        sa.Column("level", sa.String(8), nullable=False, comment="stdout/stderr/info/error"),
        sa.Column("line", sa.Text, nullable=False, comment="日志行（已脱敏）"),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, comment="落库时间（UTC）"),
        comment="步骤日志行：增量拉取（after=游标）与 SSE 的数据源",
    )
    op.create_index("ix_deploy_logs_job_id", "deploy_logs", ["job_id"])

    op.create_table(
        "events",
        sa.Column("id", BigIntPk, primary_key=True, comment="主键"),
        sa.Column("kind", sa.String(64), nullable=False,
                  comment="事件类型，如 auto_restart/release_dns_deleted"),
        sa.Column("instance_id", sa.BigInteger, nullable=True, comment="逻辑指向 instances.id，无外键"),
        sa.Column("job_id", sa.BigInteger, nullable=True, comment="逻辑指向 deploy_jobs.id，无外键"),
        sa.Column("message", sa.Text, nullable=False, comment="人话描述（已脱敏）"),
        sa.Column("data", JsonType, nullable=False, comment="结构化附加信息（redact 后）"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="发生时间（UTC）"),
        comment="审计与自动重启记录；删除对象的 id 与名字都记在这里",
    )
    op.create_index("ix_events_kind", "events", ["kind"])
    op.create_index("ix_events_instance_id", "events", ["instance_id"])
    op.create_index("ix_events_job_id", "events", ["job_id"])


def downgrade() -> None:
    for table in ("events", "deploy_logs", "deploy_steps", "deploy_jobs",
                  "instance_snapshots", "instance_configs", "instance_tokens",
                  "instances", "hosts", "credentials"):
        op.drop_table(table)
