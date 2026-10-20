"""v1 配置版本定义与「？」弹窗文案（契约 §4：写在代码里的静态字典，首期不可编辑）。

`CONFIG_V1.items[]` 的每一项：key = 下发给 dsh-family 部署套件的配置键；
label/help = 详情弹窗逐条解释的文案来源；value 在渲染时从
instance_configs.rendered 里按 render_key 取（缺省按 key 取）。
"""

from __future__ import annotations

from typing import Any

CONFIG_VERSION = "v1"

_ITEMS: list[dict[str, Any]] = [
    {
        "key": "PUBLIC_URL",
        "label": "对外访问地址",
        "help": "实例的公网入口（https://<slug>.<zone>）。DSH 生成回调、"
                "WebSocket 地址都以此为基准；与浏览器地址栏不一致会被准入判定拦下。",
    },
    {
        "key": "TRUSTED_HOSTS",
        "label": "可信 Host 列表",
        "help": "DSH 的准入判定要求请求的 Host（和 Origin）落在这个列表里。"
                "只登记本实例自己的域名，多一个都算放行越权域名。",
    },
    {
        "key": "CONTAINER_PORT",
        "label": "容器内监听端口",
        "help": "DSH 进程在容器里监听的端口。frp 隧道把它映射到 VPS 的公网端口，"
                "nginxWebUI 再按域名反代过来。端口在本控制台内全局唯一分配。",
    },
    {
        "key": "FRP_REMOTE_PORT",
        "label": "frp 公网端口",
        "help": "frps 侧声明的 remotePort，从 4893 起递增分配，建之前会先在 VPS "
                "探测占用。释放实例时这条隧道最先被删掉。",
    },
    {
        "key": "DOCUMENTS_DIRECTORY",
        "label": "工作区根目录",
        "help": "DSH 用它拼默认工作区，容器里没有 xdg-user-dir，必须显式配。"
                "约定为 <host.data_root>/<slug>，落在机械盘上。",
    },
    {
        "key": "DATA_DIR",
        "label": "实例数据目录",
        "help": "宿主机侧的数据根（挂进容器）。释放实例只停容器，"
                "这个目录连同容器、镜像一律保留，绝不删除。",
    },
    {
        "key": "IMAGE_TAG",
        "label": "镜像标签",
        "help": "被管主机上 dsh-family 套件构建出的本地镜像，默认 dsh-family:local。"
                "换版本走部署，不在这里改。",
    },
    {
        "key": "RESTART_POLICY",
        "label": "容器重启策略",
        "help": "默认 unless-stopped：宿主机重启后 Docker 自动拉起容器。"
                "自动拉起会轮换登录 token，watcher 检测到 restart_count 增长后会"
                "重新从容器日志抓最新 token 落库。",
    },
    {
        "key": "TZ",
        "label": "容器时区",
        "help": "统一 Asia/Shanghai，日志时间与备份窗口（03:20–04:00）对得上。",
    },
]


def config_items(rendered: dict[str, Any] | None) -> list[dict[str, Any]]:
    """给详情响应 config.items 用：静态文案 + 本次部署实际下发的值。"""
    rendered = rendered or {}
    items = []
    for item in _ITEMS:
        items.append(
            {
                "key": item["key"],
                "value": rendered.get(item["key"], ""),
                "label": item["label"],
                "help": item["help"],
            }
        )
    return items


def config_doc_items() -> list[dict[str, Any]]:
    """纯文案（不带值），给「？」弹窗的整表说明用。"""
    return [dict(item) for item in _ITEMS]


def default_rendered(*, slug: str, public_url: str, trusted_hosts: list[str],
                     container_port: int, frp_remote_port: int, data_dir: str,
                     image_tag: str) -> dict[str, Any]:
    """部署时实际下发的配置快照（写进 instance_configs.rendered）。"""
    return {
        "PUBLIC_URL": public_url,
        "TRUSTED_HOSTS": ",".join(trusted_hosts),
        "CONTAINER_PORT": container_port,
        "FRP_REMOTE_PORT": frp_remote_port,
        "DOCUMENTS_DIRECTORY": data_dir,
        "DATA_DIR": data_dir,
        "IMAGE_TAG": image_tag,
        "RESTART_POLICY": "unless-stopped",
        "TZ": "Asia/Shanghai",
        "SLUG": slug,
    }
