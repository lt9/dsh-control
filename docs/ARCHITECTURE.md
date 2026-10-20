# dsh-control 架构与接口契约

给一群分散在多台机器 Docker 里的 DSH 实例做开通、状态查看、重启、回收的运维主控台。
本文件是后端的实现依据，也是前端唯一的对接依据。改契约必须先改本文件。

## 0. 硬性边界

1. 只操作自己创建的东西。frp 现有 58 条隧道、nginxWebUI 现有站点、阿里云现有 74 条解析记录
   **一律只读**；创建/删除只允许作用于本控制台登记过的对象（按 `dns_record_id`、
   `frp_proxy_id`、`ng_*` 主键精确删）。
2. frp 隧道、nginxWebUI 配置、阿里云解析删除属于不可逆动作，全部放在 `release` 流程里，
   该流程要求二次确认（确认词必须等于实例 slug）。
3. 释放**不删容器**：只 `docker stop`。数据目录、容器、镜像一律保留。列表仍可见，状态
   `released`，不提供恢复入口。
4. 任何密钥、token、AK/SK 不写日志、不进错误信息、不进 URL query。列表和详情默认打码，
   完整值只在明确的 reveal 动作里返回。
5. 主机侧执行一律走 SSH + `docker` 命令，不在被管主机上常驻 agent。

## 1. 组件

```
backend/  FastAPI + SQLAlchemy 2 + asyncpg + Alembic（对齐 ai-seo-platform 后端约定）
frontend/ React 18 + Vite + TS（对齐 ai-seo-platform console 约定）
```

后端内部模块与所有者：

| 模块 | 路径 | 职责 |
|---|---|---|
| app/api | `src/dsh_control/api/` | 路由、鉴权、序列化 |
| domain | `src/dsh_control/db/` | 模型、迁移、仓储 |
| orchestrator | `src/dsh_control/jobs/` | 部署/释放/重启状态机，步骤与日志落库 |
| watcher | `src/dsh_control/watcher.py` | 周期采状态、自动重启、抓 token |
| integrations | `src/dsh_control/integrations/` | 外部系统适配器（下面 §5） |
| cli | `src/dsh_control/cli.py` | `init` / `serve` / `seed` |

## 2. 数据模型（PostgreSQL）

库：与 ai-seo-platform 同一个 PG 实例（`tierai-prod-postgres-1`，Docker 网络
`tierai-prod_default`），独立数据库 `dsh_control`。

```
credentials        外部密钥仓库（按用户要求明文存库，可读回；只在 reveal 接口出网）
  id, kind(ssh_key|ssh_password|aliyun_ak|frp_panel|ng_panel|operator_key),
  name UNIQUE, secret jsonb, note, created_at, updated_at

hosts              被管主机
  id, name UNIQUE, ssh_host, ssh_port, ssh_user, ssh_credential_id -> credentials,
  docker_context, kit_path,                 # dsh-family 仓库在被管机上的路径
  data_root,                                # 实例数据根目录（必须落在机械盘）
  frpc_client_id,                           # frp-panel 里的客户端 id，例 tuan.c.TuanCloud@1
  enabled bool, note, created_at, updated_at

instances          DSH 实例（一个容器 = 一个人）
  id, slug UNIQUE,                          # 域名前缀 dshsub2 这种，也当确认词
  owner_label,                              # 给谁用的，例 老婆
  host_id -> hosts, container_name,
  config_version, image_tag, dsh_version,
  dns_name, dns_record_id,
  frp_proxy_id, frp_remote_port,
  ng_upstream_name, ng_server_id, ng_location_id,
  public_url, container_port,
  state(pending|deploying|running|unhealthy|restarting|released|error),
  desired_state(running|released),
  last_error, released_at, created_at, updated_at

instance_tokens    登录密钥历史，详情页永远取最新一条
  id, instance_id, token, url, source(bootstrap|logs|restart|manual), captured_at

instance_configs   部署时实际下发的配置快照，详情弹窗数据源
  id, instance_id, version, rendered jsonb, created_at

instance_snapshots 采集到的运行态
  id, instance_id, captured_at, docker_state, health, restart_count,
  cpu_pct, mem_usage_bytes, mem_limit_bytes, raw jsonb

deploy_jobs        一次性任务：deploy / release / restart
  id, instance_id, kind, state(pending|running|succeeded|failed),
  error, started_at, finished_at

deploy_steps       点对点进度，前端按 seq 渲染
  id, job_id, seq, key, title, state(pending|running|succeeded|failed|skipped),
  detail, started_at, finished_at

events             审计与自动重启记录
  id, kind, instance_id NULL, job_id NULL, message, data jsonb, created_at
```

## 3. 实例状态机

```
pending → deploying → running ⇄ unhealthy → restarting → running
                        ↓                        ↑（连续 3 次健康检查失败，自动）
                     released ← releasing
```

- `restarting` 由 watcher 自动触发或用户在详情页点重启；重启必然轮换登录 token，
  重启完成后必须重新抓 token 并写 `instance_tokens` + 记 `events`。
- 自动重启的三个抑制条件：连续失败 < 3 次不重启；备份窗口内不重启（默认 03:20–04:00，
  可配 `DSHC_BACKUP_WINDOW`）；距上次自动重启 < 10 分钟不重启（`DSHC_RESTART_COOLDOWN`）。
- `released` 是终态，watcher 不再采集，也不再对容器做任何写操作。

## 4. HTTP API

除 `/api/login` 与 `/api/healthz` 外全部要鉴权。写操作要求头 `X-Requested-With: 1`
（配合 `SameSite=Lax` cookie 做 CSRF 抑制）。

鉴权：单个运维密钥。登录页粘贴 → `POST /api/login {key}` → 服务端常数时间比对
`credentials(kind=operator_key)` 里的明文（按用户要求存明文可读回）→ 下发签名 cookie
`dshc_session`（HttpOnly, SameSite=Lax, 7 天）。失败按 IP 限速 5 次/分钟。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `{key}` → `{ok, expires_at}`，Set-Cookie |
| POST | `/api/logout` | 清 cookie |
| GET | `/api/session` | `{ok, expires_at}`，前端启动时探测 |
| GET | `/api/healthz` | 存活探针，无需鉴权 |
| GET | `/api/overview` | `{running, unhealthy, released, hosts, by_host[]}` |
| GET | `/api/hosts` | 主机列表（不含密钥） |
| GET | `/api/instances?q=&state=` | `q` 匹配 slug / owner_label / dns_name |
| POST | `/api/instances` | `{owner_label, host_id, note?}` → `{instance_id, dns_name, job_id}`；域名前缀由服务端生成（§6.1） |
| GET | `/api/instances/{id}` | 详情，见下 |
| GET | `/api/instances/{id}/token` | **现取**：SSH 抓容器日志里的最新一键地址，落库后返回 |
| POST | `/api/instances/{id}/restart` | → `{job_id}` |
| POST | `/api/instances/{id}/release` | `{confirm}` 必须逐字等于 `slug`，否则 422 → `{job_id}` |
| GET | `/api/jobs/{id}` | `{kind,state,steps:[{seq,key,title,state,detail}]}` |
| GET | `/api/jobs/{id}/logs` | 增量日志：`?after=<id>` → `{items:[{id,seq,level,line,ts}], next}` |
| GET | `/api/jobs/{id}/stream` | SSE，`step` / `log` / `done` 三种事件 |
| GET | `/api/instances/{id}/events` | 事件时间线 |

详情响应（前端所有展示字段都在这一个响应里，避免多轮请求）：

```json
{
  "id": 1, "slug": "dshsub2", "owner_label": "老婆", "host": {"id":1,"name":"nas-8"},
  "state": "running", "desired_state": "running",
  "dsh_version": "1.x.y", "image_tag": "dsh-family:local", "image_id": "5023c780",
  "config_version": "v1",
  "config": {"items": [{"key":"documentsDirectory","value":"/data","label":"工作区根目录",
                       "help":"DSH 用它拼默认工作区，容器里没有 xdg-user-dir，必须显式配"}]},
  "network": {"dns_name":"dshsub2.pipboy.cn","public_url":"https://dshsub2.pipboy.cn",
              "frp_remote_port":4893,"frp_proxy_id":575,"dns_record_id":"2112...","container_ip":"172.27.0.3"},
  "runtime": {"docker_state":"running","health":"healthy","restart_count":0,
              "cpu_pct":0.4,"mem_usage_bytes":280000000,"mem_limit_bytes":4294967296,
              "captured_at":"2026-10-20T21:40:00+08:00"},
  "access": {"has_token": true, "token_masked": "M1jq…IV0", "url": "https://dshsub2.pipboy.cn/?token=…",
             "captured_at":"2026-10-20T21:37:36+08:00"},
  "last_error": null, "released_at": null, "actions": {"restart": true, "release": true}
}
```

`access.url` 与 `access.token` 是完整值：详情页就是要给运维用的一键地址。它们**只**在详情
与 `/token` 两个接口出现，列表接口绝不返回。

`config.items[].help` 就是详情弹窗里那个「？」的文案来源，写在代码里的静态字典
（`domain/config_docs.py`），首期不做在线编辑；配置版本 `v1` 定义也在代码里，UI 不暴露编辑。

## 5. 外部集成适配器（全部已实测，签名照抄）

每个适配器都有 `dry_run` 模式（只记日志不写），单测与首次上线用。所有适配器只允许操作
`managed_only` 白名单内的对象 id。

### 5.1 SSH 执行器 `integrations/shell.py`

`run(host, cmd, stream_cb=None, timeout)`：`ssh -i <key> -p <port> -o BatchMode=yes`，
私钥从 `credentials(ssh_key)` 取，写到 0600 临时文件用后即删。stdout/stderr 逐行进
`deploy_steps` 日志。约定：非零退出即步骤失败，错误信息取 stderr 末 800 字节。

被管主机上复用的现成脚本（`kit_path` 下，dsh-family 仓库）：
`scripts/new-instance.sh`、`scripts/deploy.sh <name> [--recreate]`、`scripts/verify.sh <name>`、
`scripts/backup.sh <name>`。控制台不重新实现部署逻辑，只负责传参、采日志、判定结果。

抓 token：`docker logs <container>` 里匹配 `https?://[^[:space:]]+\?token=[^[:space:]]+`，
取最后一条。

### 5.2 frp-panel `integrations/frp_panel.py`

Base：`http://8.134.168.14:9000`（VPS，`frp-panel`，无文档，以下均已实测）。
登录：`POST /api/v1/auth/login {username,password}` → cookie `frp-panel-cookie`（JWT，
httpOnly）；另有 `x-set-authorization` 头可用。响应统一 `{code,msg,body}`，`code!=200` 抛错。

| 动作 | 路径 | 请求体 |
|---|---|---|
| 列 | `POST /api/v1/proxy/list_configs` | `{clientId?, page, pageSize}` → `body.proxy_configs[]` |
| 读 | `POST /api/v1/proxy/get_config` | `{id, name, client_id, server_id}` → `body.proxy_config` |
| 建/改 | `POST /api/v1/proxy/update_config` | 见下，`config` 是 bytes（base64 的 JSON 字符串） |
| 删 | `POST /api/v1/proxy/delete_config` | 同上结构，字段名以服务端报错为准（实现时用 dry_run 试出） |

记录形状（读回来的）：`{id, name, type, client_id, server_id, config, origin_client_id}`，
`config` 解开后是
`{name, type:"tcp", transport:{bandwidthLimit:""}, loadBalancer:{group:""}, healthCheck:{type:"",
intervalSeconds:0}, localIP, localPort, plugin:null, remotePort}`。
本控制台建的隧道 `name` 固定 `dshctl_<slug>`，服务端 `server_id=default`，
客户端取 `hosts.frpc_client_id`（`dshsub1` 现挂在 `tuan.c.TuanCloud@1`，因为它用内网 IP 直连 NAS）。
**现有隧道一条都不许改。**

### 5.3 阿里云解析 `integrations/aliyun_dns.py`

RPC 签名 v1（HMAC-SHA1），endpoint `alidns.aliyuncs.com`。只用三个 Action：
`DescribeDomainRecords`（查，含按 RR 精确查）、`AddDomainRecord`、`DeleteDomainRecord`。
硬白名单：`DomainName` 必须等于 `DSHC_DNS_ZONE`（默认 `pipboy.cn`），`RR` 必须以
`DSHC_DNS_PREFIX`（默认 `dshsub`）或 `dshctl` 开头，否则拒绝。记录固定 `Type=A`、
`Value=DSHC_INGRESS_IP`（默认 `8.134.168.14`）、`TTL=600`。删除只按 `RecordId`，且该 id
必须是库里登记过的。已实测：`pipboy.cn` 共 74 条，`dshsub1 A → 8.134.168.14`
`RecordId=2112523148163198976`。

### 5.4 nginxWebUI `integrations/ng_panel.py`

Base：`http://8.134.168.14:18180`，账号在 `credentials(ng_panel)`。`/api/*` 要 `token` 头
（登录接口换取），文档在 jar 的 `static/doc/*.html`：`ServerApiController`、`UpstreamApiController`。
**没有 LocationApiController**，location 靠 `ServerApiController` 的嵌套字段下发。

必须生成的配置（这是今晚踩过的坑，写死在模板里）：
- 反代站点的 location 上 `headerHost = "$host"`，**不能是 `$host:$proxy_port`**：DSH 的准入判定
  要求 `Origin` 的 authority 与 `Host` 完全相等，带端口必被 403，症状是"界面能打开但一直重连"。
- `websocket = true`：`/api/remote.mux` 走 WebSocket。
- 服务器级反代（不带 location）永远生成 `$host:$proxy_port`，所以**必须走 location**。

创建/更新调用要幂等：先按 `server_name` 查，存在则带 id 更新。

### 5.5 首发验证顺序

控制台自身就是第一个实例：`dshctl.pipboy.cn`。部署时按 DNS → frp → NG → 探活的顺序创建，
每一步都先 dry_run 打印将要发出的请求，再真做，做完立刻回读核对（`list_configs` 里能查到
自己的隧道、`DescribeDomainRecords` 里能查到自己的记录）。**这是唯一允许对 frp/NG/DNS
写数据的场景，作用域只有这一条新对象。**

## 6. 编排步骤

### 6.1 新建实例 `deploy`

| seq | key | 做什么 | 失败怎么办 |
|---|---|---|---|
| 10 | `plan` | 生成 slug：取 `DSHC_DNS_PREFIX` + 现有实例最大序号+1；查 DNS 确认未被占 | 直接失败，无副作用 |
| 20 | `host_prepare` | SSH 检查主机可达、数据盘剩余空间、dsh-family kit 在位 | 无副作用 |
| 30 | `dns_create` | 加 A 记录，回读拿 RecordId | 删掉刚建的记录 |
| 40 | `frp_create` | 建隧道（remotePort 从 4893 起递增，先探测 VPS 上未被占用） | 删刚建的隧道 |
| 50 | `ng_create` | 建 upstream + 反代站点（location：`$host` + websocket） | 删刚建的站点/upstream |
| 60 | `container_deploy` | SSH 跑 `new-instance.sh` + `deploy.sh`，传 `PUBLIC_URL`/`TRUSTED_HOSTS` | 保留现场，标 error |
| 70 | `token_capture` | 从容器日志抓一键地址 | 标 error，可重试 |
| 80 | `probe_public` | 公网探测：首页 200 + 无 token 打 `/api/remote.mux` 期望 401（403 说明反代 Host 又错了） | 标 degraded，给出修正提示 |
| 90 | `verify` | 跑 kit 的 `verify.sh <slug>`，收条目数 | 记录，不推翻成功 |

每步把命令、耗时、stdout 摘要写进 `deploy_steps`，前端按 seq 逐条点亮。

### 6.2 释放 `release`（二次确认后）

顺序必须是先断外网入口再停容器：`probe_guard`（确认 slug 对得上）→ `ng_delete` →
`frp_delete` → `dns_delete` → `container_stop`（**只 stop，不 rm，不动数据目录，不动镜像**）
→ `mark_released`。每步都记「删掉的对象的 id 与名字」进 `events`，出事能查到删了什么。

### 6.3 重启 `restart`

`container_restart` → `wait_healthy`（最多 120 秒）→ `token_capture` → `probe_public`。

## 7. 部署形态

单容器 `dsh-control`：FastAPI 直接 serve 前端构建产物（`frontend/dist` 打进镜像），
一个进程同时给 API 和静态页，避免跨域与第二个服务。监听 `DSHC_LISTEN_PORT=13300`。

- 连 PG：加入 `tierai-prod_default` 网络，`DSHC_DATABASE_URL=postgresql+asyncpg://tierai:…@172.19.0.6:5432/dsh_control`
- 出公网：`dshctl.pipboy.cn` → NG → frps → `192.168.3.109:13300`
- 迁到 .8：换一份 `.env`（PG 地址、监听端口、frpc 客户端、数据根目录）重起即可，
  代码里不允许出现任何写死的机器地址，全部走环境变量或 `hosts` 表。

## 8. 环境变量

```
DSHC_DATABASE_URL            必填
DSHC_SESSION_SECRET          必填，cookie 签名密钥
DSHC_LISTEN_HOST/PORT        0.0.0.0 / 13300
DSHC_SESSION_DAYS            默认 7
DSHC_INGRESS_IP              8.134.168.14
DSHC_DNS_ZONE                pipboy.cn
DSHC_DNS_PREFIX              dshsub
DSHC_PUBLIC_BASE             https://dshctl.pipboy.cn
DSHC_BACKUP_WINDOW           03:20-04:00
DSHC_RESTART_COOLDOWN        600
DSHC_POLL_INTERVAL           30
DSHC_FRP_PANEL_URL / _USER / _PASS
DSHC_NG_URL / _USER / _PASS
DSHC_ALIYUN_AK / _SK
DSHC_SSH_KEY_PATH_NAS8 / _HOST109    init 时读进库，之后从库取
DSHC_DRY_RUN                 1 = 所有外部写操作只打印
```

`python -m dsh_control.cli init` 建表、灌种子（两台主机、v1 配置版本、把上面这些外部
凭据读进 `credentials`、生成运维密钥并**只在本次 stdout 打印一次**，库里同时存一份明文
以便日后读回）。

## 9. 前端

`frontend/` React + Vite + TS，设计走 `/impeccable`，模式 **Operate**（运维者来完成动作，
扫读效率优先，不做营销式花活）。四个界面：

1. 登录页：一个密钥输入框 + 粘贴即可，不要求用户名。
2. 列表页：搜索框（域名/用途都能搜）+ 新建按钮 + 表格：用途与 slug、状态、DSH 版本、
   配置版本、所在主机、域名、更新时间。`released` 默认折叠在次要分组里。
3. 详情页：运行态与网络信息；配置卡底部一个小「？」开弹窗逐条解释每个配置项；
   一键登录 URL 与 token（一键复制，复制后给明确反馈）；操作区重启 + 释放（输入 slug
   原文才能点，二次确认）；异常时给最近事件与错误。
4. 新建向导：选主机 → 填用途标签 → 自动出域名前缀（可改）→ 确认 → 逐步进度条 + 实时日志
   → 完成后给出可复制的一键地址并高亮。

约定：中文界面；状态色语义固定（running 绿、unhealthy/restarting 琥珀、error 红、
released 灰）；所有时间按 `Asia/Shanghai`；复制成功要有可见反馈；表格与日志里的
token 默认打码，点一下才展开。
