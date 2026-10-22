# dsh-control 架构与接口契约

给一群分散在多台机器 Docker 里的 DSH（DeepSeek Harness）实例做开通、状态查看、
重启、回收的运维主控台。非官方第三方工具，不含也不依赖 DSH 源码。
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

库：独立的 PostgreSQL 数据库 `dsh_control`。可以和别的项目共用一个 PG 实例
（容器加入该实例所在的 Docker 网络即可），但不要共用库——本控制台的库属于
最高敏感级，里面集中存着各实例的登录密钥。

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
软删不用（实例的可见性只有业务状态：`released` 是可恢复的停放态，`purged` 是保留
审计记录的终态，都不是软删标记）；金额/字节用 `bigint`；
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
  upstream_mode(frp|direct),                # 公网回源方式：frp=走隧道（默认）；direct=本机公网直连（§6.1.4）
  mem_limit, cpu_limit,                     # 该机实例的资源配额（如 "1g"/"2"）；留空=kit 默认（4g/4核）。共享机必填，防 OOM 伤及同机服务
  direct_backend,                           # direct 主机 NG 回源实际拨的地址；留空=ssh_host。云轻量服务器（SWAS 等）EIP 是云端 NAT 映射不在网卡上，入口机=本机时须登记网卡实址；兼作实例 HOST_BIND_IP 绑定址（§6.1.4）
  llm_base_url,                             # 该机实例的模型入口覆盖；留空=站点 llm_base_url（局域网网关地址，云上机器拨不到）。只换地址不换密钥
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
  state(pending|deploying|running|unhealthy|restarting|released|purged|error),
  desired_state(running|released),
  last_error, released_at, purged_at, created_at, updated_at

instance_tokens    登录密钥历史，详情页永远取最新一条
  id, instance_id, token, url, source(bootstrap|logs|restart|manual), captured_at

instance_configs   部署时实际下发的配置快照，详情弹窗数据源
  id, instance_id, version, rendered jsonb, created_at

instance_snapshots 采集到的运行态
  id, instance_id, captured_at, docker_state, health, restart_count,
  cpu_pct, mem_usage_bytes, mem_limit_bytes, raw jsonb

deploy_jobs        一次性任务：deploy / restart / release / purge / publish
  id, instance_id, kind, state(pending|running|succeeded|failed),
  params jsonb,               # 工单附加参数：{"image": 升级目标tag} / {"purpose": create|upgrade|restore|source: auto|manual}
  error, started_at, finished_at

deploy_steps       点对点进度，前端按 seq 渲染
  id, job_id, seq, key, title, state(pending|running|succeeded|failed|skipped),
  detail, started_at, finished_at

events             审计与自动重启记录
  id, kind, instance_id NULL, job_id NULL, message, data jsonb, created_at

image_artifacts    镜像制品库（§6.1.5）：一行一个 tag，行存在即文件已完整落库
  id, tag UNIQUE, filename, size_bytes, sha256,
  token UNIQUE,             # 公开链接的凭证（随机 26 字符，撤销=删行删文件）
  origin_host, created_at   # origin_host: 从哪台机 save 的；空串=手工投放

image_channels     制品的外部分发渠道（§6.1.6）：一制品一渠道一行，没有行=没推过
  id, artifact_id, channel(github|gitee), state(pending|ok|failed),
  url,                      # 目标机可直连的入口：github=asset 直链；gitee=manifest 直链
  release_tag, error, updated_at
  UNIQUE(artifact_id, channel)
```

分发渠道的令牌是 `credentials(kind=mirror_token)`（name=github/gitee）：**只写不回显**
（API 任何出口都只给 `token_set: true/false`）——与运维密钥的「明文可读回」刻意不同：
那是自己的门，这是第三方的门，泄露面多一处都是白送。

上面每个 `id` 类关联列都只建普通索引，不建外键；`instance_id`/`job_id`/`host_id`/`ssh_credential_id` 的取值合法性由仓储层在写入前校验。

## 3. 实例状态机

```
pending → deploying → running ⇄ unhealthy → restarting → running
                        ↓                        ↑（连续 3 次健康检查失败，自动）
                     released ← releasing
                     ↕（续建恢复，§6.1.3）
                     │（保留期满或手动，purge 工单，§6.4）
                     ↓
                   purged（终态）
```

- `restarting` 由 watcher 自动触发或用户在详情页点重启；重启必然轮换登录 token，
  重启完成后必须重新抓 token 并写 `instance_tokens` + 记 `events`。
- 自动重启的三个抑制条件：连续失败 < 3 次不重启；备份窗口内不重启（默认 03:20–04:00，
  可配 `DSHC_BACKUP_WINDOW`）；距上次自动重启 < 10 分钟不重启（`DSHC_RESTART_COOLDOWN`）。
- `released` **不再是终态**：保留期内可用 deploy 续建恢复（§6.1.3）；保留期满
  （`DSHC_PURGE_AFTER_DAYS`，默认 15 天；site.toml `[site].purge_after_days` 落库可改，
  watcher 每轮现读最新值）后 watcher 自动入队一个 purge 工单（§6.4），purge 成功才进入
  真正的终态 `purged`——列表仍可见该记录，标「已彻底释放」，只读，任何写操作拒绝。
- **token 落库与事件一律去重**：任何路径（watcher 自动重抓、restart/deploy 工单、
  详情页 reveal）抓到一个 token 后，先与该实例库里最新一条比对：**值没变就什么都不写**
  ——不落 `instance_tokens`、不记 `events`，reveal 响应照常返回现取值；值变了才落库并记
  一条 `token_captured` 事件。watcher 原有的自动重抓场景保留（restart_count 增长、
  部署后补缺），但按上面的规矩静默；这修掉了「事件流被获取密码刷满」的问题。

## 4. HTTP API

除 `/api/login`、`/api/healthz` 与公开下载前缀 `/dl/`（§6.1.5，token 即门票）外全部要鉴权。写操作要求头 `X-Requested-With: 1`
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
| POST | `/api/instances` | 请求体 `{confirm}`（必填）或 `{host_id?, confirm}`（不传 host_id 由服务端按剩余空间选机）→ `{instance_id, dns_name, job_id}`；slug 与端口一律服务端生成（§6.1.1）。`confirm` 必须逐字等于**服务端刚算出的 slug**——防"连点两次凭空开出公网实例"（2026-10-21 现网事故）；值由前端从确认卡回显，用户零打字（§9） |
| GET | `/api/instances/{id}` | 详情，见下 |
| GET | `/api/instances/{id}/token` | **现取**：SSH 抓容器日志里的最新一键地址返回；与库中最新值相同则不落库不记事件（§3 去重），变了才落库 + 记事件 |
| POST | `/api/instances/{id}/deploy` | 续建统一入口：pending/error（开通失败重试）、running（**升级/切镜像**，§6.1）、released（**恢复**，§6.1.3）都可重跑；body 可选 `{"image": "目标tag"}`（API/脚本文档用，留空=站点当前目标 tag）。purged 拒绝 |
| POST | `/api/instances/{id}/restart` | → `{job_id}` |
| POST | `/api/instances/{id}/release` | `{confirm}` 必须逐字等于 `slug`，否则 422 → `{job_id}`。**这个值由前端自己填**（前端知道 slug），用户不需要打字；防误操作靠 §9 的按住式确认，不靠输入 |
| POST | `/api/instances/{id}/purge` | **彻底释放**：`{confirm}` 逐字等于 `slug`（同样前端填，交互用 §9 长按确认）→ `{job_id}`。仅 released 可发起（§6.4） |
| GET | `/api/jobs/{id}` | `{kind,purpose,state,steps:[{seq,key,title,state,detail}]}`。`purpose` 仅 deploy 有值：入队时服务端按当时状态写进 `params.purpose`——`create`（新建）/ `upgrade`（running 续建）/ `restore`（released 恢复），前端据此出标题与完成文案，不靠 URL 猜 |
| GET | `/api/jobs/{id}/logs` | 增量日志：`?after=<id>` → `{items:[{id,seq,level,line,ts}], next}` |
| GET | `/api/jobs/{id}/stream` | SSE，`step` / `log` / `done` 三种事件 |
| GET | `/api/instances/{id}/events` | 事件时间线 |
| GET | `/api/settings` | 面板运行设置（§6.1.5）：`{image_ship: {mode, base_url}}`；`base_url` 空串=吃 site 的 `public_base`，返回的是解析后的生效值 |
| PUT | `/api/settings` | 局部更新（只认已知键）：`mode` 非 `ssh|download` → 422；写进 site 凭据 secret 的 `image_ship`，deploy 每轮现读，改完不重启生效。可扩展键：`mirror_order`（`gitee/github/console` 的排列，探测优先序，§6.1.6）、`auto_mirror`（发布是否顺道推外部渠道，默认 true）；`mirrors.{channel}.repo` 也在 PUT 白名单（如 `lt9/dsh-control`） |
| GET | `/api/settings` 的 `mirrors` 段 | 每渠道 `{repo, token_set, state?}`——**token 永不出网**（§6.1.6） |
| PUT | `/api/mirror-tokens/{channel}` | `{token}` → upsert `credentials(kind=mirror_token)`；channel 非 github/gitee → 422；写后任何 GET 只回 `token_set`（§6.1.6） |
| GET | `/api/images` | 制品列表：`{items:[{id,tag,size_bytes,sha256,token,url,origin_host,created_at}], store_free_bytes}`；`url` = 解析后的 base_url + `/dl/{token}/{filename}`；`store_free_bytes` null=制品库不可用（未挂载/不可写），前端可不显示该栏，可用性点名留给发布/搬运失败 |
| POST | `/api/images` | `{tag, from_host_id}` → `{job_id}`（kind=publish，§6.1.5）。同 tag 重发=原子替换，旧链接立死 |
| DELETE | `/api/images/{id}` | 下架：删行删文件，链接即刻失效 |
| GET | `/dl/{token}/{filename}` | **公开免登录**流式下载（§6.1.5）；支持单区间 `Range` 续传；token 不认识 404；`filename` 只做展示名，认 token 不认它 |

详情响应（前端所有展示字段都在这一个响应里，避免多轮请求）：

```json
{
  "id": 1, "slug": "dsh-02", "owner_label": "家人", "host": {"id":1,"name":"nas-1"},
  "state": "running", "desired_state": "running",
  "dsh_version": "1.x.y", "image_tag": "dsh-family:local", "image_id": "5023c780",
  "config_version": "v1",
  "config": {"items": [{"key":"documentsDirectory","value":"/data","label":"工作区根目录",
                       "help":"DSH 用它拼默认工作区，容器里没有 xdg-user-dir，必须显式配"}]},
  "network": {"dns_name":"dsh-02.example.com","public_url":"https://dsh-02.example.com",
              "frp_remote_port":20001,"frp_proxy_id":575,"dns_record_id":"2112...","container_ip":"192.0.2.5"},
  "runtime": {"docker_state":"running","health":"healthy","restart_count":0,
              "cpu_pct":0.4,"mem_usage_bytes":280000000,"mem_limit_bytes":4294967296,
              "captured_at":"2026-10-20T21:40:00+08:00"},
  "access": {"has_token": true, "token_masked": "M1jq…IV0",
             "captured_at":"2026-10-20T21:37:36+08:00"},
  "last_error": null, "released_at": null, "purged_at": null,
  "purge_after": null,
  "actions": {"restart": true, "release": true, "upgrade": true,
              "restore": false, "purge": false, "deploy": false}
}
```

`actions.deploy` 是「开通失败重试」的页面按钮口径（pending/error 才 true），与
upgrade/restore 三个互不重叠；列表行携带与详情相同的
`state/purged_at/purge_after/actions` 子集（列表接口连 mask 都不给，规矩不变）。
列表次要分组里的时间列：released 行显示 `released_at`，purged 行显示 `purged_at`
（悬停注明「彻底释放于…」）——语义都是「这台什么时候没的」。

`purge_after` 仅在 `state=released` 时给出（= `released_at` + 当前保留期，渲染口径
与全站时间字段一致：`+08:00`），前端据此显示「保留期还剩 N 天，YYYY-MM-DD 将自动彻底释放」；
其余状态为 null。
`actions.restore`=可恢复（released 未 purge），`actions.purge`=可彻底释放，
`actions.upgrade`=running/unhealthy 可用「升级/重建」按钮走 deploy。

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
| `confirm_mismatch` | 422 | 释放/彻底释放的确认词与 slug 不一致（确认词由前端填，出现即前后端数据不同步） | 页面数据过期了，刷新后再试 |
| `already_released` | 409 | 重复释放 | 已经释放过了 |
| `cannot_deploy_from_<state>` | 409 | 从 purged/deploying 等不可续建状态发 deploy | 当前状态不能重新开通 |
| `cannot_purge_from_<state>` | 409 | 从非 released 状态发起彻底释放（purged 也在其列） | 只有已释放的实例能彻底释放 |
| `invalid_mode` | 422 | `PUT /api/settings` 的 `image_ship.mode` 不是 `ssh|download` | 分发方式只有两种，页面数据错了 |
| `invalid_mirror_order` | 422 | `PUT /api/settings` 的 `mirror_order` 不是 `gitee/github/console` 的去重非空排列/子集 | 探测优先序只能是这三条渠道 |
| `invalid_repo` | 422 | `mirrors.{ch}.repo` 不是 `owner/repo` 形（空串=清掉配置） | 仓库名写错了格式 |
| `invalid_channel` | 422 | `PUT /api/mirror-tokens/{channel}` 渠道不是 `github|gitee` | 外部渠道只有这两个 |
| `invalid_token` | 422 | 同上接口，token 是空白 | 令牌栏还空着 |

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
`/api/nginx/reload` 只是 `nginx -s reload`，**不物化**。所以 `reload()` 是四步：软预检
`check` → `replace`（按库落盘）→ 硬闸 `check` → `reload`，任一步失败就停在原位。

**`check` 不能排在 `replace` 前面当闸门**（2026-10-21 现网实测，此前把它当闸门，两次真实
开通都卡在这）：面板 check 把库拼进临时主配置，而主配置里 `include conf.d/upstreams.X.conf`
指的是**磁盘**文件；新建对象只有 `replace` 之后才有文件。所以「库里多一条还没落盘的对象」
必然让 check 失败——用「只建一条 upstream → check 失败 → 删掉 → check 恢复」的可逆实验坐实。
于是预检只提醒不拦（拿它当闸 = 一次遗留脏库把所有开通永久卡死），硬闸移到 `replace` 之后：
校验不过就不 `reload`，nginx 仍跑内存里的旧配置，用户正在服务的东西不受影响，回退走面板
「备份恢复」（面板每次操作都自动快照进 sqlite 的 `bak` 表）。
漏了 `replace` 的症状最难查：库里站点、location、后端全对，`conf.d` 里却没有文件，请求被别的
站点接走，而日志照样打"reload 完成"。

> `replace` 是**全面板重建**：`conf.d` 整目录按库重写，`nginx.conf` 里那份逐条
> `include conf.d/xxx.conf;` 的清单也一起重写。三条推论，都是硬约束：
> 1. **绝不可手工编辑 nginx 的任何文件**。手改的内容会在下一次任何人（包括用户自己点 UI 保存）
>    触发 `replace` 时被无声打回库里的样子。
> 2. 库里任何一处与磁盘不一致（drift）都是定时炸弹。跑 `replace` 之前必须核对：把库里的
>    location 逐条与磁盘上对应 conf 的 `proxy_set_header Host` 比对；有漂移就先**走面板**把库
>    对齐（绝不动文件系统），否则这次重建会把别人正在跑的服务打回坏状态。
> 3. **红线：库里没有的 conf 文件会被 `replace` 直接删掉**——任何绕过面板塞进 `conf.d` 的
>    文件（手工加的 server 块、别的工具放的配置）在一次重建后无声消失。往这台面板上加东西，
>    只有走它自己的库一条路。
>
> **控制台没有读磁盘 conf 的能力**：这版 nginxWebUI 不提供任何读配置文件内容的接口
> （`getFileContent` / `getFile` / `fileContent` / `confContent` 实测全 404）。真正能证明
> 库盘一致的做法只有**运维侧只读比对 conf.d 与库值**（做法与最近一次实测见
> `docs/05-故障排查.md`）。`drift_scan` 只是 replace 前的黑盒旁证：HTTP 状态码仅在后端
> 可证明是 DSH 时才是 Host 证据（登记名单或 §4.1 信封指纹），证明不了一律
> `inconclusive`——非 DSH 后端（jellyfin/Gogs/onlyOffice…）的 200/401/403/502 一概不是
> 证据，拿它们判 Host 会给出假警报或假安全感（2026-10 实测翻案）。

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
| 20 | `host_prepare` | SSH 检查主机可达、数据盘剩余空间、dsh-family kit 在位，**并检查 `image_tag` 指向的镜像在本机存在**（`docker image inspect`）；不在就按设置的分发方式自动搬运（§6.1.5），搬运失败才拦。本步骤**永不构建镜像** | 镜像缺失时会在目标机 load 一份 |
| 30 | `dns_create` | 加 A 记录，回读拿 RecordId | 删掉刚建的记录 |
| 40 | `frp_create` | 建隧道（remotePort 从 frp_port_start 起递增，先探测 VPS 上未被占用）；**direct 主机整步 skipped**（§6.1.4） | 删刚建的隧道 |
| 50 | `ng_create` | 建 upstream + 反代站点（location：`$host` + websocket）；upstream 后端：frp 主机=入口机 `ingress_ip:公网端口`，direct 主机=`hosts.direct_backend`（留空=`ssh_host`）`:公网端口`（§6.1.4） | 删刚建的站点/upstream |
| 60 | `container_deploy` | SSH 跑 `new-instance.sh` + `deploy.sh`，传 `DSH_IMAGE`（选定的镜像） `PUBLIC_URL`/`TRUSTED_HOSTS`；`hosts.mem_limit`/`cpu_limit` 有值时改写实例 `.env` 的 `MEM_LIMIT`/`CPU_LIMIT`（共享机的护栏，§2）；`direct_backend` 有值时改写 `HOST_BIND_IP`、`hosts.llm_base_url` 有值时改写 `LLM_BASE_URL`（§6.1.4） | 保留现场，标 error |
| 65 | `config_backfill` | 从实例 `.env` 与 docker 端口映射读回**生效值**，覆盖第 10/20 步按猜测写的配置快照 | 读不到不失败，快照自标来源 |
| 66 | `capabilities` | 铺生图技能与智能体团队（站点没配生图就跳过并记一行警告），并读回 DSH 生效配置 | 缺材料跳过，不挡住开通 |
| 67 | `fonts` | **铺字体**：把站点字体库（`font_library`）里的文件 `cp -n` 进实例 DATA_DIR 的 `home/.fonts`（容器内 `/data/home/.fonts`，Word→PDF 两条转换路共同认的投递口），落地后文件 644、目录 755。只 cp 不挂载，同名不覆盖。库未配置或宿主上目录不存在 → 记 warning 并跳过，不失败工单 | 记 warning 跳过（铺字体失败才标 error） |
| 70 | `token_capture` | 从容器日志抓一键地址 | 标 error，可重试 |
| 80 | `probe_public` | 公网探测：首页 200 + 无 token 打 `/api/remote.mux` 期望 401（403 说明反代 Host 又错了） | 标 degraded，给出修正提示 |
| 90 | `verify` | 跑 kit 的 `verify.sh <slug>`，收条目数 | 记录，不推翻成功 |

每步把命令、耗时、stdout 摘要写进 `deploy_steps`，前端按 seq 逐条点亮。

**从 running 续建（切镜像的正规路径）**：deploy 不只许 pending/error 重跑，running
实例也允许 `POST /instances/{id}/deploy`——每一步都是「已存在且一致就跳过」的幂等
续建，容器那步发现实例 `.env` 的 `DSH_IMAGE` 与站点当前目标 tag 不一致时由 compose
重建容器（`docker restart` 做不到这件事：既不重读 `.env` 也不重建容器）。后果是
会话短暂被打断；登录密钥**不轮换**——cookie secret 在实例 DATA_DIR 的
`.credentials.yaml` 里，重建不动它，一键链接切完照用。详情页把这个入口做成
「升级/重建」按钮（§9）。released 的恢复见 §6.1.3；purged 拒绝一切续建。

**升级就是同实例原地重建**：compose 用新 tag 重建同名容器，DATA_DIR 原样挂回，
数据天然继承，**绝不允许出现"新建一个实例再迁数据"的双份实例路径**。
镜像来源仍守 §6.1.2——构建与搬运在镜像侧完成，工单第 20 步只验收目标 tag 在目标机存在。

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
  详情页显示"这个实例跑在哪个镜像上"。升级 = §6.1 的 running 续建（同实例原地重建，数据继承），
  目标默认站点当前 tag，deploy body 可用 `{image}` 指定。

### 6.1.4 直连主机（upstream_mode=direct）：公网机不过 frp

frp 的存在意义是 NAT 后面的机器；**本身有公网 IP 的主机不需要隧道**。hosts.upstream_mode=direct 时：

- 第 40 步 skipped（不建隧道，`frp_proxy_id` 留空；`frp_remote_port` 仍写——它就是
  实例在本机发布的公网端口，列名沿用不改）；
- 公网端口分配仍走 `frp_port_start` 起的同一个池，但**占用探测改在目标机上做**
  （`ss -ltn` 实探 + 库里在用端口），不再问 frp-panel；
- 第 50 步 NG upstream 后端 = `hosts.direct_backend:公网端口`，未登记时 =
  `hosts.ssh_host:公网端口`（NG 仍在入口机上，证书与 location 规则一字不变）；
- **云轻量服务器（SWAS 这类）的 EIP 陷阱**：公网 IP 是云端 NAT 映射，机器网卡上
  根本没有它——入口机恰好就是这台机时，NG 回源拨自家 EIP 会出网卡绕云端 NAT 发卡
  拐（hairpin 黑洞，2026-10-22 dsh-05 现网实测：TCP 都不完成）。这类主机必须登记
  `direct_backend`=网卡实址，第 60 步同时把它写进实例 `.env` 的 `HOST_BIND_IP`：
  端口只绑网卡实址，公网路由不到，**云防火墙一行都不用放行**（比防火墙红线更小
  的暴露面）。kit 的 `deploy.sh` 等端口要用 `HOST_BIND_IP` 而不是 127.0.0.1
  （绑非回环地址时 docker-proxy 不应答 loopback，现网实测连 900s ssh 超时都是它）；
- **防火墙红线**（未登记 `direct_backend` 的普通公网机）：实例端口只应对入口机
  （`DSHC_INGRESS_IP`）放行，不对全世界开放。云防火墙/安全组不由本控制台管理，
  第 50 步的 detail 会提醒这一条，运维在云上补；
- 按主机的模型入口：站点 `llm_base_url` 是局域网网关，云上机器拨不到；登记
  `hosts.llm_base_url` 后第 60 步用它改写实例 `.env` 的 `LLM_BASE_URL`（密钥不变，
  同一网关的两个门）；
- 资源红线：共享机（入口机兼跑实例这类场景）**必须**设 `hosts.mem_limit`/`cpu_limit`，
  kit 的 4g 默认值在挤机上会把同机基础设施送进 OOM。

### 6.1.5 镜像分发：SSH 直传 与 控制台制品下载

镜像 GB 级、目标机横跨局域网和公网两头，搬运路径不许写死。设置页存
`site.image_ship = {"mode": "ssh"|"download", "base_url": ""}`（进 site 凭据
secret；`base_url` 空=用 `public_base`）。deploy 每轮现读，改完不重启生效——
与 purge 保留期同一个模式。默认 `ssh`（现行为零改变）。

**SSH 直传**（局域网首选）：源机 `docker save` 的 stdout 经面板内存管道灌目标机
`docker load`（`images.py` 现成实现）。零落盘；两台之间不需要互信。跨公网慢且
两头都吃面板带宽——公网机应切 download。

**控制台制品下载**（公网机首选）：先把镜像发布成制品，目标机自己拉：

```
curl -fL --retry 3 '<url>' -o <文件> && echo '<sha256>  <文件>' | sha256sum -c - \
  && docker load -i <文件> && rm -f <文件>
```

下载源就是面板自己的公网域名（`base_url`，本站即控制台公网域，设置页可改）。
每个制品一条公开链接 `{base_url}/dl/{token}/{filename}`：

- **免鉴权，token（随机 26 字符 urlsafe）就是门票**。镜像非机密（跑到公网还得过
  实例自己的准入 token），泄链接=泄镜像，接受；撤销=设置页下架，链接立死。
- 文件 `<tag 安全化>.tar.gz` 存 `DSHC_IMAGE_STORE` 目录；只用 gzip——gzip 全员
  自带，不赌目标机装没装 zstd/xz。sha256 发布时算好，拉取时强制 `sha256sum -c`。
- 每台机从哪拉可以单独指定：`hosts.image_base_url`（登记值优先，空=设置页的
  base_url）。必填场景：入口机=目标机本身时，公网域名解析回自家 EIP 是 hairpin
  黑洞（§6.1.4 实测同款），登记一个够得着的入口——如 frps 隧道口
  `http://198.51.100.77:4893` 这类本机可达的门（与 NG 回源同路）。

发布（`POST /api/images` → 工单 `kind=publish`，2+2 步）：
`10 save`：源主机 `docker save <tag> | gzip -n` 流进制品库 `<file>.tmp`，按已落
字节回显进度（GB 级超时给足 1800s）；
`20 record`：算 sha256/size，改名去 `.tmp`，**文件落定才插行/换行**——表里的行
永远指向完整文件。同 tag 重发：新行落库成功后才删旧行旧文件。
`30 mirror_github` / `40 mirror_gitee`：§6.1.6。未配 token 或 `auto_mirror=false`
→ skipped（不拦 publish）；配了却失败 → 工单红（渠道状态落 `image_channels`，
修好重跑 publish 全部重来是幂等的）。

deploy 第 20 步缺镜像分支（替代旧「报错让人跑 CLI」）：
- `mode=download` 且该 tag 有制品 → 按 §6.1.6 探测选路后，目标机跑 curl 三连
  （gitee 是分卷版三连）；缓存放 `{data_root}/ship-cache/`，不赌 /tmp 是不是
  tmpfs；哪步挂了 StepFailed 写清哪步。
- `mode=ssh` → 在 enabled 主机里找有该镜像的（优先面板所在宿主机），流式直传后复查。
- 没制品/没源机 → StepFailed：点名去设置页发布该 tag 的制品或切回 SSH 直传，
  并附目标机现有 dsh 镜像列表。

前置：目标机有 `curl`（kit 的 verify.sh 本来就依赖它）；`download` 模式还要
`DSHC_IMAGE_STORE` 挂得出可写目录，发布/搬运遇不可写直接报错，不静默降级。

### 6.1.6 制品多路分发：控制台 / GitHub Releases / Gitee Releases

公网机从控制台拉制品要绕「自家出口→入口机→隧道→控制台」一整圈，还吃控制台带宽。
把制品镜像到公共代码托管平台的 Releases，就白嫖它们的 CDN——**匿名 curl 直链、
无需 token**（GitHub Packages 排除：下载普遍要鉴权、按包型设计，不是给 curl 直取
准备的）。本站仓库全公开，制品上公共渠道不新增泄露面（镜像本就公开可取）。

**渠道与格式**：
- **github**：一个制品一个 release（tag=`image-<tag安全化>`，body 写明是面板自动
  发布的镜像制品与 sha256），asset=完整 `<file>.tar.gz`。历史 release 保留不限。
  API：`POST /repos/{repo}/releases` + uploads 域名挂 asset。
- **gitee**：国内机快路。**硬限制：单附件 ≤100M、仓库附件总量 ≤1G**（官方口径），
  所以制品**分卷**：`split -b 95m` 出 `.part-aa…`，加一个 `<file>.manifest.json`
  （file/sha256/size/parts[{name,size,sha256,url}]）。发布新版本时先传新（两版并
  存 814M<1G 刚好过）再删旧 tag 的 release——仓库永远只有最新一版。gitee 渠道行
  的 `url` 指向 manifest。
- **console**：§6.1.5 的 `/dl/{token}/`，永远兜底。

**三条落地细则**（实现拍板，契约背书）：同 tag 重发先把该制品的旧渠道行打回
`pending`——制品换货了，外部副本还是旧的，留着 `ok` 会让第 20 步拉到上一版；
GitHub 同 tag 内同名 asset 先删旧再传新（幂等重跑不叠 asset，「历史保留」指的是
别的 tag）；gitee 删旧版的触发口径 = 同渠道更早的 `ok` 行（覆盖同制品重发与
新制品占位两种来路），删不动只 warn——1G 配额是软约束，不卡发布主流程。

**探测选路（在目标机上做，不在面板上）**：面板装了梯子≠目标机有梯子，探测结果
没有参考价值。download 模式缺镜像时，按 `mirror_order`（默认 gitee→github→console，
= 国内优先 gitee、国外机器 gitee 探测不通自然滑向 github）对**该制品状态 ok 的
渠道**逐个在目标机探测：

```
curl -fsL --max-time 8 -r 0-0 -o /dev/null '<入口 url>' && echo OK
```

第一个 OK 的渠道胜出；取货脚本按渠道分支（github/console=整发三连；gitee=下
manifest→按序拉全部 parts→`cat` 拼回→**整包 sha256 强校验**→load→删缓存）。
全败 → StepFailed 逐渠道点名探测结果。探测与取货全程用目标机自己的网络。

**令牌**：`credentials(kind=mirror_token)`，UI 写入（§4），**只写不回显**；
删除渠道 = 把 token 抹掉 + 对应 release 留着无妨（公开仓库，可再手动删）。

### 6.2 释放 `release`（二次确认后）

顺序必须是先断外网入口再停容器：`probe_guard`（确认 slug 对得上）→ `ng_delete` →
`frp_delete` → `dns_delete` → `container_stop`（**只 stop，不 rm，不动数据目录，不动镜像**）
→ `mark_released`。每步都记「删掉的对象的 id 与名字」进 `events`，出事能查到删了什么。

### 6.3 重启 `restart`

`container_restart` → `wait_healthy`（最多 120 秒）→ `token_capture` → `probe_public`。

### 6.3.1 恢复 `released` → 运行（走 deploy，不另立工单）

released 实例在 purge 之前的恢复 = 对同一实例重跑 deploy 工单（§6.1 续建语义，
`POST /deploy`，`actions.restore` 控制按钮出现）：DNS/frp/NG 三处都是**重建**
（释放时删的是我们自己建的对象，名字仍从 slug 派生，天然还是原来那套域名与
upstream 名），容器步对既有的停止容器执行 `compose up`——**同一 DATA_DIR、同一
容器名，数据天然继承，不存在第二个实例**。恢复后按 §3 走 deploy 全流程回 running：
plan 步把 `desired_state` 翻回 `running`——不翻的话恢复完 watcher 不守护。

### 6.4 彻底释放 `purge`（released 的终态化）

purge 工单**只碰本控制台创建且登记在案的对象**，顺序与 release 对称——先补删
释放时删掉一半的外部对象（若还挂着 id），再删容器，最后删数据目录：

| seq | key | 做什么 | 失败怎么办 |
|---|---|---|---|
| 10 | `confirm_guard` | 手动工单核对确认词等于 slug；自动工单核对 `source=auto` 且保留期确实已过（每轮现读配置） | 直接失败，无副作用 |
| 20 | `external_cleanup` | 库里还挂着 dns/frp/ng id 就逐个补删（对象多半早在释放时删了，404 视为已删） | 记 error，可重跑 |
| 30 | `container_remove` | `docker rm` 容器（还活着先 stop）；镜像不动 | 记 error，可重跑 |
| 40 | `data_delete` | **删数据目录**：路径只认实例 `.env` 读回的生效 `DATA_DIR`，且必须严格位于该机 `hosts.data_root` 之下、非 data_root 本身——不满足宁可不删直接失败；满足则 `rm -rf` | 宁可不删，绝不删错 |
| 50 | `mark_purged` | `state=purged`，`purged_at=now`，清掉外部 id 字段（released 时特意保留的 `frp_remote_port` 一并清——端口回池，终态不再赎回），记 `instance_purged` 事件 | — |

- 触发两种：**自动**（watcher：`released_at + purge_after_days < now` 入队，
  `purge_after_days` 每轮现读库里 site 行，改了 DB 不需重启）；**手动**（详情页
  长按确认，`confirm=slug` 前端填）。
- purge 成功后记录与历史（工单、事件、密钥历史、配置快照）**保留**——列表与详情
  仍可看，全部只读；容器、数据目录、外部对象三样都没了。purged 不可恢复，要再用就新建。
  `desired_state` 保持 `released` 不变——desired 只表达"要不要跑着"，没有第三种愿望。
- 数据目录是**用户数据**，删之前不留最后快照是用户明确的选择（默认保留期 15 天就是
  反悔窗口）；本控制台不在 purge 里夹带备份动作。

## 7. 部署形态

单容器 `dsh-control`：FastAPI 直接 serve 前端构建产物（`frontend/dist` 打进镜像），
一个进程同时给 API 和静态页，避免跨域与第二个服务。监听 `DSHC_LISTEN_PORT=13300`。

- 连 PG：加入 PostgreSQL 所在的 Docker 网络，`DSHC_DATABASE_URL=postgresql+asyncpg://USER:…@DB_HOST:5432/dsh_control`
- 出公网：`console.example.com` → NG → frps → `192.0.2.10:13300`
- 制品库：挂一个目录 `DSHC_IMAGE_STORE`（如 `-v /srv/dsh-console/images:/var/lib/dsh-control/images`）。
  不挂就没有 download 模式（发布直接报「制品库未挂载」），SSH 直传不受影响
- 换宿主机：换一份 `.env`（PG 地址、监听端口、frpc 客户端、数据根目录）重起即可，
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
DSHC_FONT_LIBRARY            站点字体库目录（实例宿主机上的绝对路径；留空 = 开通第 67 步铺字体跳过）
DSHC_IMAGE_STORE             镜像制品库目录（§6.1.5），默认 /var/lib/dsh-control/images；未挂载/不可写=download 模式禁用
DSHC_PURGE_AFTER_DAYS        released 保留天数，默认 15；到期 watcher 自动 purge（§6.4）。库里的值优先（seed 可改，watcher 每轮现读）
DSHC_DRY_RUN                 1 = 所有外部写操作只打印
```

`python -m dsh_control.cli init` 建表、灌种子（两台主机、v1 配置版本、把上面这些外部
凭据读进 `credentials`、生成运维密钥并**只在本次 stdout 打印一次**，库里同时存一份明文
以便日后读回）。

## 9. 前端

`frontend/` React + Vite + TS，设计走 `/impeccable`，模式 **Operate**（运维者来完成动作，
扫读效率优先，不做营销式花活）。五个界面：

1. 登录页：一个密钥输入框 + 粘贴即可，不要求用户名。
2. 列表页：搜索框（域名/用途都能搜）+ 新建按钮 + 表格：用途与 slug、状态、DSH 版本、
   配置版本、所在主机、域名、更新时间。`released` 与 `purged` 默认折叠在次要分组里
   （purged 额外标「已彻底释放」）。
3. 详情页：运行态与网络信息；配置卡底部一个小「？」开弹窗逐条解释每个配置项；
   一键登录 URL 与 token（一键复制，复制后给明确反馈）；操作区重启 + 释放（输入 slug
   原文才能点，二次确认）；异常时给最近事件与错误。
4. 新建向导：选主机 → 填用途标签 → 自动出域名前缀（可改）→ 确认 → 逐步进度条 + 实时日志
   → 完成后给出可复制的一键地址并高亮。
5. 设置页：镜像分发方式单选（SSH 直传=局域网快路 / 控制台下载=公网机自取，各配一句
   适用说明）+ 下载基址（留空=用站点公网域）+ 制品表（tag、大小、时间、sha256 短码、
   公开链接一键复制、长按下架——下架是删文件，照 §9 长按确认）+「从主机发布」表单
   （源主机下拉 × tag 输入——全站唯一要打字的地方，datalist 给出库内已有 tag 兜底），发布进度点进常规工单页。下架不加 HTTP 层确认词：误操作防线是 UI 长按，接口层已有 CSRF 头与单运维密钥。
   多路分发区（§6.1.6）：渠道表（控制台/GitHub/Gitee：仓库名可改、token 状态灯只显
   「已配置/未配置」、最新制品在该渠道的状态与直链复制）、探测优先序的上下调、
   token 输入框写完即焚（保存后清空，永不回填）。Gitee 行挂 ? 讲分卷与只留最新一版。

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
  删隧道、删 nginx 站点，容器只停不删，数据保留，**保留期内可在详情页一键恢复**。
  服务端仍然要求 `confirm == slug`，由前端填。
- 升级/重建（running 详情页）= 一次点击 + 一张后果卡（说清：重建容器、会话短暂打断、
  数据不动、登录链接不变、目标镜像是谁），零打字。
- released 详情页两个动作：**恢复**（一次点击，说清重建哪三处外部对象、原域名原端口回来）；
  **彻底释放**（长按确认 + 一屏后果清单：容器删除、数据目录删除、**不可恢复**，
  并显示自动彻底释放的日期）。released 行/详情要显示「保留期还剩 N 天」。
- purged：灰上加删除态标记「已彻底释放」，详情只读无按钮，历史（工单/事件）照看。
- 密钥不进列表接口；详情页进页面时 reveal 一次拿最新，平时只显示打码与时间；
  **界面不轮询抓密钥**；reveal 与自动重抓同守 §3 去重——没变化不产生任何事件。
- 危险与不可逆动作不用弹窗套弹窗，一屏内把后果说完。
- 不显示用户用不上的字段（镜像 id、内部状态机原始值这类收进次要位置或不显示）。
