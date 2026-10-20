"""契约 §2 的表结构。枚举一律用 String + Python 常量：同一份模型要在
PostgreSQL（生产）与 aiosqlite（单测）两边跑，不用原生 ENUM。
JSON 列在 PG 上用 JSONB，sqlite 上退化为 JSON。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB, "postgresql")
# PG 上 BIGINT 自增需要 BIGSERIAL；sqlite  INTEGER PRIMARY KEY 即自增
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Credential(Base):
    """外部密钥仓库：按用户要求明文存库、可读回；只在 reveal 接口出网。"""

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # ssh_key|ssh_password|aliyun_ak|frp_panel|ng_panel|operator_key
    name: Mapped[str] = mapped_column(String(128), unique=True)
    secret: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    ssh_host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(128))
    ssh_credential_id: Mapped[int | None] = mapped_column(ForeignKey("credentials.id"))
    docker_context: Mapped[str] = mapped_column(String(128), default="default")
    kit_path: Mapped[str] = mapped_column(String(512), default="")
    data_root: Mapped[str] = mapped_column(String(512), default="")
    frpc_client_id: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Instance(Base):
    """state: pending|deploying|running|unhealthy|restarting|released|error
    desired_state: running|released（released 是终态）"""

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    owner_label: Mapped[str] = mapped_column(String(255), default="")
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"))
    container_name: Mapped[str] = mapped_column(String(128), default="")
    config_version: Mapped[str] = mapped_column(String(32), default="")
    image_tag: Mapped[str] = mapped_column(String(255), default="")
    dsh_version: Mapped[str] = mapped_column(String(64), default="")
    dns_name: Mapped[str] = mapped_column(String(255), default="")
    dns_record_id: Mapped[str] = mapped_column(String(64), default="")
    frp_proxy_id: Mapped[int | None] = mapped_column(BigInteger)
    frp_remote_port: Mapped[int | None] = mapped_column(Integer)
    ng_upstream_name: Mapped[str] = mapped_column(String(128), default="")
    ng_server_id: Mapped[int | None] = mapped_column(BigInteger)
    ng_location_id: Mapped[int | None] = mapped_column(BigInteger)
    public_url: Mapped[str] = mapped_column(String(512), default="")
    container_port: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    desired_state: Mapped[str] = mapped_column(String(32), default="running")
    last_error: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class InstanceToken(Base):
    """登录密钥历史。详情页永远取最新一条（captured_at 最大，平局取 id 最大）。"""

    __tablename__ = "instance_tokens"
    __table_args__ = (Index("ix_instance_tokens_instance_captured", "instance_id", "captured_at"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    token: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="logs")  # bootstrap|logs|restart|manual
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InstanceConfig(Base):
    __tablename__ = "instance_configs"
    __table_args__ = (UniqueConstraint("instance_id", "version"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    rendered: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InstanceSnapshot(Base):
    __tablename__ = "instance_snapshots"
    __table_args__ = (
        Index("ix_instance_snapshots_instance_captured", "instance_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    docker_state: Mapped[str] = mapped_column(String(32), default="")
    health: Mapped[str] = mapped_column(String(16), default="none")
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    mem_usage_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mem_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class DeployJob(Base):
    """kind: deploy|release|restart；state: pending|running|succeeded|failed"""

    __tablename__ = "deploy_jobs"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeployStep(Base):
    """点对点进度，前端按 seq 渲染。state: pending|running|succeeded|failed|skipped"""

    __tablename__ = "deploy_steps"
    __table_args__ = (UniqueConstraint("job_id", "seq"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("deploy_jobs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(16), default="pending")
    detail: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeployLog(Base):
    """步骤日志，`GET /jobs/{id}/logs?after=` 的数据源。
    契约 §4 定义了日志接口但 §2 没给表——这是补的最小增量；
    行内文本入库前必须已过 redact_text（不含 token/AK/SK 明文）。"""

    __tablename__ = "deploy_logs"
    __table_args__ = (Index("ix_deploy_logs_job_id", "job_id"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("deploy_jobs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[str] = mapped_column(String(8), default="stdout")  # stdout|stderr|info|error
    line: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """审计与自动重启记录。data 入库前必须已过 integrations.base.redact()。"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_jobs.id"), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
