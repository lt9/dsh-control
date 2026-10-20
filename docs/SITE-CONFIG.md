# 站点配置与可替换性

本仓库**不含任何站点专属事实**。域名、入口 IP、面板地址、被管主机、SSH 用户与路径、
frpc 客户端 id、已分配的公网端口、DNS RecordId、frp proxy id、NG server id——这些一律是
**数据库里的行**或**站点描述文件里的值**，代码与迁移脚本里出现即为缺陷。

## 三件事各归各位

| 东西 | 放哪 | 怎么进去 |
|---|---|---|
| 数据库表结构 | `migrations/` | `cli init`，**零数据**，不含任何 INSERT 站点数据 |
| 运维密钥 | `credentials(kind=operator_key)` | `cli init` 生成并只打印一次 |
| 外部系统凭据 | `credentials`（`aliyun_ak` / `frp_panel` / `ng_panel` / `ssh_key`） | `cli seed --site <file>`，值从环境变量读，文件里只写变量名 |
| 被管主机 | `hosts` 表 | 同上 |
| 入口 IP、DNS zone、域名前缀、公网端口起始值 | `site` 段 → 落 `settings` 与 `credentials(site)` | 同上 |
| 每个实例的 RecordId / frp proxy id / NG server id / 公网端口 | `instances` 表对应列 | 创建流程写入，删除流程按它删 |

`docs/site/` 是我自己站点的运维笔记（已 gitignore），不是配置来源，也不随开源发布。

## 站点描述文件

仓库里只提交 [deploy/site.example.toml](../deploy/site.example.toml)（占位值）。
真实文件 `deploy/site.toml` 不提交。形状：

```toml
[site]
public_base = "https://console.example.com"
ingress_ip = "203.0.113.10"          # A 记录指向的入口（frps 所在机器的公网 IP）
dns_zone = "example.com"
domain_prefix = "dsh-"               # 一级子域，受证书通配层级约束
frp_port_start = 4893
dsh_image = "dsh-family:local"      # 开通时用的镜像标签；本机必须已有，控制台不构建

[providers]                           # 换厂商只改这里，见下
dns = "aliyun"                        # aliyun | noop
frp = "frp_panel"                     # frp_panel | noop
ng = "nginxwebui"                     # nginxwebui | noop

[[credentials]]
kind = "aliyun_ak"
name = "prod-dns"
env = { access_key_id = "DSHC_ALIYUN_AK", access_key_secret = "DSHC_ALIYUN_SK" }

[[credentials]]
kind = "frp_panel"
name = "panel"
env = { username = "DSHC_FRP_PANEL_USER", password = "DSHC_FRP_PANEL_PASS" }
base_url = "http://203.0.113.10:9000"

[[credentials]]
kind = "ng_panel"
name = "panel"
env = { username = "DSHC_NG_USER", password = "DSHC_NG_PASS" }
base_url = "http://203.0.113.10:18180"

[[credentials]]
kind = "ssh_key"
name = "console-ed25519"
env_file = { private_key_pem = "DSHC_SSH_KEY_FILE" }   # 指向一个私钥文件路径

[[credentials]]
kind = "llm_key"
name = "family-llm"
secret = { api_key = "……", search_api_key = "……（可空）" }

[[hosts]]
name = "nas-1"
ssh_host = "192.0.2.10"
ssh_port = 22
ssh_user = "ops"
kit_path = "/srv/dsh-family"
data_root = "/srv/dsh-data"
frpc_client_id = "ops.c.nas1@1"
credential_name = "console-ed25519"
```

## 换掉 frps / nginxWebUI / 云解析要动什么

只有两处，且都不碰业务代码：

1. `[providers]` 换成新实现（或 `noop` 表示这层人工做，开通流程把该步标 `skipped`
   并把该人工做的事打印出来）。
2. 对应 `[[credentials]]` 换 `base_url` 与凭据。

`integrations/registry.py` 按 `providers` 的名字取实现，业务层（jobs / watcher / api）
只依赖 `integrations/base.py` 里的 Protocol。新增一个厂商 = 新增一个实现文件 +
在 registry 注册一行，不改调用方。

镜像同理：具体标签不许出现在代码里，只有站点配置里有。控制台不构建镜像，只在开通前检查目标机上是否已有，缺失时用 `cli images ship` 从另一台搬过来。

硬约束：入口 IP 不许出现在代码里（探活、建记录、拼 upstream 地址都从 `site` 段取）；
证书通配层级决定域名只能是 `<前缀><序号>.<zone>` 这种一级子域，这条与厂商无关，
是 TLS 的普遍约束，写在 `domain/slug.py` 并在文档里说明。

`noop` provider 是通用性的另一半：没有阿里云也能用（DNS 自己指），没有 nginxWebUI 也能用
（反代自己配），只有被管主机与 SSH 是必需的。

## 模型端点与密钥（开通时必须就位）

```toml
[site]
llm_base_url = "http://192.0.2.20:8000/v1"   # 实例必须只往这里发（也是安全门的判据）
llm_model_id = ""                             # 留空则用镜像内默认
```

`.env` 侧对应 `DSHC_LLM_BASE_URL` / `DSHC_LLM_MODEL_ID`；密钥放
`credentials(kind=llm_key)`，`secret.api_key` 必填、`secret.search_api_key` 可空。

开通第 60 步把这三样写进实例的 `.env`（`LLM_BASE_URL` / `LLM_API_KEY` / `SEARCH_API_KEY`）。
**密钥走 stdin 下发，不进命令行**：命令行进任务日志，也会在目标机的 `ps` 里被看见。

两处名字陷阱（kit 的历史包袱，别按容器里看到的名字去改 `.env`）：
`.env` 的 `LLM_API_KEY` 在容器里叫 `LLAMA_API_KEY`，`SEARCH_API_KEY` 叫 `DEEPSEEK_API_KEY`。

## 安全门（不通过就不算开通成功）

第 90 步读**容器生效配置**（`docker exec … dsh web --dump-config`，不是仓库模板）逐条核对：

- 必须 `disabled: true`：`llm-deepseek`、`llm-deepseek-account`、`deepseek-account`、
  `session-log-deepseek`、`plugin-package-inventory-deepseek`、`authorization`
  —— 少一条都算没过（**条目不存在也按没过处理**），因为这几条是往云端发请求、会
  产生账单或把内容外传的路径。
- 必须**没被**禁用：`web-deepseek-api`（联网搜索走它，禁了她的实例会静默失去搜索）。
- `providers.local.baseURL` 必须等于本站 `llm_base_url`，空值或指向别处都不过。
- 拿不到配置（输出为空）= 不过。

条目块读到下一个 `- id:` 为止，不按固定行数开窗：`grep -A8` 那种会把下一条目的
`disabled: true` 算到这一条头上。

判定权在控制台，不在 kit 的 `verify.sh`：现网出现过同一份配置两台机器结论相反
（脚本在 `set -uo pipefail` 下被 `printf | grep -q` 的 SIGPIPE 骗出假警报）。kit 的
检查项现在只作为参考，不通过时发 `verify_warnings` 事件，不影响开通结论。
