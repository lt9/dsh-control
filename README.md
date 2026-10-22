# dsh-control

给分散在多台机器上的 **DSH（DeepSeek Harness）** 容器做 **开通 → 查看 → 重启 → 回收**
的运维主控台。单容器部署，登录后是一份中文运维台账：谁在用哪台、活着没有、公网入口
通不通、里面到底配的什么模型，一眼看得到；开通和回收都是**逐条呈现、可回滚、可续建**
的工单。

> 非官方第三方工具，与 DSH 的开发方无隶属关系，也不含、不依赖 DSH 源码——它只通过
> SSH 在被管宿主机上执行 `docker` 与 `docker exec`，读容器里实际生效的配置。
> DSH 及相关名称归各自权利人所有。

![实例台账](docs/images/ledger.png)

> 缘起很实在：给自己和家人各开一个能公网访问的 AI 助手实例，手工做一遍要动四个
> 系统（云解析、内网穿透、反向代理、宿主机 Docker），40 分钟起步，而且每次都要
> 重新回忆一遍顺序。漏掉任何一处，症状都是"能解析但打不开"，排查一次半小时。
> 回收更危险——反代面板的"按库重建全部站点"会把你正在跑的其他服务一起覆盖掉。
>
> 这个控制台把这条链路固化成一台状态机：**外部对象只碰自己创建的，任何一步失败
> 可以接着做完，不会留下半条隧道**。

---

## 一个实例是什么

```
一个实例 = 一个人的 DSH 容器
         + 一条 DNS A 记录        （云解析，真实 API）
         + 一条 frp 隧道          （frp-panel，真实 API）
         + 一个 nginx 反代站点    （nginxWebUI，真实 API）
         + 一个登录密钥           （容器每次启动都会轮换，控制台负责追回来）
```

开通顺序 `DNS → frp → 反代 → 容器 → 抓密钥 → 公网探活 → 复核`，每步都记进工单，
带真实执行的命令与输出；失败可以从失败那一步重试，不必从头再来。
回收按 **反代 → frp → DNS** 反向断开，然后**只停容器不删容器**：数据保留、档案仍可查，
进入 `released` 停放态——**保留期内（默认 15 天，`[site].purge_after_days` 可改）可在
详情页一键恢复**（重跑开通工单：解析/隧道/反代重建，原域名原端口回来）。保留期满
（或你手动长按确认）走 **彻底释放 `purge`**：补删残留外部对象、删容器、删数据目录，
进入只读终态 `purged`——这一步不可恢复，事件里能查到删了什么。

## 它替你把住的几道关

| 关 | 具体做法 |
|---|---|
| 不碰别人的东西 | 三处外部系统只操作本控制台创建的对象；隧道名有前缀护栏，指向别的机器的隧道一律拒绝复用 |
| 不猜外部状态 | 动手前先核对"库里的站点"与"盘上的配置文件"是否漂移，漂移就先报告，绝不盲目重建 |
| 不放水通过复核 | 最后一道闸门读**容器里实际生效的配置**（`docker exec … dsh web --dump-config`），不是仓库模板；模型地址不是站内端点就判失败 |
| 密钥不泄漏 | 密钥集中存库，日志与错误信息自动脱敏；界面默认打码，点眼睛才展开；复制按钮在无明文时先现取，取不到就什么都不做 |
| 不可逆动作要打字 | 释放必须逐字输入实例域名才能提交；长按按钮这种"假二次确认"不算确认 |
| 界面做减法 | 除登录与改备注名外，界面不该要求你打字——域名由系统给出，你只需抄对 |

## 详情页能看到什么

![实例详情](docs/images/detail.png)

- **一键登录**：当前最新的密钥（重启会轮换，页面每次重新取），一键复制成完整可用链接
- **运行态**：容器状态、健康、重启次数、CPU 与内存占用（`docker stats` 采的，
  不是 `docker inspect` 里那几个永远为空的字段）
- **DSH 配置**：容器里实际生效的模型、上下文窗口与压缩触发点、插件与智能体团队开关、
  已发现的技能、预设与并发、准入与权限；点「重新读取」现取
- **网络**：域名、公网入口、公网端口、容器 IP，以及三处外部对象各自的面板编号
- **异常与事件**：谁在什么时候自动重启过一次、密钥什么时候抓回来过

新实例还会被自动铺好两项能力：**生图技能**（DSH 自身没有生图入口，技能 + 零依赖
单文件客户端调 ComfyUI 接口）与**智能体团队**（写进 DSH 的 home 层补丁，不重build
镜像）。没配生图服务就跳过并记一行警告——生图没配不该让一个实例开不出来。

## 第一次接手要先准备什么

先说最重要的省事事：**云解析、frp、nginxWebUI 三块都不是必需的**。它们各自有
`noop` 适配器（在 `deploy/site.toml` 里写 `dns=noop` / `frp=noop` / `ng=noop`），
选了 noop 之后控制台一个请求都不发，而是把"这一步本来要做什么"写成一条结构化的人工
动作，显示在工单日志里让你手工去做，做完继续往下走。还有一个全局开关 `DSHC_DRY_RUN=1`，
写操作全部只记录不执行（读操作照真发，因为幂等判断要先读）。**所以你可以只准备一台
机器就把主流程跑通**，三块外部系统等有条件再接。

### 按你想做到哪一步，准备对应的东西

| 你想做到 | 需要准备 | 不需要 |
|---|---|---|
| **A 看代码 / 改界面** | 一台任意机器 + Node 18+。`node mock/server.mjs` 起假接口，向导、工单流、SSE 日志、详情页全能点 | 数据库、任何真实系统 |
| **B 真开实例，只在内网** | PostgreSQL；一台跑 Docker 的被管宿主机 + 一把专用 SSH 密钥；实例的部署套件（compose 或脚本）；一个 OpenAI 兼容模型端点。三块外部系统全填 `noop` | 域名、公网 IP、云账号 |
| **C 真开到公网域名** | 上面全部，再加：一个域名 + 可写 API 的云解析凭据、一个有公网 IP 的位置跑 frps + frp-panel 与 nginxWebUI、云上放行的端口段 | Kubernetes、Redis、消息队列、对象存储 |

### 要几台服务器

| 台数 | 怎么摆 | 适合 |
|---|---|---|
| 1 台 | 控制台、PostgreSQL、被管容器、frps/面板/反代全在一台**有公网 IP** 的机器上 | 试水。能跑，但控制台一挂全家失联；而且"穿透"变成自己打洞到自己，纯属绕路——这种拓扑直接让反代指向 `127.0.0.1:端口`，frp 那层填 `noop` 就行 |
| **2 台（推荐）** | 一台常年在线的公网机：frps + frp-panel + nginxWebUI（+ PostgreSQL 也行）；一台家里的 NAS 或小主机跑实例 | 绝大多数家庭场景 |
| 3 台 | 控制台搬进内网一台小主机（密钥不落在公网机上，值得），公网机只做穿透与反代 | 你打算长期用它管多台实例 |

控制台本身很轻：一个 Python 进程 + 一个 PostgreSQL 库，实测空载常驻内存 70 MiB
量级，随便一台小主机都够。**它不需要被实例访问**——只要它能 SSH 到被管宿主机、
能访问那三个面板的地址，出方向通就够了。

**有公网 IP 的宿主机可以完全不过 frp**：seed 的主机条目写 `upstream_mode = "direct"`
即直连主机——开通时不建隧道（该步 skipped），反代从入口机直接回源这台机的
`ssh_host:公网端口`；端口占用改为在本机实探（`ss -ltn` + 库里同机实例），详情页会标出
回源方式。云防火墙上把实例端口段**只对入口机放行**，别对全世界开（详见
[docs/SITE-CONFIG.md](docs/SITE-CONFIG.md) 的直连主机一节）。同机兼跑入口服务的挤机
务必再设 `mem_limit`/`cpu_limit`：kit 默认 4g 会把同机基础设施送进 OOM。

### 凭据与软件清单

| 东西 | 必需？ | 要准备什么 | 我们实际跑过、因此验证过的版本 |
|---|---|---|---|
| PostgreSQL | ✅ 必需 | 一个库 `dsh_control` + 只有该库权限的角色。别给它开公网 | 17.x（`postgres:17-alpine`）。SQLite 能起来跑演示，但涉及 CHECK 约束的迁移会逐条跳过 |
| Docker（被管宿主机） | ✅ 必需 | 装 Docker、给一个**专用** SSH 用户并把公钥写进 `authorized_keys`；容器操作要用到 docker，所以这个用户得进 `docker` 组。别拿你的日常私钥顶上 | Docker 29.x，Debian 与群晖/威联通 QTS 都跑过 |
| 实例部署套件 | ✅ 必需 | 一套能"建一个目录 + 起一个容器"的东西：仓库里的 `new-instance.sh`/`deploy.sh` 那种形态（位置参数、`.env` 驱动）。控制台只调你的脚本，不重复实现部署 | 我们自己那套（见下条） |
| DSH 镜像 | ✅ 必需 | 事先构建好、`docker load` 到被管宿主机上。控制台**不构建镜像** | 自己构建的 `dsh-family` 镜像 |
| 模型端点 | ✅ 开通必需 | 一个 OpenAI 兼容 `/v1` 地址 + key（自架 vLLM 或订阅 API）。实例唯一允许的外发端点，也是复核闸门的判据 | 自架 vLLM；任何兼容端点都行 |
| 云解析 | 要公网域名才必须 | 域名托管 + 一对 AK/SK。**用 RAM 子账号，只挂 `AliyunDNSFullAccess`**；root key 的泄露代价是整个账号 | 阿里云 DNS RPC 签名 v1，`Version=2015-01-01`（换别的版本号会被服务端拒 `InvalidVersion`）。想接 Cloudflare/腾讯 DNS 就照 `integrations/` 里那 300 行改一个适配器 |
| frp-panel | 要内网穿透才必须 | [VaalaCat/frp-panel](https://github.com/VaalaCat/frp-panel) 的面板账号（用户名/密码）、一个 frps 监听端口、每台宿主机一个 frpc 客户端注册进来 | `vaalacat/frp-panel`（2025-02 的镜像）。用的是 `POST /api/v1/auth/login` 换 JWT，加 `/api/v1/proxy/{list_configs,create_config,update_config,delete_config}` |
| nginxWebUI | 要按域名建反代才必须 | [cym1102/nginxWebUI](https://github.com/cym1102/nginxWebUI) 容器 + 面板用户名口令（它的鉴权是 `GET /token/getToken?name=&pass=` 明文口令换 token，所以这个面板**只在内网可达**） | `cym1102/nginxwebui:4.2.5`（内置 nginx 1.26.2） |
| 生图服务 | 可选 | ComfyUI，前面放一道 Bearer token 门卫（它自己没有任何鉴权）。留空就不铺生图技能，实例照常开出来 | ComfyUI 0.37 的**原生异步接口**（`POST /prompt` → 轮询 `/history/{id}` → `/view` 取图），不是 OpenAI 的 `/v1/images` |
| 一台能签证书的机器 | 要 HTTPS 才必须 | 要么让 nginxWebUI 走 ACME，要么自己在前面再放一层反代 | — |

**版本支持口径（不吹）**：本项目不声明版本支持矩阵。上表最后一列是"我们跑过、所以
知道它是对的"的那些版本；换版本之前，先照 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
§5 列的真实调用点逐条核对。适配器全部在 `src/dsh_control/integrations/`，四个适配器互
不依赖，测试里用的是假面板，所以你自己改一个适配器不会牵动别处。

### 网络与端口要提前想清楚的三件事

1. **公网端口要一次开一段**，不要"每开一个实例去云上点一个端口"。控制台从
   `DSHC_FRP_PORT_START` 起往后找空闲端口，它会探测 VPS 上端口是否被占用，但
   **探测不到云防火墙的放行状态**——所以端口段要在云上安全组里先放行好一段，
   并把 `DSHC_FRP_PORT_START` 设在这段里面。
2. **云厂商的"秒连"陷阱**：不少云在边缘代答 TCP 握手，没放行的端口也表现为
   `connect` 成功但没有数据。判断一个公网门到底通不通，看的是**有没有真实 HTTP
   响应**（或 SSH banner），不是能不能连上。
3. **反代那一层的 Host 头必须透传**：站点是按实例的 `publicUrl` 建的，如果公网路径
   上还有一层 CDN 或代理改了 Host，实例会以 401/404 拒绝，症状和"没配好"长得一样。

另外一条与本项目无关但你一定会撞上：**域名解析到中国内地服务器提供 web 服务需要
ICP 备案**，这是云厂商与管局的要求，不是本工具的要求。用境外中继的部署请自行确认
你所在地的合规口径。

### 照着做一遍的准备清单

1. 一个域名，托管到云解析；建一个只挂 `AliyunDNSFullAccess` 的 RAM 子账号，拿 AK/SK
2. 公网机：起 frp-panel、起 nginxWebUI、起 PostgreSQL；云上放行 frps 端口 + 一段公网
   端口 + 80/443
3. 被管宿主机：装 Docker；建专用 SSH 用户并加入 `docker` 组；把公钥写进 `authorized_keys`；
   把 `dsh-control_ed25519` 这种**专用**密钥放在控制台这边，权限 600
4. 被管宿主机上准备好实例部署套件（`new-instance.sh <名字> <端口>` 那种），并
   `docker load` 一个实例镜像
5. 一个模型端点地址 + key
6. `git clone` → `cp .env.example .env`（填 `DSHC_DATABASE_URL`）→ `docker compose up -d`
   → `cli migrate` → `cli init`（运维密钥只打印一次，立刻存进密码管理器）
7. 写 `deploy/site.toml`：zone、前缀、端口起点、数据根目录、PG、模型端点、生图地址（可空）、
   三块面板的地址与凭据变量名
8. `cli seed --site /site/site.toml` 把主机与凭据灌进去
9. **先设 `DSHC_DRY_RUN=1` 跑一次新建**，看清每一步本来会往哪个面板发什么请求，
   再关掉它真开一台
10. 真开第一台时全程盯工单日志；失败就地重试，**不要去云上手工删对象**——库里会留下
    对不上的记录，那是最难查的一类故障

## 快速开始

需要 Python 3.11+、PostgreSQL、Docker。三块外部面板（云解析 / frp-panel /
nginxWebUI）**没有也不影响启动**：不配就跳过对应步骤，本地先把界面与工单跑通，
再接真面板。

```bash
git clone https://github.com/lt9/dsh-control.git && cd dsh-control
cp .env.example .env                      # 至少填 DSHC_DATABASE_URL

docker compose build                      # 需要时传镜像源：APT_MIRROR=... PIP_INDEX_URL=...
docker compose up -d
docker compose exec dsh-control python -m dsh_control.cli migrate   # 建表
docker compose exec dsh-control python -m dsh_control.cli init      # 生成运维密钥，只打印一次
```

浏览器打开 `http://127.0.0.1:13300`，粘进运维密钥即登录。

要把真实主机与各面板凭据灌进来，写一份站点描述文件（`deploy/` 已只读挂进容器的
`/site`，且默认被 gitignore，不会被误提交）：

```bash
cp deploy/site.example.toml deploy/site.toml    # 填主机、zone、凭据变量名
docker compose exec dsh-control python -m dsh_control.cli seed --site /site/site.toml
```

### 不接任何外部系统，先把界面走一遍

仓库自带一份假接口，契约与真后端逐字对齐，够你把新建向导、工单流、SSE 实时日志、
详情页全部走一遍：

```bash
cd frontend && npm ci
node mock/server.mjs &                                 # 127.0.0.1:13300，登录密钥 dshc-demo
VITE_API_TARGET=http://127.0.0.1:13300 npx vite        # 开发端口 13301
```

本 README 的两张截图就是这么来的：域名、密钥、IP 全是文档占位值。

## 配置放哪

三件事各归各位，混在一起就再也分不清"换个站点要改什么"：

| 东西 | 放哪 |
|---|---|
| 表结构 | `migrations/`，零站点数据、零 INSERT |
| 密钥与凭据 | `credentials` 表（`operator_key` / `ssh_key` / `aliyun_ak` / `frp_panel` / `ng_panel` / `llm_key` / `image_key` / `site`） |
| 被管主机 | `hosts` 表 |
| 站点级参数 | `deploy/site.toml` → 落 `settings`，或同名 `DSHC_*` 环境变量 |
| 每实例的外部对象编号 | `instances` 表对应字段（记录 id、隧道 id、面板站点 id、公网端口） |

常用几项：

```ini
DSHC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/dsh_control
DSHC_DNS_ZONE=example.com            # 实例域名后缀
DSHC_DNS_PREFIX=dsh-                 # 实例名前缀，同时是"只碰自己对象"的护栏之一
DSHC_FRP_PORT_START=20000            # 公网端口搜索起点
DSHC_LLM_BASE_URL=http://192.0.2.20:8000/v1    # 实例唯一允许的外发端点（也是复核判据）
DSHC_IMAGE_API_URL=http://192.0.2.20:8188      # 生图服务；留空则不铺生图技能
DSHC_TEAM_MAX_MEMBERS=4              # 新实例的智能体团队人数上限
```

完整清单见 [docs/SITE-CONFIG.md](docs/SITE-CONFIG.md)，其中"换掉 frps / nginxWebUI /
云解析要动什么"是单独一节。

## 代码结构

```
src/dsh_control/
  api/           路由与序列化（写操作要求 X-Requested-With，挡住跨站写）
  domain/        纯函数：配置文档、安全护栏、DSH 生效配置解析、日志脱敏
  integrations/  云解析 / frp-panel / nginxWebUI / SSH+Docker 四个适配器
  jobs/          工单状态机：deploy · restart · release · expose · 能力铺设
  db/            模型与仓储
  skills/        随开通铺进实例的技能（生图）
frontend/        React + Vite + TypeScript，手写组件，无 UI 库
docs/            契约、站点配置、故障排查（现网踩坑逐条记账）
```

工单是唯一的状态推进者：`jobs/runner.py` 按序号执行步骤，每步落库（状态、耗时、
真实命令、输出摘要），失败即停并把已建对象记进回滚清单。前端读 SSE 实时追日志，
连不上就退回轮询。

数据库三条规矩在评审里逐条查：**不建外键、不写联表、每个字段带中文备注**。
理由见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §2.0。

## 开发

```bash
uv venv .venv && uv pip install -e ".[dev]"
make lint test                                    # ruff + pytest
cd frontend && npm ci && npx tsc -b && npm run build
```

测试跑的是假接口与假主机，**不碰任何真实系统**，任何时候都能跑。
契约改动先改 `docs/ARCHITECTURE.md` 再改代码，PR 里注明改的是哪一节。
详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 已知限制（不藏）

- **单一运维密钥**：拿到它等于拿到所有实例的一键登录地址。默认只走内网；
  确需公网入口请自行叠加前置认证与 IP 白名单。
- **凭据明文存库**：为了能把值读回来。这个库和它的备份属于最高敏感级，别开公网。
- **实例登录 token 会出现在容器日志里**，容器保留期内日志等同凭据。
- **旧 token 在实例重启后仍能通过实例自己的校验**——那是被管程序的行为，控制台能
  做的只是把最新那份追回来、显示在页面上，并告诉你哪条链接才是当前有效的。
- **released 有保留期，到期数据会被真删**：默认 15 天（`DSHC_PURGE_AFTER_DAYS`，库里的
  site 值优先、watcher 每轮现读），到期自动入队 purge 工单删容器删数据目录，不可恢复；
  保留期内可随时一键恢复。purge 的数据目录只认实例 `.env` 的生效值且必须严格位于该主机
  `data_root` 之下——路径对不上宁可不删、工单直接失败。
- **限流是进程内的**，多副本部署需要换成共享存储；判定内网来源时信任 RFC1918 地址。
- **释放后域名仍会被递归 DNS 缓存到 TTL 到期**（默认 600 秒），命中也只是 401/404。
- **PostgreSQL 是唯一被完整测试的后端**：SQLite 能起来跑演示，但涉及 CHECK 约束的
  迁移会逐条跳过（迁移里显式判了方言），少几道白名单保护。
- 本工具**不构建镜像**：被管实例的镜像由另一套部署套件产出，控制台只编排它。

## 文档

| 文件 | 讲什么 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 契约：数据模型、接口、工单步骤与回滚、状态定义 |
| [docs/SITE-CONFIG.md](docs/SITE-CONFIG.md) | 站点配置分层；换外部系统要动什么 |
| [docs/05-故障排查.md](docs/05-故障排查.md) | 现网踩过的坑逐条记账，含判断方法 |
| [docs/images/](docs/images/) | 界面截图（假接口渲染，无真实站点信息） |
| [PRODUCT.md](PRODUCT.md) / [DESIGN.md](DESIGN.md) | 产品简报与设计令牌（配色、密度、语气） |
| [SECURITY.md](SECURITY.md) | 漏洞报告方式与已知设计边界 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志 |

## 许可

[MIT](LICENSE)
