### 5.2 frp-panel `integrations/frp_panel.py`

> **实测坑二：建好 ≠ 通**。面板的 API 面（把它前端里所有接口挖出来核过：只有
> `auth/*`、`client/{list,get,init,delete}`、`server/*`、`platform/baseinfo`、
> `proxy/{list_configs,get_config,create_config,delete_config}`）**没有任何"下发/重载客户端配置"
> 的接口**。`create_config` 之后，面板会为在线客户端把公网端口占上（真机核对：59 条配置里
> 57 条在听，掉线客户端的那 2 条不在听），但 **frpc 进程要自己重新拉到配置才真开始转发**。
> 真机证据：端口在听、配置逐字段正确，公网打过去 15 秒无响应，而目标机上
> `tcpdump -ni any tcp port <localPort>` **一个包都没有**——客户端根本没去拨。
>
> 推论，都是硬约束：
> 1. 探活不许当成"已生效"的凭据反推。隧道建完必须端到端探测；探不通时报错要写清
>    "隧道配置已建，等待该客户端重新加载（重启该 frpc 会瞬断它名下其它隧道）"，
>    **绝不自动重启别人的 frpc**。
> 2. 同理，`list_configs` 里有记录也不能当作门已开的证据。
> **实测坑**：`list_configs` 带 `clientId` 过滤时返回**空集**而不是筛选结果（同一时刻
> 不带过滤 59 条、带过滤 0 条）。所以过滤必须在本地做。这条不是性能问题：只要经由
> 面板过滤，`find_by_name` 就永远查不到，`ensure` 会重复建门，创建后的回读校验也永远
> 失败——第一次真实开通就是这样把已经建好的隧道判成"不存在"的。


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
（登录接口换取），文档在 jar 的 `static/doc/*.html`：`ServerApiController`、`UpstreamApiController`。
**没有 LocationApiController**，location 靠 `ServerApiController` 的嵌套字段下发。

必须生成的配置（这是今晚踩过的坑，写死在模板里）：
- 反代站点的 location 上 `headerHost = "$host"`，**不能是 `$host:$proxy_port`**：DSH 的准入判定
  要求 `Origin` 的 authority 与 `Host` 完全相等，带端口必被 403，症状是"界面能打开但一直重连"。
- `websocket = true`：`/api/remote.mux` 走 WebSocket。
- 服务器级反代（不带 location）永远生成 `$host:$proxy_port`，所以**必须走 location**。

创建/更新调用要幂等：先按 `server_name` 查，存在则带 id 更新。

**API 只写库，不落盘**（真机踩出来的，面板自己的前端也是这么走的）：`insertOrUpdate*` 全部只写
sqlite；把库物化成磁盘 conf 的入口是 `/api/nginx/replace`，UI 保存站点后前端替用户调它就是它。
`/api/nginx/reload` 只是 `nginx -s reload`，**不物化**。所以 `reload()` 必须是三步：
`check`（把库构出的配置写 temp 跑 `nginx -t`，不碰真实文件）→ `replace` → `reload`，
任一步失败就停在原位。漏了 `replace` 的症状最难查：库里站点、location、后端全对，
`conf.d` 里却没有文件，公网访问落到默认服务器，而日志照样打"reload 完成"。

> `replace` 是**全面板重建**：`conf.d` 整目录按库重写，`nginx.conf` 里那份逐条
> `include conf.d/xxx.conf;` 的清单也一起重写。两条推论，都是硬约束：
> 1. **绝不可手工编辑 nginx 的任何文件**。手改的内容会在下一次任何人（包括用户自己点 UI 保存）
>    触发 `replace` 时被无声打回库里的样子。
> 2. 因此库里任何一处与磁盘不一致（drift）都是定时炸弹。上线前必须核对：把库里的 location
>    逐条与磁盘上对应 conf 的 `proxy_set_header Host` 比对，有漂移就先随库对齐（走面板，
>    不走文件系统），否则本次重建会把别人正在跑的服务打回坏状态。

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
