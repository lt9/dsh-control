"""全部环境配置，前缀 DSHC_。契约 §8：代码里不允许出现写死的机器地址，
这里给出的默认值与 ARCHITECTURE.md §8 一致，可被环境变量覆盖。"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSHC_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 必填
    database_url: str = "postgresql+asyncpg://tierai:tierai-local-only@172.19.0.6:5432/dsh_control"
    session_secret: str = ""

    # 监听
    listen_host: str = "0.0.0.0"
    listen_port: int = 13300
    session_days: int = 7

    # 公网入口与 DNS
    ingress_ip: str = "8.134.168.14"
    dns_zone: str = "pipboy.cn"
    dns_prefix: str = "dsh-"                  # §6.1.1：slug = dsh-NN
    public_base: str = "https://dshctl.pipboy.cn"
    # NG 容器里访问 frps 的地址（dshsub1 实测后端 172.19.4.1:4892 可用）。
    # 不是被管机地址，是 VPS 上 frps 容器在 NG 所在 docker 网络里的 IP。
    frp_internal_ip: str = "172.19.4.1"

    # watcher / 状态机
    backup_window: str = "03:20-04:00"
    restart_cooldown: int = 600          # 秒
    poll_interval: int = 30              # 秒
    unhealthy_threshold: int = 3         # 连续失败次数（契约 §3 固定为 3）
    wait_healthy_seconds: int = 120      # 重启后等待健康上限

    # frp
    frp_panel_url: str = "http://8.134.168.14:9000"
    frp_panel_user: str = ""
    frp_panel_pass: str = ""
    frp_port_start: int = 4893         # DSHC_FRP_PORT_START：从此起找第一个未占用端口
    prefer_hdd: bool = True            # DSHC_PREFER_HDD：自动选主机时机械盘优先

    # nginxWebUI
    ng_url: str = "http://8.134.168.14:18180"
    ng_user: str = ""
    ng_pass: str = ""

    # 阿里云解析
    aliyun_ak: str = ""
    aliyun_sk: str = ""

    # SSH 私钥（init 时读进 credentials 的 console-ed25519，之后从库取；两台共用）
    ssh_key_path_nas8: str = ""
    ssh_key_path_host109: str = ""

    # 种子主机：照 docs/SEED.md §1 逐字（实例种子为空）
    seed_hosts_json: str = Field(
        default=json.dumps(
            [
                {
                    "name": "nas-8",
                    "ssh_host": "192.168.3.8",
                    "ssh_port": 2333,
                    "ssh_user": "tuan",
                    "ssh_credential": "console-ed25519",
                    "docker_context": "default",
                    "kit_path": "/home/tuan/dsh-family",
                    "data_root": "/mnt/hdd2/dsh-family",
                    "frpc_client_id": "tuan.c.TuanCloud@1",
                    "note": "机械盘 sdb1 剩 1.7T；系统盘 sda2 不许放实例数据",
                },
                {
                    "name": "host-109",
                    "ssh_host": "192.168.3.109",
                    "ssh_port": 22,
                    "ssh_user": "twan",
                    "ssh_credential": "console-ed25519",
                    "docker_context": "default",
                    "kit_path": "/home/twan/src/dsh-family",
                    "data_root": "/home/twan/dsh-data",
                    "frpc_client_id": "tuan.c.TuanCloud@1",
                    "note": "主控台自己这台",
                },
            ],
            ensure_ascii=False,
        )
    )

    dry_run: bool = False

    tz: str = "Asia/Shanghai"

    def seed_hosts(self) -> list[dict[str, Any]]:
        return json.loads(self.seed_hosts_json or "[]")


@lru_cache
def get_settings() -> Settings:
    return Settings()
