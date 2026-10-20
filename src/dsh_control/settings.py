"""环境配置（前缀 DSHC_）。

站点零专属（docs/SITE-CONFIG.md）：这里不出现任何站点事实——域名、入口 IP、面板
地址、主机、路径、frpc 客户端 id 一律来自环境变量、`cli seed --site` 写进库的
`credentials(kind=site)` 行，或 `hosts` 表。默认值只有两类：与站点无关的通用常量
（时区、冷却秒数、端口起点这类）与空串（必填项）。

`apply_site_overrides` 让运行期以库里的 site 行为准（seed --site 灌入），
环境变量作为兜底。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSHC_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 必填（无站点默认值）
    database_url: str = ""
    session_secret: str = ""

    # ---- 监听（通用）
    listen_host: str = "0.0.0.0"
    listen_port: int = 13300
    session_days: int = 7
    tz: str = "Asia/Shanghai"

    # ---- 站点事实（site.toml 灌库后由 apply_site_overrides 覆盖；env 兜底；默认全空）
    public_base: str = ""
    ingress_ip: str = ""
    dns_zone: str = ""
    dns_prefix: str = "dsh-"          # §6.1.1 一级子域前缀（通用规则的一部分）
    frp_port_start: int = 4893        # DSHC_FRP_PORT_START：公网端口搜索起点
    prefer_hdd: bool = True           # DSHC_PREFER_HDD：自动选主机时机械盘优先

    # ---- provider 注册表选择（site.toml [providers]；noop = 该层人工做，步骤标 skipped）
    provider_dns: str = "noop"        # aliyun | noop
    provider_frp: str = "noop"        # frp_panel | noop
    provider_ng: str = "noop"         # nginxwebui | noop

    # ---- 外部面板凭据 env 兜底（正式来源是 seed 写进 credentials 的对应 kind 行）
    frp_panel_url: str = ""
    frp_panel_user: str = ""
    frp_panel_pass: str = ""
    ng_url: str = ""
    ng_user: str = ""
    ng_pass: str = ""
    aliyun_ak: str = ""
    aliyun_sk: str = ""

    # ---- watcher / 状态机（通用）
    backup_window: str = "03:20-04:00"
    restart_cooldown: int = 600          # 秒
    poll_interval: int = 30              # 秒
    unhealthy_threshold: int = 3         # 连续失败次数（契约 §3 固定 3）
    wait_healthy_seconds: int = 120      # 重启后等待健康上限

    dry_run: bool = False

    def require(self, field: str) -> str:
        """站点事实缺失时的统一报错，不带着空值往下走。"""
        value = getattr(self, field, "")
        if not value:
            raise RuntimeError(
                f"缺少站点配置 {field}：先 cli seed --site deploy/site.toml，"
                f"或设置环境变量 DSHC_{field.upper()}"
            )
        return str(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def apply_site_overrides(settings: Settings, site: dict[str, Any] | None) -> Settings:
    """把 credentials(kind=site).secret 里的 [site]+[providers] 段覆盖进 settings。"""
    if not site:
        return settings
    mapping = {
        "public_base": "public_base", "ingress_ip": "ingress_ip",
        "dns_zone": "dns_zone", "domain_prefix": "dns_prefix",
        "frp_port_start": "frp_port_start", "prefer_hdd": "prefer_hdd",
    }
    for src, dst in mapping.items():
        if site.get(src) not in (None, ""):
            setattr(settings, dst, site[src])
    providers = site.get("providers") or {}
    for name, dst in (("dns", "provider_dns"), ("frp", "provider_frp"),
                      ("ng", "provider_ng")):
        if providers.get(name):
            setattr(settings, dst, providers[name])
    return settings
