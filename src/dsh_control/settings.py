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

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 少数配置项的 env 名不是「DSHC_ + 字段名大写」（lead 定的对外名），显式登记：
# 字段 -> 环境变量首选名。docs/SITE-CONFIG.md 与此保持一致。
ENV_NAMES: dict[str, str] = {"dsh_image": "DSHC_IMAGE"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSHC_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", populate_by_name=True
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
    # NG 回源地址（frps 监听地址，NG 容器视角可达的那一个）。
    # 不能由 ingress_ip 推：云上无 NAT 回环，公网入口 IP 在入口机上自己都出不去。
    frps_upstream_host: str = ""
    frp_port_start: int = 20000        # DSHC_FRP_PORT_START：公网端口搜索起点
    prefer_hdd: bool = True           # DSHC_PREFER_HDD：自动选主机时机械盘优先

    # ---- 主控台自身公网入口（cli expose 用；console_host 留空=hosts 恰好一台时自动选）
    console_host: str = ""            # DSHC_CONSOLE_HOST：主控台所在主机（name 或 ssh_host）
    wildcard_cert_name: str = ""      # DSHC_WILDCARD_CERT_NAME：NG 建站用的现成通配证书名

    # ---- 模型（kit 的 compose 里 LLM_BASE_URL / LLM_API_KEY 是必填，缺了容器拒启）
    # 地址是站点事实，放这里；密钥是机密，放 credentials(kind=llm_key)。
    llm_base_url: str = ""            # DSHC_LLM_BASE_URL：自部署 provider 的 /v1 地址
    llm_model_id: str = ""            # DSHC_LLM_MODEL_ID：留空则用 kit 默认模型

    # ---- 生图（DSH 没有原生生图入口，靠技能调 ComfyUI；地址在这，token 在 credentials）
    image_api_url: str = ""           # DSHC_IMAGE_API_URL：门卫地址（内网优先）
    image_backup_url: str = ""        # DSHC_IMAGE_BACKUP_URL：备用入口，主入口不通时自动换
    team_max_members: int = 4         # DSHC_TEAM_MAX_MEMBERS：智能体团队人数上限

    # ---- 字体（开通第 67 步「铺字体」的源；站点配置，不是凭据，不进 credentials）
    # 站点字体库目录：实例宿主机上的绝对路径，开通时 cp -n 进实例 DATA_DIR 的
    # home/.fonts（Word→PDF 两条转换路共同认的投递口）。留空 = 不铺，步骤标 skipped。
    font_library: str = ""            # DSHC_FONT_LIBRARY：站点字体库目录（绝对路径）

    # ---- 镜像（契约 §6.1.2：镜像是「准备好的版本」，开通阶段永不构建）
    # 默认部署镜像标签（准备好的版本，如 registry.local/dsh:stable）
    dsh_image: str = Field(default="",
                           validation_alias=AliasChoices("DSHC_IMAGE", "DSHC_DSH_IMAGE",
                                                         "dsh_image"))

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
    # released 保留天数（契约 §3/§6.4）：到期 watcher 自动入队 purge。
    # 库里的 site 行优先（site.toml [site].purge_after_days 落库，watcher 每轮现读），
    # 这里只是 env 兜底默认值。
    purge_after_days: int = 15           # DSHC_PURGE_AFTER_DAYS

    dry_run: bool = False

    def require(self, field: str) -> str:
        """站点事实缺失时的统一报错，不带着空值往下走。"""
        value = getattr(self, field, "")
        if not value:
            env_name = ENV_NAMES.get(field, f"DSHC_{field.upper()}")
            raise RuntimeError(
                f"缺少站点配置 {field}：先 cli seed --site deploy/site.toml，"
                f"或设置环境变量 {env_name}"
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
        "frps_upstream_host": "frps_upstream_host",
        "dsh_image": "dsh_image", "console_host": "console_host",
        "wildcard_cert_name": "wildcard_cert_name",
        "llm_base_url": "llm_base_url", "llm_model_id": "llm_model_id",
        "image_api_url": "image_api_url", "image_backup_url": "image_backup_url",
        "team_max_members": "team_max_members", "font_library": "font_library",
        "frp_port_start": "frp_port_start", "prefer_hdd": "prefer_hdd",
        "purge_after_days": "purge_after_days",
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
