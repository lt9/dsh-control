"""契约 §2 的表结构 + §2.0 三条规矩：

1. 不用外键——表间只有逻辑关联列 + 普通索引，级联行为在程序里显式写；
2. 不写联表查询在仓储层（见 repo.py），这里只提供单表模型；
3. 每个字段带中文 comment，表也带 comment，psql \\d+ 可见。

枚举一律 `text` + CHECK 约束（不用 PG enum，加值要改类型太脆）；同一份模型要在
PostgreSQL（生产）与 aiosqlite（单测）两边跑。JSON 列 PG 用 JSONB、sqlite 退化为 JSON。
时间列一律 timestamptz 存 UTC，显示层转 Asia/Shanghai。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB, "postgresql")
# PG 上 BIGINT 自增需要 BIGSERIAL；sqlite INTEGER PRIMARY KEY 即自增
BigIntPk = BigInteger().with_variant(Integer, "sqlite")

INSTANCE_STATES = ("pending", "deploying", "running", "unhealthy", "restarting",
                   "released", "error")
DESIRED_STATES = ("running", "released")
JOB_KINDS = ("deploy", "restart", "release")
JOB_STATES = ("pending", "running", "succeeded", "failed")
STEP_STATES = ("pending", "running", "succeeded", "failed", "skipped")
TOKEN_SOURCES = ("bootstrap", "logs", "restart", "manual")
CREDENTIAL_KINDS = ("ssh_key", "ssh_password", "aliyun_ak", "frp_panel",
                    "ng_panel", "operator_key", "site")


def _in(col: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{col} IN ({quoted})")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Credential(Base):
    """外部密钥仓库：按用户要求明文存库、可读回；只在 reveal 接口出网。"""

    __tablename__ = "credentials"
    __table_args__ = (
        _in("kind", CREDENTIAL_KINDS),
        {"comment": "外部密钥仓库：明文存储（用户要求可读回），只在 reveal 接口出网"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    kind: Mapped[str] = mapped_column(
        String(32),
        comment="类别：ssh_key/ssh_password/aliyun_ak/frp_panel/ng_panel/operator_key/site")
    name: Mapped[str] = mapped_column(String(128), unique=True,
                                      comment="唯一名称，如 console-ed25519")
    secret: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict,
                                                   comment="密钥内容 JSON（明文）")
    note: Mapped[str] = mapped_column(Text, default="", comment="备注")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, comment="更新时间（UTC）")


class Host(Base):
    __tablename__ = "hosts"
    __table_args__ = ({"comment": "被管主机：SSH + docker，dsh-family kit 所在"},)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    name: Mapped[str] = mapped_column(String(128), unique=True,
                                      comment="主机名，如 nas-1")
    ssh_host: Mapped[str] = mapped_column(String(255), comment="SSH 地址")
    ssh_port: Mapped[int] = mapped_column(Integer, default=22, comment="SSH 端口（NAS 只有 2333）")
    ssh_user: Mapped[str] = mapped_column(String(128), comment="SSH 用户（各主机可不同）")
    ssh_credential_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="逻辑指向 credentials.id（ssh_key），无外键")
    docker_context: Mapped[str] = mapped_column(String(128), default="default",
                                                comment="docker context 名")
    kit_path: Mapped[str] = mapped_column(String(512), default="",
                                          comment="dsh-family 仓库在被管机上的路径")
    data_root: Mapped[str] = mapped_column(String(512), default="",
                                           comment="实例数据根目录（必须落在机械盘）")
    frpc_client_id: Mapped[str] = mapped_column(String(255), default="",
                                                comment="frp-panel 里的客户端 id")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否参与新实例分配")
    note: Mapped[str] = mapped_column(Text, default="", comment="备注")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, comment="更新时间（UTC）")


class Instance(Base):
    __tablename__ = "instances"
    __table_args__ = (
        _in("state", INSTANCE_STATES),
        _in("desired_state", DESIRED_STATES),
        {"comment": "DSH 实例：一个容器 = 一个人；slug 同时是释放二次确认词"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    slug: Mapped[str] = mapped_column(
        String(64), unique=True, comment="域名标签 dsh-NN，服务端生成不可改，也是释放确认词")
    owner_label: Mapped[str] = mapped_column(String(255), default="",
                                             comment="给谁用的（事后可改的备注名）")
    note: Mapped[str] = mapped_column(Text, default="", comment="备注（PATCH 可改）")
    host_id: Mapped[int] = mapped_column(BigInteger, index=True,
                                         comment="逻辑指向 hosts.id，无外键")
    container_name: Mapped[str] = mapped_column(String(128), default="",
                                                comment="容器名（= slug）")
    config_version: Mapped[str] = mapped_column(String(32), default="",
                                                comment="部署时使用的配置版本，如 v1")
    image_tag: Mapped[str] = mapped_column(String(255), default="", comment="镜像标签")
    dsh_version: Mapped[str] = mapped_column(String(64), default="", comment="DSH 版本号")
    dns_name: Mapped[str] = mapped_column(String(255), default="", comment="完整域名 dsh-NN.zone")
    dns_record_id: Mapped[str] = mapped_column(String(64), default="",
                                               comment="阿里云解析 RecordId（释放按它精确删）")
    frp_proxy_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="frp-panel proxy_config id（释放按它精确删）")
    frp_remote_port: Mapped[int | None] = mapped_column(
        Integer, comment="frps 公网端口（释放后保留，不复用）")
    ng_upstream_name: Mapped[str] = mapped_column(String(128), default="",
                                                  comment="nginxWebUI upstream 名 DSH_NN")
    ng_server_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="nginxWebUI 站点 id（释放按它精确删）")
    ng_location_id: Mapped[int | None] = mapped_column(BigInteger,
                                                      comment="nginxWebUI location id")
    public_url: Mapped[str] = mapped_column(String(512), default="", comment="公网访问地址")
    container_port: Mapped[int | None] = mapped_column(Integer, comment="容器发布端口")
    state: Mapped[str] = mapped_column(
        String(32), default="pending", index=True,
        comment="运行状态：pending/deploying/running/unhealthy/restarting/released/error")
    desired_state: Mapped[str] = mapped_column(String(32), default="running",
                                               comment="期望状态：running/released（终态）")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次错误（已脱敏）")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         comment="释放时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, comment="更新时间（UTC）")


class InstanceToken(Base):
    """登录密钥历史。详情页永远取最新一条（captured_at 最大，平局取 id 最大）。"""

    __tablename__ = "instance_tokens"
    __table_args__ = (
        _in("source", TOKEN_SOURCES),
        Index("ix_instance_tokens_instance_captured", "instance_id", "captured_at"),
        {"comment": "登录密钥历史：详情 access 永远取最新一条；token 只在 reveal 接口出网"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    instance_id: Mapped[int] = mapped_column(BigInteger, index=True,
                                             comment="逻辑指向 instances.id，无外键")
    token: Mapped[str] = mapped_column(Text, comment="登录密钥明文（reveal 才出网）")
    url: Mapped[str] = mapped_column(Text, default="", comment="一键登录完整 URL")
    source: Mapped[str] = mapped_column(String(16), default="logs",
                                        comment="来源：bootstrap/logs/restart/manual")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                  comment="抓取时间（UTC）")


class InstanceConfig(Base):
    __tablename__ = "instance_configs"
    __table_args__ = (
        UniqueConstraint("instance_id", "version"),
        {"comment": "部署时实际下发的配置快照，详情配置弹窗数据源"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    instance_id: Mapped[int] = mapped_column(BigInteger, index=True,
                                             comment="逻辑指向 instances.id，无外键")
    version: Mapped[str] = mapped_column(String(32), comment="配置版本，如 v1")
    rendered: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict,
                                                     comment="实际下发的键值快照")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 comment="创建时间（UTC）")


class InstanceSnapshot(Base):
    __tablename__ = "instance_snapshots"
    __table_args__ = (
        Index("ix_instance_snapshots_instance_captured", "instance_id", "captured_at"),
        {"comment": "watcher 周期采集的容器运行态"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    instance_id: Mapped[int] = mapped_column(BigInteger, index=True,
                                             comment="逻辑指向 instances.id，无外键")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                  comment="采集时间（UTC）")
    docker_state: Mapped[str] = mapped_column(String(32), default="",
                                              comment="docker 状态 running/exited/absent…")
    health: Mapped[str] = mapped_column(String(16), default="none",
                                        comment="健康：healthy/unhealthy/starting/none")
    restart_count: Mapped[int] = mapped_column(Integer, default=0,
                                               comment="docker RestartCount（自动拉起检测）")
    cpu_pct: Mapped[float | None] = mapped_column(Float, comment="CPU 百分比")
    mem_usage_bytes: Mapped[int | None] = mapped_column(BigInteger, comment="内存使用字节")
    mem_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, comment="内存上限字节")
    raw: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict,
                                                comment="原始字段（image_id、ip 等，已脱敏）")


class DeployJob(Base):
    __tablename__ = "deploy_jobs"
    __table_args__ = (
        _in("kind", JOB_KINDS),
        _in("state", JOB_STATES),
        {"comment": "一次性任务：deploy/restart/release 状态机"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    instance_id: Mapped[int] = mapped_column(BigInteger, index=True,
                                             comment="逻辑指向 instances.id，无外键")
    kind: Mapped[str] = mapped_column(String(16), comment="任务类型：deploy/restart/release")
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True,
                                       comment="任务状态：pending/running/succeeded/failed")
    error: Mapped[str | None] = mapped_column(Text, comment="失败原因（已脱敏）")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        comment="开始时间（UTC）")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         comment="结束时间（UTC）")


class DeployStep(Base):
    """点对点进度，前端按 seq 渲染。"""

    __tablename__ = "deploy_steps"
    __table_args__ = (
        UniqueConstraint("job_id", "seq"),
        _in("state", STEP_STATES),
        {"comment": "任务步骤：前端按 seq 逐条点亮"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    job_id: Mapped[int] = mapped_column(BigInteger, index=True,
                                        comment="逻辑指向 deploy_jobs.id，无外键")
    seq: Mapped[int] = mapped_column(Integer, comment="步骤序号（契约固定）")
    key: Mapped[str] = mapped_column(String(64), comment="步骤键，如 dns_create")
    title: Mapped[str] = mapped_column(String(255), default="", comment="中文标题")
    state: Mapped[str] = mapped_column(String(16), default="pending",
                                       comment="pending/running/succeeded/failed/skipped")
    detail: Mapped[str] = mapped_column(Text, default="",
                                        comment="命令、耗时、stdout 摘要（已脱敏）")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        comment="开始时间（UTC）")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         comment="结束时间（UTC）")


class DeployLog(Base):
    """步骤日志：契约 §4 定义了 `GET /jobs/{id}/logs?after=` 增量接口但 §2 未给表——
    这里是最小补角。行内文本入库前必须已过 redact_text（无 token/AK/SK 明文）。"""

    __tablename__ = "deploy_logs"
    __table_args__ = (
        Index("ix_deploy_logs_job_id", "job_id"),
        {"comment": "步骤日志行：增量拉取（after=游标）与 SSE 的数据源"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True,
                                    comment="主键，即日志游标（after= 参数）")
    job_id: Mapped[int] = mapped_column(BigInteger,
                                        comment="逻辑指向 deploy_jobs.id，无外键")
    seq: Mapped[int] = mapped_column(Integer, default=0, comment="所属步骤 seq")
    level: Mapped[str] = mapped_column(String(8), default="stdout",
                                       comment="stdout/stderr/info/error")
    line: Mapped[str] = mapped_column(Text, comment="日志行（已脱敏）")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                         comment="落库时间（UTC）")


class Event(Base):
    """审计与自动重启记录。data 入库前必须已过 integrations.base.redact()。"""

    __tablename__ = "events"
    __table_args__ = ({"comment": "审计与自动重启记录；删除对象的 id 与名字都记在这里"},)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    kind: Mapped[str] = mapped_column(String(64), index=True,
                                      comment="事件类型，如 auto_restart/release_dns_deleted")
    instance_id: Mapped[int | None] = mapped_column(BigInteger, index=True,
                                                    comment="逻辑指向 instances.id，无外键")
    job_id: Mapped[int | None] = mapped_column(BigInteger, index=True,
                                               comment="逻辑指向 deploy_jobs.id，无外键")
    message: Mapped[str] = mapped_column(Text, default="", comment="人话描述（已脱敏）")
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict,
                                                 comment="结构化附加信息（redact 后）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 comment="发生时间（UTC）")
