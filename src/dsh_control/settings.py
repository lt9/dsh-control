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
    # 字体库来源主机（hosts.name 或 ssh_host，须唯一命中 enabled 主机）：实例宿主机上
    # 没有 font_library 目录时，67 步先从这台机器经**控制台内存中转**复制一份到同样的
    # 路径再铺（tar→控制台→目标，两台机器之间不需要任何互信，形态同 cli images ship）。
    # 留空 = 老行为：目标机没库目录就记 warning 跳过。
    font_library_source: str = ""     # DSHC_FONT_LIBRARY_SOURCE

    # ---- 记忆中枢（memory-hub，能力 kind=service；site.toml [memory] 段）
    # enabled 默认 false：站点没有 [memory] 段时，开通第 68 步标 skipped、
    # watcher 不探、purge 只保留——行为与引入本功能之前逐字节一致。
    memory_enabled: bool = False          # DSHC_MEMORY_ENABLED
    # Core 元数据面基址（控制台直连，如 http://192.0.2.9:8420）：建用户/verify/回收都打它
    memory_core_url: str = ""             # DSHC_MEMORY_CORE_URL
    # 实例模型出口前缀（Proxy，不带 /v1——proxy 的 anthropic 转发自己拼 /v1/messages）；
    # 实例实际 baseURL = proxy_url + "/" + space_id
    memory_proxy_url: str = ""            # DSHC_MEMORY_PROXY_URL
    memory_space_id: str = "default"      # DSHC_MEMORY_SPACE_ID（U-1 定案：全家一个 space）
    # 可选：业务用户建好后拉进这个 Team（Memory Hub 的组织结构，P0 现场是「家庭」team）
    memory_team_id: str = ""              # DSHC_MEMORY_TEAM_ID
    # 按主机覆盖出口前缀（{hosts.name 或 ssh_host: 前缀}）：不在记忆栈同网段的机器
    # 走自己的路（例：云上机器经 frp 隧道口）。命中顺序 name → ssh_host → 默认。
    memory_host_proxy: dict[str, str] = {}  # DSHC_MEMORY_HOST_PROXY（JSON 对象）
    # home 层补丁 models 段的模型 id：留空 = 用 llm_model_id（与 kit 现值对齐）
    memory_model_id: str = ""             # DSHC_MEMORY_MODEL_ID
    # Panel 同源反代壳的两个坐标（MEMORY-PANEL-EMBED ③-B；memory_proxy.py 用）：
    # 空串 = 用 memory_proxy 内置缺省（上游 http://192.168.3.109:8125、
    # dist /opt/dsh-panel/dist）；站点不同才在这显式覆盖
    memory_panel_base: str = ""           # DSHC_MEMORY_PANEL_BASE
    memory_panel_dist: str = ""           # DSHC_MEMORY_PANEL_DIST

    # ---- 镜像（契约 §6.1.2：镜像是「准备好的版本」，开通阶段永不构建）
    # 默认部署镜像标签（准备好的版本，如 registry.local/dsh:stable）
    dsh_image: str = Field(default="",
                           validation_alias=AliasChoices("DSHC_IMAGE", "DSHC_DSH_IMAGE",
                                                         "dsh_image"))
    # 镜像制品库目录（契约 §6.1.5）：publish 落盘、/dl 下载都只在这下面走。
    # 未挂载/不可写 = download 模式禁用（发布与搬运直接报错，不静默降级）。
    image_store: str = "/var/lib/dsh-control/images"   # DSHC_IMAGE_STORE

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
        "font_library_source": "font_library_source",
        "frp_port_start": "frp_port_start", "prefer_hdd": "prefer_hdd",
        "purge_after_days": "purge_after_days",
        # 记忆中枢也收 flat 写法（[site].memory_enabled=…）：seed_into 现在只镜像
        # [site]+[providers]，这条路径不依赖 cli.py 改动，站点可以先这么写。
        "memory_enabled": "memory_enabled", "memory_core_url": "memory_core_url",
        "memory_proxy_url": "memory_proxy_url", "memory_space_id": "memory_space_id",
        "memory_team_id": "memory_team_id", "memory_host_proxy": "memory_host_proxy",
        "memory_model_id": "memory_model_id",
        "memory_panel_base": "memory_panel_base", "memory_panel_dist": "memory_panel_dist",
    }
    for src, dst in mapping.items():
        if site.get(src) not in (None, ""):
            setattr(settings, dst, site[src])
    # [memory] 独立段（docs/SITE-CONFIG.md 的正写）：段内键不带 memory_ 前缀。
    # 段整体缺省 = 一个字段都不碰，enabled 停在 False——无 [memory] 的站点行为与
    # 引入本功能之前逐字节一致（enabled 的 False 也照写覆盖，段在就以段为准）。
    mem = site.get("memory")
    if isinstance(mem, dict):
        flat = {"enabled": "memory_enabled", "core_url": "memory_core_url",
                "proxy_url": "memory_proxy_url", "space_id": "memory_space_id",
                "team_id": "memory_team_id", "host_proxy": "memory_host_proxy",
                "model_id": "memory_model_id", "panel_base": "memory_panel_base",
                "panel_dist": "memory_panel_dist"}
        for src, dst in flat.items():
            if src in mem and mem[src] is not None:
                setattr(settings, dst, mem[src])
    providers = site.get("providers") or {}
    for name, dst in (("dns", "provider_dns"), ("frp", "provider_frp"),
                      ("ng", "provider_ng")):
        if providers.get(name):
            setattr(settings, dst, providers[name])
    return settings
