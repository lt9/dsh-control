"""v1 配置版本定义与「？」弹窗文案（契约 §4：写在代码里的静态字典，首期不可编辑）。

`CONFIG_V1.items[]` 的每一项：key = 下发给 dsh-family 部署套件的配置键；
label/help = 详情弹窗逐条解释的文案来源；value 在渲染时从
instance_configs.rendered 里按 render_key 取（缺省按 key 取）。
"""

from __future__ import annotations

from typing import Any

# v2：第 66 步开始往快照里写 "DSH" 这一组（模型/压缩/插件/技能/预设）。
# add_config_snapshot 按 (instance_id, version) upsert，沿用 v1 会让旧快照
# 在新文案下渲染出一屏空值，所以语义变了就必须升版本号。
CONFIG_VERSION = "v2"

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
        "help": "DSH 进程在容器里监听的端口，取自 docker 的端口映射（8080/tcp -> "
                "0.0.0.0:20002 里的 8080）。现网曾把宿主机端口当这个值显示，"
                "于是 20002 被标在了容器上。",
    },
    {
        "key": "HOST_PORT",
        "label": "宿主机发布端口",
        "help": "容器端口发布到宿主机上的哪个端口（上面那条映射的右半边）。"
                "frpc 从这里取流量，再经隧道送到 VPS 的公网端口。",
    },
    {
        "key": "FRP_REMOTE_PORT",
        "label": "frp 公网端口",
        "help": "frps 侧声明的 remotePort，从 frp_port_start 起递增分配，建之前会先在 VPS "
                "探测占用。释放实例时这条隧道最先被删掉。",
    },
    {
        "key": "DOCUMENTS_DIRECTORY",
        "label": "工作区根目录",
        "help": "DSH 用它拼默认工作区，容器里没有 xdg-user-dir，必须显式配。"
                "真实路径由 kit 的 new-instance.sh 决定："
                "<DATA_ROOT>/dsh-family/<实例名>/data，所以 data_root 该给一个挂载点"
                "（例如 /mnt/hdd2），别给已经带项目名的目录，否则会多出一层。",
    },
    {
        "key": "DATA_DIR",
        "label": "实例数据目录",
        "help": "宿主机侧的数据根（挂进容器）。释放实例只停容器，"
                "这个目录连同容器、镜像一律保留，绝不删除。",
    },
    {
        "key": "CONFIG_SOURCE",
        "label": "配置来源",
        "help": "「生效值」= 部署完成后从实例目录的 .env 读回来的真实值；"
                "「估算」= 没读到 .env，只能沿用开通前的推测值（端口与目录都是算出来的，"
                "别当成现场事实）。",
    },
    {
        # 展示条目：镜像的来龙去脉（值与 IMAGE_TAG 同源，都是站点配置的默认版本）
        "key": "image",
        "label": "镜像",
        "help": "镜像是事先构建好传到那台机器上的，控制台不构建；"
                "换版本是以后的能力，现在固定用站点配置的默认版本",
    },
    {
        "key": "IMAGE_TAG",
        "label": "镜像标签",
        "help": "部署时选定的镜像标签；来自站点配置 DSHC_IMAGE（准备好的版本，开通阶段不构建）。"
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
        "key": "LLM_MODEL_ID",
        "label": "实例模型名",
        "help": "写进实例 .env、由 kit 渲染进 DSH 配置的那个名字，要对的是推理服务真实"
                "在服务的模型名（别名是给旧客户端兼容用的，不是真实模型）。"
                "换名只改这一个值，重启容器生效。",
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
        "HOST_PORT": 0,
        "FRP_REMOTE_PORT": frp_remote_port,
        "DOCUMENTS_DIRECTORY": data_dir,
        "DATA_DIR": data_dir,
        "IMAGE_TAG": image_tag,
        "image": image_tag,
        "RESTART_POLICY": "unless-stopped",
        "TZ": "Asia/Shanghai",
        "SLUG": slug,
    }
