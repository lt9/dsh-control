# dsh-control 架构与接口契约

给一群分散在多台机器 Docker 里的 DSH 实例做开通、状态查看、重启、回收的运维主控台。
本文件是后端的实现依据，也是前端唯一的对接依据。改契约必须先改本文件。

## 0. 硬性边界

1. 只操作自己创建的东西。frp 现有 58 条隧道、nginxWebUI 现有站点、阿里云现有 74 条解析记录
   **一律只读**；创建/删除只允许作用于本控制台登记过的对象（按 `dns_record_id`、
   `frp_proxy_id`、`ng_*` 主键精确删）。
2. frp 隧道、nginxWebUI 配置、阿里云解析删除属于不可逆动作，全部放在 `release` 流程里，
   该流程要求二次确认：服务端仍校验 `confirm == slug`，但该值由前端填，用户用长按确认
   而不是打字（§9）。
3. 释放**不删容器**：只 `docker stop`。数据目录、容器、镜像一律保留。列表仍可见，状态
   `released`，不提供恢复入口。
4. 任何密钥、token、AK/SK 不写日志、不进错误信息、不进 URL query。列表和详情默认打码，
   完整值只在明确的 reveal 动作里返回。
5. 主机侧执行一律走 SSH + `docker` 命令，不在被管主机上常驻 agent。

## 1. 仓库布局与组件

MIT 开源项目，目录按标准 src-layout 组织：

```
dsh-control/
├── LICENSE  README.md  CONTRIBUTING.md  SECURITY.md  CHANGELOG.md
├── Makefile  pyproject.toml  .env.example  .editorconfig
├── Dockerfile  docker-compose.yml
├── src/dsh_control/
│   ├── settings.py  main.py  cli.py  watcher.py
│   ├── api/        路由、鉴权、schema、序列化
│   ├── db/         模型、仓储、会话（迁移在仓库根 migrations/）
│   ├── domain/     slug、配置项说明、时间等纯逻辑
│   ├── jobs/       deploy / restart / release 状态机与 runner
│   └── integrations/  外部系统适配器（§5），base.py 是冻结接口
├── migrations/     Alembic
├── tests/          unit/ 与 integration/，不碰真外部系统
├── frontend/       React + Vite + TS，构建产物打进镜像由后端 serve
├── deploy/         部署样例（compose 片段、反代样例配置）
├── scripts/        开发辅助脚本
└── docs/           本文件、adr/、vendor/；docs/site/ 是本站点专属资料，已 gitignore，
                    含内网地址与记录 id，开源发布时不带
```

导入一律 `from dsh_control...`，**没有 `backend/` 前缀**（2026-10-20 已从
`backend/src/...` 迁到 `src/...`，测试从 `tests/` 起，Alembic 在 `migrations/`）。
ADR 一条决策一个文件：`docs/adr/NNN-标题.md`。

| 模块 | 路径 | 职责 |
|---|---|---|
| api | `src/dsh_control/api/` | 路由、鉴权、序列化 |
| db | `src/dsh_control/db/` | 模型、迁移、仓储 |
| jobs | `src/dsh_control/jobs/` | 部署/释放/重启状态机，步骤与日志落库 |
| watcher | `src/dsh_control/watcher.py` | 周期采状态、自动重启、抓 token |
| integrations | `src/dsh_control/integrations/` | 外部系统适配器（下面 §5） |
| cli | `src/dsh_control/cli.py` | `init` / `serve` / `seed` |

## 2. 数据模型（PostgreSQL）

库：与 ai-seo-platform 同一个 PG 实例（`tierai-prod-postgres-1`，Docker 网络
`tierai-prod_default`），独立数据库 `dsh_control`。

### 2.0 数据库三条规矩（不可违反，评审会逐条查）

1. **不用外键**。表之间只有逻辑关联（`host_id`、`instance_id` 这类列 + 普通索引），
   没有 `FOREIGN KEY` 约束，级联行为一律在程序里显式写。删除父行不自动动子行，
   谁删谁负责，删完记 `events`。
2. **不写联表查询**。仓储层一张表一张表查，用 `WHERE id IN (...)` 批量取回后在 Python
   里组装；一个请求里不许出现 `JOIN`。列表接口组装详情时也是这个走法。
3. **每个字段都要有备注**。SQLAlchemy 每个列写 `comment="…"`，Alembic 迁移里
   `op.create_table` 的列同样带 `comment=`，落库后 `psql` 里 `\d+ 表名` 必须能看到
   中文说明。表本身也要 `Comment`。没有备注的列不许合入。

顺带：布尔列用 `is_`/`has_` 前缀或 `enabled` 这类形容词；时间列统一 `created_at`、
`updated_at`、`*_at`，一律 `timestamptz` 存 UTC，显示层转 `Asia/Shanghai`；
软删不用（本项目的"释放"是业务终态，不是软删标记）；金额/字节用 `bigint`；
状态列用 `text` + CHECK 约束（不用 PG enum，加值要改类型太脆）。

```
credentials        外部密钥仓库（按用户要求明文存库，可读回；只在 reveal 接口出网）
  id, kind(ssh_key|ssh_password|aliyun_ak|frp_panel|ng_panel|operator_key),
  name UNIQUE, secret jsonb, note, created_at, updated_at

hosts              被管主机
  id, name UNIQUE, ssh_host, ssh_port, ssh_user, ssh_credential_id,   # 逻辑指向 credentials.id，无外键
  docker_context, kit_path,                 # dsh-family 仓库在被管机上的路径
  data_root,                                # 实例数据根目录（必须落在机械盘）
  frpc_client_id,                           # frp-panel 里的客户端 id，例 office-frpc@1
  enabled bool, note, created_at, updated_at

instances          DSH 实例（一个容器 = 一个人）
  id, slug UNIQUE,                          # 域名标签 dsh-NN，程序生成，也当确认词
  owner_label,                              # 备注名，事后可改，不影响任何外部对象
  host_id, container_name,                    # 逻辑指向 hosts.id，无外键
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

上面每个 `id` 类关联列都只建普通索引，不建外键；`instance_id`/`job_id`/`host_id`/`ssh_credential_id` 的取值合法性由仓储层在写入前校验。

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
| POST | `/api/instances` | 请求体可为空，或 `{host_id?}`（不传就由服务端按剩余空间选主机）→ `{instance_id, dns_name, job_id}`；slug 与端口一律服务端生成（§6.1.1），**任何情况下不接受用户传入的名字** |
| GET | `/api/instances/{id}` | 详情，见下 |
| GET | `/api/instances/{id}/token` | **现取**：SSH 抓容器日志里的最新一键地址，落库后返回 |
| POST | `/api/instances/{id}/restart` | → `{job_id}` |
| POST | `/api/instances/{id}/release` | `{confirm}` 必须逐字等于 `slug`，否则 422 → `{job_id}`。**这个值由前端自己填**（前端知道 slug），用户不需要打字；防误操作靠 §9 的按住式确认，不靠输入 |
| GET | `/api/jobs/{id}` | `{kind,state,steps:[{seq,key,title,state,detail}]}` |
| GET | `/api/jobs/{id}/logs` | 增量日志：`?after=<id>` → `{items:[{id,seq,level,line,ts}], next}` |
| GET | `/api/jobs/{id}/stream` | SSE，`step` / `log` / `done` 三种事件 |
| GET | `/api/instances/{id}/events` | 事件时间线 |

详情响应（前端所有展示字段都在这一个响应里，避免多轮请求）：

```json
{
  "id": 1, "slug": "dsh-02", "owner_label": "家人", "host": {"id":1,"name":"nas-8"},
  "state": "running", "desired_state": "running",
  "dsh_version": "1.x.y", "image_tag": "dsh-family:local", "image_id": "5023c780",
  "config_version": "v1",
  "config": {"items": [{"key":"documentsDirectory","value":"/data","label":"工作区根目录",
                       "help":"DSH 用它拼默认工作区，容器里没有 xdg-user-dir，必须显式配"}]},
  "network": {"dns_name":"dsh-02.example.com","public_url":"https://dsh-02.example.com",
              "frp_remote_port":4893,"frp_proxy_id":575,"dns_record_id":"2112...","container_ip":"192.0.2.5"},
  "runtime": {"docker_state":"running","health":"healthy","restart_count":0,
              "cpu_pct":0.4,"mem_usage_bytes":280000000,"mem_limit_bytes":4294967296,
              "captured_at":"2026-10-20T21:40:00+08:00"},
  "access": {"has_token": true, "token_masked": "M1jq…IV0",
             "captured_at":"2026-10-20T21:37:36+08:00"},
  "last_error": null, "released_at": null, "actions": {"restart": true, "release": true}
}
```

`access` 只有 `has_token` / `token_masked` / `captured_at` 三个字段，**没有任何完整值**。
完整 token 与含 token 的一键地址只在 reveal 动作 `GET /api/instances/{id}/token` 里出现，
详情页进页面和每次刷新都调它现取——用户要的「详情页永远是最新的登录密钥」由这条满足，
而不是把密钥摊在每次详情响应里。列表接口连 mask 都不给。
把完整值放进详情等于多一处泄露面：详情会被缓存、被日志抓到、被随手截图分享，
而 reveal 是一次有意的动作，还要求 `X-Requested-With` 头。

`config.items[].help` 就是详情弹窗里那个「？」的文案来源，写在代码里的静态字典
（`domain/config_docs.py`），首期不做在线编辑；配置版本 `v1` 定义也在代码里，UI 不暴露编辑。

### 4.1 错误响应机器码清单（前端照表映射，别靠猜）

所有本 API 主动返回的错误统一形态：HTTP 状态码 + JSON
`{"ok": false, "error": "<机器码>", ...可选补充字段}`。
机器码是唯一稳定契约，文案前端自己组织。

| 机器码 | HTTP | 触发场景 | 建议人话 |
|---|---|---|---|
| `bad_key` | 401 | 登录时运维密钥不对 | 密钥不对，再试一次 |
| `unauthenticated` | 401 | 未登录 / cookie 过期，访问任何需鉴权接口 | 登录过期了，重新登录 |
| `not_initialized` | 500 | 库里还没有 operator_key（没跑 `cli init`） | 系统未初始化，联系运维跑 init |
| `rate_limited` | 429 | 登录按 IP 限流命中 | 试得太快，等一分钟再试 |
| `missing_x_requested_with` | 403 | 写请求没带 `X-Requested-With` 头 | 页面脚本异常，刷新重试 |
| `not_found` | 404 | 实例/任务 id 不存在（含静态资源兜底） | 找不到了，回列表看看 |
| `host_not_found` | 404 | `/token` reveal 时实例所在主机不存在 | 主机登记丢了 |
| `token_not_found` | 502 | reveal 时容器里抓不到最新 token | 刚从容器日志抓不到新地址，先确认容器在跑再重试 |
| `no_enabled_host` | 422 | 新建实例但 hosts 里无可用主机（自动选机） | 没有能用的主机，先 seed/启用主机 |
| `host_not_available` | 422 | 指定 host_id 但该机禁用或数据盘不可用 | 这台机现在不能接单 |
| `site_not_seeded` | 422 | 缺站点级配置（域名 zone/入口 IP 等）就尝试部署 | 站点还没 seed，先 `cli seed --site …` |
| `cannot_restart_from_<state>` | 409 | 从 released 等非 running/error 状态发起重启 | 当前状态不能重启 |
| `confirm_mismatch` | 422 | 释放确认词与 slug 不一致 | 确认词不对，输入框里输入实例名 |
| `already_released` | 409 | 重复释放 | 已经释放过了 |

不在表里但会遇到的两种：

- **请求体校验失败**：FastAPI 原生 422，`detail` 是数组不是 `{ok:false}` 形态——
  本系统所有请求体字段都是可选/宽松的，正常 UI 流不会触发；碰到即 bug。
- **任务失败**不在此表：job 的失败看 `GET /api/jobs/{id}` 的 `error`
  （已脱敏文本），不是 HTTP 错误响应。

> 没有 `dry_run` / `not_managed` 这类 HTTP 码：dry-run 与 noop 只存在于 CLI
> （`cli expose --dry-run` / providers=noop），API 层永远真执行。

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

> **实测坑一**：`list_configs` 带 `clientId` 过滤时返回**空集**而不是筛选结果（同一时刻
> 不带过滤 59 条、带过滤 0 条）。所以过滤必须在本地做。这条不是性能问题：只要经由
> 面板过滤，`find_by_name` 就永远查不到，`ensure` 会重复建门，创建后的回读校验也永远
> 失败——第一次真实开通就是这样把已经建好的隧道判成"不存在"的。

> **实测坑二：探活要用对回源地址，否则会把完全正常的隧道判成死的**。frps 的隧道端口在云上
> **只在入口机的内网地址上服务**：同一时刻 `127.0.0.1:<port>` 一律无响应，
> `<内网IP>:<port>` 正常服务（拿老隧道对照过，200/403 都有）。所以 NG 的 upstream 后端必须
> 写那个内网地址（站点配置 `frps_upstream_host`）——这也正是"公网只放 443、隧道端口不暴露
> 到公网"的做法。本次就被回环地址带偏过一次：拿 `127.0.0.1` 探活全挂，又在目标机上
> `tcpdump` 抓到零报文（那正是回环探测根本没出机的必然结果），于是误判成"面板没下发、
> 客户端没加载、要重启 frpc"。**实际上隧道建完就是 running、就能用**：在面板上操作即可
> 完成一切，不需要动任何客户端。硬约束：隧道建完必须端到端探测；探测失败时**先自查回源
> 地址是不是用错了**，再谈"没生效"；**绝不主动重启别人的 frpc/frps**。

Base：`http://203.0.113.10:9000`（VPS，`frp-panel`，无文档，以下均已实测）。
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
客户端取 `hosts.frpc_client_id`（`legacy-door` 现挂在 `office-frpc@1`，因为它用内网 IP 直连 NAS）。
**现有隧道一条都不许改。**

### 5.3 阿里云解析 `integrations/aliyun_dns.py`

RPC 签名 v1（HMAC-SHA1），endpoint `alidns.aliyuncs.com`。只用三个 Action：
`DescribeDomainRecords`（查，含按 RR 精确查）、`AddDomainRecord`、`DeleteDomainRecord`。
硬白名单：`DomainName` 必须等于 `DSHC_DNS_ZONE`（默认 `example.com`），`RR` 必须以
`DSHC_DNS_PREFIX`（默认 `dsh-`）或 `dshctl` 开头，否则拒绝。`RR` 只能是**一级**子域
（不含点），因为现成证书 `CN=*.example.com`、SAN 只有 `*.example.com` 与 `example.com`，
通配符只覆盖一层；`wife.dsh.example.com` 这种两级名字会撞上证书不覆盖。本控制台**不申请
自己的证书**，只复用 NG 里 acme.sh 自动续期的那张通配证书。记录固定 `Type=A`、
`Value=DSHC_INGRESS_IP`（默认 `203.0.113.10`）、`TTL=600`。删除只按 `RecordId`，且该 id
必须是库里登记过的。已实测：`example.com` 共 74 条，`legacy-door A → 203.0.113.10`
`RecordId=2112523148163198976`。

### 5.4 nginxWebUI `integrations/ng_panel.py`

Base：`http://203.0.113.10:18180`，账号在 `credentials(ng_panel)`。`/api/*` 要 `token` 头
（登录接口换取）。location 有独立入口 `GET /api/server/insertOrUpdateLocation`（form 参数，
面板侧只强制 `serverId` + `path`），**没有**独立的 `LocationApiController` 文档页，字段表按
部署版 smart-doc + 上游源码 + 真机回读三方对齐，全集见 `ng_panel.LOCATION_PARAM_FIELDS`，
越界字段本地拒写。

必须生成的配置（这是今晚踩过的坑，写死在模板里）：
- 反代站点的 location 上 `headerHost = "$host"`，**不能是 `$host:$proxy_port`**：DSH 的准入判定
  要求 `Origin` 的 authority 与 `Host` 完全相等，带端口必被 403，症状是"界面能打开但一直重连"。
- `websocket = true`：`/api/remote.mux` 走 WebSocket。
- 服务器级反代（不带 location）永远生成 `$host:$proxy_port`，所以**必须走 location**。

创建/更新调用要幂等：先按 `server_name` 查，存在则带 id 更新。upstream 后端同理：地址变了要
**带 `id` 原地改**那一条，不是再添加一条——两条后端是轮询关系，留着旧地址等于一半请求打到
死地址；同时挂着我们和其它地址两组后端时直接报 `ng_backend_drift`，不猜。

**API 只写库，不落盘**（真机踩出来的，面板自己的前端也是这么走的）：`insertOrUpdate*` 全部只写
sqlite；把库物化成磁盘 conf 的入口是 `/api/nginx/replace`，UI 保存站点后前端替用户调的就是它；
`/api/nginx/reload` 只是 `nginx -s reload`，**不物化**。所以 `reload()` 是三步：`check`（把库
构出的配置写 temp 跑 `nginx -t`，不碰真实文件）→ `replace` → `reload`，任一步失败就停在原位。
漏了 `replace` 的症状最难查：库里站点、location、后端全对，`conf.d` 里却没有文件，请求被别的
站点接走，而日志照样打"reload 完成"。

> `replace` 是**全面板重建**：`conf.d` 整目录按库重写，`nginx.conf` 里那份逐条
> `include conf.d/xxx.conf;` 的清单也一起重写。两条推论，都是硬约束：
> 1. **绝不可手工编辑 nginx 的任何文件**。手改的内容会在下一次任何人（包括用户自己点 UI 保存）
>    触发 `replace` 时被无声打回库里的样子。
> 2. 库里任何一处与磁盘不一致（drift）都是定时炸弹。跑 `replace` 之前必须核对：把库里的
>    location 逐条与磁盘上对应 conf 的 `proxy_set_header Host` 比对；有漂移就先**走面板**把库
>    对齐（绝不动文件系统），否则这次重建会把别人正在跑的服务打回坏状态。

### 5.5 首发验证顺序

控制台自身就是第一个实例：`dshctl.example.com`。部署时按 DNS → frp → NG → 探活的顺序创建，
每一步都先 dry_run 打印将要发出的请求，再真做，做完立刻回读核对（`list_configs` 里能查到
自己的隧道、`DescribeDomainRecords` 里能查到自己的记录）。**这是唯一允许对 frp/NG/DNS
写数据的场景，作用域只有这一条新对象。**

## 6. 编排步骤

### 6.1 新建实例 `deploy`

| seq | key | 做什么 | 失败怎么办 |
|---|---|---|---|
| 10 | `plan` | 生成域名标签（§6.1.1），查 DNS 与库确认未被占用 | 直接失败，无副作用 |
| 20 | `host_prepare` | SSH 检查主机可达、数据盘剩余空间、dsh-family kit 在位，**并检查 `image_tag` 指向的镜像在本机存在**（`docker image inspect`）；不在就失败并提示用 `cli images ship` 传过来。本步骤**永不构建镜像** | 无副作用 |
| 30 | `dns_create` | 加 A 记录，回读拿 RecordId | 删掉刚建的记录 |
| 40 | `frp_create` | 建隧道（remotePort 从 4893 起递增，先探测 VPS 上未被占用） | 删刚建的隧道 |
| 50 | `ng_create` | 建 upstream + 反代站点（location：`$host` + websocket） | 删刚建的站点/upstream |
| 60 | `container_deploy` | SSH 跑 `new-instance.sh` + `deploy.sh`，传 `DSH_IMAGE`（选定的镜像） `PUBLIC_URL`/`TRUSTED_HOSTS` | 保留现场，标 error |
| 70 | `token_capture` | 从容器日志抓一键地址 | 标 error，可重试 |
| 80 | `probe_public` | 公网探测：首页 200 + 无 token 打 `/api/remote.mux` 期望 401（403 说明反代 Host 又错了） | 标 degraded，给出修正提示 |
| 90 | `verify` | 跑 kit 的 `verify.sh <slug>`，收条目数 | 记录，不推翻成功 |

每步把命令、耗时、stdout 摘要写进 `deploy_steps`，前端按 seq 逐条点亮。

#### 6.1.1 域名与命名规则（全部程序生成，用户不输入）

`slug` 就是域名标签，同时当释放时的二次确认词，三处必须一致：

- 形态：`dsh-NN`（两位零填充，`dsh-01`、`dsh-02`…），完整域名 `dsh-NN.example.com`。
  只能是**一级**子域：现成证书 `CN=*.example.com`、SAN 只有 `*.example.com` 与 `example.com`，
  通配符只覆盖一层，两级名字会证书不覆盖。本控制台**不申请自己的证书**，只复用 NG 里
  acme.sh 自动续期那张。
- **不允许用户输入或修改标签**。新建流程里标签由服务端按 `dsh-` + 现有实例最大序号 + 1
  生成，创建后不可改（域名写进 DSH 的 `PUBLIC_URL` 与 `TRUSTED_HOSTS`，改名等于重开一个）。
- 唯一性：`instances.slug` 唯一 + `dns.find(rr)` 为空 + VPS 上公网端口未占用，三者都过才继续。
- 主机也由服务端选：`enabled` 主机里 `data_root` 剩余空间最大的那台（机械盘优先，
  见 `DSHC_PREFER_HDD`），新建确认页只做展示，允许用一次点击换另一台（不输入）。
- 给谁用的名字（`owner_label`）**不在新建流程里问**。它是事后可选的备注，在列表或详情页
  点铅笔就地改，改它不影响域名、容器名或任何外部对象。
- 派生名一律从 slug 出：容器名 `dsh-NN`、隧道名 `dsh_NN`（点换下划线）、NG upstream
  `DSH_NN`、NG 站点 `server_name` 用完整域名。
- 历史遗留的 `legacy-door` 不参与推导也不改名，它由人工拆除（站点私有手册（不随仓库发布） §2）。

#### 6.1.2 镜像是准备好的，不是现场构建的

镜像生命周期与实例生命周期分开：

- **构建**发生在 kit 仓库里（`dsh-family/scripts/build.sh`），由人触发，不在本控制台。
- **传输**用 `cli images ship --tag T --from <host> --to <host>`：控制台在两台主机之间
  流式搬运（源机 `docker save` 的 stdout 接目标机 `docker load` 的 stdin），
  不落盘、不要求两台机器互相配信任——控制台已经有两边的 SSH 凭据，借一道。
- **使用**：开通时镜像标签来自站点配置 `dsh_image`（默认 `dsh-family:local`），
  代码里不许出现任何具体标签。第 20 步检查它在本机存在，不存在就失败。
- **多版本**：本期**不做任何版本选择的交互**（用户明确：只要架构支持，先固定默认版本）。
  架构留的就是 `instances.image_tag` 列与"标签来自配置不是代码常量"这两件事。
  需求来了再把候选做成"目标机上 `docker images` 实际有哪些标签"，在新建确认卡上做成一次点击的选项，
  仍然不输入；实例行上记 `image_tag`，
  详情页显示"这个实例跑在哪个镜像上"。升级是"用新标签重新部署一个实例并迁移数据"，
  不是把老容器就地换镜像——首期不做，但表结构已经留了 `image_tag` 与 `config_version`。

### 6.2 释放 `release`（二次确认后）

顺序必须是先断外网入口再停容器：`probe_guard`（确认 slug 对得上）→ `ng_delete` →
`frp_delete` → `dns_delete` → `container_stop`（**只 stop，不 rm，不动数据目录，不动镜像**）
→ `mark_released`。每步都记「删掉的对象的 id 与名字」进 `events`，出事能查到删了什么。

### 6.3 重启 `restart`

`container_restart` → `wait_healthy`（最多 120 秒）→ `token_capture` → `probe_public`。

## 7. 部署形态

单容器 `dsh-control`：FastAPI 直接 serve 前端构建产物（`frontend/dist` 打进镜像），
一个进程同时给 API 和静态页，避免跨域与第二个服务。监听 `DSHC_LISTEN_PORT=13300`。

- 连 PG：加入 `tierai-prod_default` 网络，`DSHC_DATABASE_URL=postgresql+asyncpg://tierai:…@198.51.100.6:5432/dsh_control`
- 出公网：`dshctl.example.com` → NG → frps → `192.0.2.10:13300`
- 迁到 .8：换一份 `.env`（PG 地址、监听端口、frpc 客户端、数据根目录）重起即可，
  代码里不允许出现任何写死的机器地址，全部走环境变量或 `hosts` 表。

## 8. 环境变量

```
DSHC_DATABASE_URL            必填
DSHC_SESSION_SECRET          必填，cookie 签名密钥
DSHC_LISTEN_HOST/PORT        0.0.0.0 / 13300
DSHC_SESSION_DAYS            默认 7
DSHC_INGRESS_IP              203.0.113.10
DSHC_DNS_ZONE                example.com
DSHC_DNS_PREFIX              dsh-
DSHC_PUBLIC_BASE             https://dshctl.example.com
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

**交互做减法**（用户原话：有选择困难症，别让他输入这啊那啊）。落到界面上是硬要求：

- 全系统只有两处需要用户输入：登录页粘贴密钥、给实例改备注名（且后者是点铅笔之后的
  就地编辑，不做也是完整可用的）。新建、释放、重启、探活都不许要求打字。
- 新建只有一个动作：点「新建」→ 一张自动填好的确认卡（域名、主机、资源、数据盘、
  公网端口，全部程序生成，写清楚是自动生成）→ 点「开始部署」。要换主机是卡片上
  一次点击的事，不是表单。
- 释放的二次确认用**按住确认**（长按 1.5 秒才触发，松手取消），配合后果清单：会删解析、
  删隧道、删 nginx 站点，容器只停不删，数据保留，以后不能恢复。服务端仍然要求
  `confirm == slug`，由前端填。
- 危险与不可逆动作不用弹窗套弹窗，一屏内把后果说完。
- 不显示用户用不上的字段（镜像 id、内部状态机原始值这类收进次要位置或不显示）。
