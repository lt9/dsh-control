# ADR 0001：能力包 manifest 与 capability 契约（dshctl 就是 dsh-control）

- 状态：Accepted（2026-10-29，评审点处置见文末「拍板记录」；P2 按此实施）
- 日期：2026-10-29
- 决策者：待评审

## 口径修订记录

**v1（2026-10-29 上午的初稿）假设存在一个随 kit 分发到被管主机上的 `dshctl` CLI，
并把「实例侧唯一变更执行者」这个角色交给它。这个假设作废。**

用户澄清：「我一直说的 dshctl 就是 dsh-control」，`dshctl` 是项目域名 `https://dshctl.pipboy.cn/` 的前缀简称。
三处既有用法与此一致，全都指控制台本身而不是主机上的可执行文件：

- [ARCHITECTURE §5.5](../ARCHITECTURE.md)：「控制台自身就是第一个实例：`dshctl.example.com`」；
- §5.2：本控制台建的 frp 隧道固定叫 `dshctl_<slug>`；
- dsh-family `docs/05-故障排查.md` 2026-10-27 晚那条跟进：「『货』也全部收进 dshctl……
  `dsh-control fontlib publish` 进制品库……`dsh-control kit publish` 进数据库……
  本仓库（dsh-family）职责收敛为**造货**，**发货台账全在 dshctl**」。
  同一个口径在 [SITE-CONFIG「资产分发」](../SITE-CONFIG.md) 里写着：「镜像、配置、脚本、字体全部由 dshctl 持有并下发」。

**所以：`dshctl` = dsh-control 控制台，含它自己的管理 CLI（[cli.py](../../src/dsh_control/cli.py) 的
`kit publish` / `fontlib publish` / `images ship` 那一族）。不存在「实例侧的 dshctl 执行器」。
能力的变更执行走控制台既有机械：SSH + `docker exec` + `deploy_jobs` / `deploy_steps` 状态机 + kit 整包换装通道**
（ARCHITECTURE §6 步骤 20 的 `kit/.dshctl-kit-version` 对账换装就是现成的发货机制，能力包只是新增一类货）。

改动点清单：

| 篇节 | v1 的说法 | 本版 |
|---|---|---|
| 决策一 | 被管主机上的唯一变更执行者是 dshctl CLI | 变更执行者就是控制台；被管主机上只跑 `docker`，不装 agent 也不装能力 CLI（§0 第 5 条不变） |
| 决策二 | manifest 由主机上的 dshctl 解释 | manifest 由控制台解释：新模块 `integrations/capabilities.py`（引擎）+ `cli.py` 子命令 + jobs 步骤 |
| 决策三 | `dshctl capability` 六个主机子命令、`status --json` 是主机回传、按退出码判定 | 两层入口：控制台管理 CLI（`dsh-control capability …`）+ 页面/API 点击；观测由引擎经 SSH `docker exec` 跑 manifest 声明的探针；判定按 `deploy_steps` 三态 |
| 决策三 | 退出码表（0/1/2/3/4/5/6/7/64） | 步骤结果语义 `ok`（`succeeded`）/ `failed` / `skipped`；U-6 随之消解（comfy 先例已定案） |
| 决策五 | 「能力包随 kit 分发」列为一条待拍的通道问题 | 通道确定就是 kit 换装：`kit_releases` 的清单从 `scripts` + `compose.yaml` + `.env.example` 扩到 `capabilities/` |
| 未决问题 | U-4 问「dshctl 与 entrypoint 谁赢」 | 重写为「kit entrypoint 启动时铺」vs「控制台 deploy 步骤经 `docker exec` 铺」，倾向 entrypoint |
| 未决问题 | U-6 问 `skipped` 用哪个退出码 | 删除（有先例，直接定案） |

**保持不变**（用户已认可）：四类 kind、manifest 的 TOML 结构与四类 kind 的字段、`compatible_dsh` 门槛
（U-2 仍待拍板，但比对口径改成现读的两个选项）、`capabilities` / `instance_capabilities` 两张表、
desired ↔ observed 对账环、speckit 默认开。文件名沿用 `0001-能力包manifest与dshctl-capability契约.md` 不改，
避免已发出的链接失效。

## 术语

- **dshctl** = dsh-control 控制台（含它的管理 CLI）。全文不再出现「主机上的 dshctl」。
- **能力（capability）** = 一件可装可卸的货：技能、配置补丁、DSH 插件、外部服务，四类 `kind` 之一。
- **能力包（capability pack）** = kit 目录里 `capabilities/<id>/` 那一份 manifest 加它的资产。
  一个 kit 包（一次 `kit publish`）带着一整棵 manifest 树发货，控制台读包内 manifest 记账。
- **skill** = 四类 kind 之一（往 `$DSH_HOME/skills/` 铺文件的那个装法），不要与「能力包」混用。
- **引擎** = 控制台里解释 manifest、拼命令、经 SSH `docker exec` 执行并回读的那段代码（本篇建议落点
  `src/dsh_control/integrations/capabilities.py`）。

## 背景与问题

「生图技能」与「智能体团队」现在是两段写在 Python 里的 shell：正文在
[capabilities.py](../../src/dsh_control/jobs/capabilities.py) 的 heredoc 里拼，开通第 66 步与重启第 5 步各调一次。
[vendor 分析 §3.1](../vendor/2026-10-29-agent-memory-与-spec-kit-融入dsh-control分析.md) 判定的问题是知识放错了地方：
技能正文与补丁拼装规则属于「货」，却住在控制台仓库里——改一件能力要发一次控制台。

同报告 §3.2–§3.3 给的收敛方向是把技能、补丁、插件、外置服务统一成「能力包」，
用 `capabilities` / `instance_capabilities` 两张表对期望态与观测态。
按现行口径，这件事不是造一个新部件，而是**把 heredoc 换成货 + 清单**：能力知识搬进 kit 里的 manifest，
控制台只留通用引擎、目录、台账、工单与密钥注入。实施分期沿用 vendor §6，本篇不重复。

## 决策一：职责边界

|  | 控制台 dshctl（编排 + 记账 + 动手） | 能力包（kit 里的一类货） | 被管主机与实例 |
|---|---|---|---|
| 是什么 | 唯一编排者，也是唯一的变更执行者 | 自包含的声明 + 资产 | 被操作的对象 |
| 知道 | 能力 id、版本、参数 schema、期望状态、密钥、审计、哪个实例该开哪些能力 | 自己怎么装 / 卸 / 量（manifest 里的动作与探针） | — |
| 不知道 | 技能正文怎么写、补丁正文怎么拼、compose 细节（读包，不抄进代码） | 哪个实例该开自己、期望版本、密钥、账本 | 有什么货就装什么 |
| 现在 | 能力知识内嵌在 `capabilities.py` 的 heredoc | 无清单，逻辑散在仓库两侧 | 只跑 `docker` 与 kit 脚本 |
| 目标 | 目录 + 台账 + 工单 + 密钥注入 + 审计 + 一个通用引擎 | `capabilities/<id>/manifest.toml` + 资产 | 不变，多装一样东西都不新装部件 |

三条硬规则：

1. **控制台不得再内嵌 shell 知识。** P3 完成后 `capabilities.py` 只剩编排（取参数、取密钥、发命令、读结果）；
   新增能力一律以 manifest 表达，评审时看到为某个能力新写的 heredoc 直接驳回。
2. **被管主机上能力的唯一变更执行者是 dshctl（= 控制台）。** 手段是 SSH + `docker exec`，
   经 `deploy_jobs` / `deploy_steps` 工单执行；容器生命周期脚本（`new-instance.sh`、`deploy.sh`、
   `verify.sh`、`backup.sh`，§5.1）继续由控制台直接调用，不受此条约束。
   **不在被管主机上装常驻 agent，也不装能力 CLI**（§0 第 5 条）——运维手敲的临时改动不是变更，
   它会在下一轮对账里被探针判成漂移并黄标。
3. **密钥一律 stdin。** 不进 argv（会在目标机 `ps` 里裸奔）、不进日志、不进错误信息、不进 URL query
   （§0 第 4 条；`capabilities.py` 现有的 `read -r` 三行 stdin 就是这个约束的实现）。
   密钥只住 `credentials`，工单执行时现取现塞 stdin，台账只记非敏感参数。

推论（评审若认可，一并落进 ARCHITECTURE）：控制台不再自己 grep 容器文件判断能力状态——
`capabilities_ready` 那种「查 home 层在不在」的旁证退役，观测态只认引擎按 manifest 探针采回的结果。

## 决策二：manifest v1

manifest 是**货的内容格式 + 控制台的解释契约**，不是主机程序的配置。每个能力包一份 TOML，
与它的资产同放在 kit 仓库的 `capabilities/<id>/` 下，随 kit 整包发货。
kit 清单要从 `assets.py` 现在的 `scripts` + `compose.yaml` + `.env.example` 扩到 `capabilities/`。

通用字段里 `id / version / kind / params / compatible_dsh / healthcheck / uninstall` 七项来自
vendor §3.2 的最小字段集；`title`、`marker`、`status`、`restart_required` 四项是本契约补的，补的理由写在表里。

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `id` | string | 是 | 能力 id，也是目录名；全局唯一，命名 `[a-z0-9-]` |
| `version` | string | 是 | 包版本；页面上的「升级到 vX」与台账 `desired_version` 都取它 |
| `kind` | string | 是 | `skill` \| `patch` \| `plugin` \| `service` |
| `title` | string | 是 | 详情页展示名（补的：台账要给不懂技术的人看） |
| `params` | table | 是 | 参数 schema：每项含类型、是否必填、`secret`；外加缺料策略 `on_missing` = `skipped` \| `failed` |
| `compatible_dsh` | string | 是 | 兼容的 DSH 版本区间（比对口径见未决 U-2） |
| `marker` | string | 是 | 铺设记号（补的：`capabilities.py` 现在就靠 `TEAM_MARKER` 判幂等，把它提到契约里）；幂等判定与摘除的锚点 |
| `status` | string | 是 | 在位探针（补的）：一行命令，退出码 0 即「装着」。引擎经 `docker exec` 跑，结果进台账 `marker_present` / `installed` |
| `healthcheck` | string | 是 | 健康探针：一行命令，退出码 0 即健康。`skill` / `patch` / `plugin` 在容器内跑，`service` 在主机上跑（探的是外部服务） |
| `uninstall` | string | 是 | v1 只允许 `reverse`：卸载 = 安装的反向 |
| `restart_required` | bool | 否 | 默认 `true`（补的：DSH 只在启动时组合一次配置，home 层补丁铺完必须重启才生效，`restart.py` 第 5 步注释已记这条） |
| `default_on` | bool | 否 | 默认 `false`（P2 落地时补的：新实例开通第 69 步只铺 `default_on=true` 且站点未关的能力——speckit 的「默认开」全靠它，站点级关停走 credentials(kind=site) 的 `capabilities={"<id>":false}`） |

kind 专属字段：

| kind | 装法 | 卸法 | 专属字段 |
|---|---|---|---|
| `skill` | 文件铺进 `$DSH_HOME/skills/<id>/` | 删目录 | `files`（资产文件名，随包同放）、`config_template`（可选，渲染成 `config.env`）、`secret_target`（密钥落地的文件名 + 权限） |
| `patch` | 追加 home 层 `cordis.patch.yml`（带 marker） | 按 marker 摘除整段 | `source`（补丁正文来源，见 agent-team 例）、`append`（追加条目，可含 `{{param}}` 插值） |
| `plugin` | 包进 profile 的 `node_modules/` + 启用条目 | 删启用条目 + 删目录 | `packages`（包目录名）、`enable_entries`（cordis 补丁里的 `insert` 条目）、`laid_by`（`entrypoint` \| `console`，见 U-4） |
| `service` | 主机级 compose 服务 + 实例接线（改 baseURL / 凭据），外部 API 由控制台适配器管 | 实例还原直连 + 停用外部用户 | `compose`（片段，镜像 tag 钉在清单里）、`external_api`（指向控制台适配器名）、`wiring`（实例接线怎么改） |

四个真实候选的完整示例（字段值取现网与 vendor 报告，不发明新能力；`version = "1.0.0"` 是占位——
现网这两个能力从来没有版本概念，首次发布时定；`memory-hub` 的版本取上游 release `2.0.3`）：

```toml
# capabilities/comfy-image/manifest.toml —— kind = skill
id = "comfy-image"
version = "1.0.0"
kind = "skill"
title = "生图技能"
compatible_dsh = ">=0.2.1-rc.2"          # 取 dsh-family 现锁版本（dsh-family README）
marker = "# dsh-control: comfy-image 由控制台铺设"
status = "test -f ${DSH_HOME}/skills/comfy-image/SKILL.md"
healthcheck = "test -f ${DSH_HOME}/skills/comfy-image/config.env"
uninstall = "reverse"
restart_required = false
files = ["SKILL.md", "comfy_generate.py"]
config_template = "config.env.tmpl"      # 渲染出 COMFY_URL / COMFY_BACKUP_URL / COMFY_TOKEN / COMFY_TIMEOUT
secret_target = { file = "config.env", mode = "0600" }

[params]
on_missing = "skipped"                   # 站点没配生图就跳过，不挡开通（capabilities.py 现行为）

[params.url]        type = "string"  required = true   secret = false
[params.backup_url] type = "string"  required = false  secret = false
[params.token]      type = "string"  required = true   secret = true
```

```toml
# capabilities/agent-team/manifest.toml —— kind = patch
id = "agent-team"
version = "1.0.0"
kind = "patch"
title = "智能体团队（实验组合包）"
compatible_dsh = ">=0.2.1-rc.2"
marker = "# dsh-control: 智能体团队（实验组合包）由控制台铺设"   # 沿用现有 TEAM_MARKER，迁移期两段文本必须逐字相同
status = "grep -qF 'dsh-control: 智能体团队' ${DSH_HOME}/cordis.patch.yml"
healthcheck = "grep -q maxMembers ${DSH_HOME}/cordis.patch.yml"
uninstall = "reverse"
restart_required = true
# 补丁正文不抄进仓库：运行时顺着 dsh 可执行文件解析包根，在镜像里找官方组合包那一份
# （现网实测 183 ms 命中；找不到就 skipped，不猜路径）
source = { bundle = "@deepseek-ai/dsh-experimental-agent-team-profile", file = "cordis.patch.yml" }
append = """
- id: agent-team
  name: "@deepseek-ai/dsh-experimental-agent-team"
  config:
    maxMembers: {{max_members}}
    maxTasks: 256
    maxMessageBytes: 65536
    disposalTimeoutMs: 5000
"""

[params]
on_missing = "skipped"                   # 镜像里找不到官方组合包 → 跳过并说明

[params.max_members] type = "integer"   required = true   secret = false
```

```toml
# capabilities/dsh-file-download/manifest.toml —— kind = plugin
id = "dsh-file-download"
version = "1.0.0"
kind = "plugin"
title = "工作区文件下载"
compatible_dsh = ">=0.2.1-rc.2"
marker = "# dsh-control: dsh-file-download 由控制台铺设"
# 路由注册成功的可观测信号：未登录打 /api/download 回 401，未注册的 /api/* 回 404
# （dsh-family plugins/README.md 实测的两种返回，正好能区分「装上了」和「没进 handler」）
# 探针依赖容器内有 curl：未实测，与 U-4 一起核
status = "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${DSH_PORT}/api/download | grep -q 401"
healthcheck = "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${DSH_PORT}/api/download | grep -q 401"
uninstall = "reverse"
restart_required = true
packages = ["dsh-file-download", "dsh-host-file-download"]   # UI 半 + host 半，一对
enable_entries = [
  { id = "host-file-download", name = "dsh-host-file-download" },
  { id = "ui-file-download",   name = "dsh-file-download" },
]
laid_by = "entrypoint"                   # 谁动手铺：见 U-4，v1 倾向保持现状

[params]
on_missing = "failed"                    # 包在 kit 里，缺文件属于安装问题，不该静默跳过
```

```toml
# capabilities/memory-hub/manifest.toml —— kind = service
id = "memory-hub"
version = "2.0.3"                        # 钉上游 release 版本（vendor §1.1）
kind = "service"
title = "团队记忆中枢"
compatible_dsh = ">=0.2.0-rc.1"          # 官方 DSH 集成的要求；镜像内实际版本待核（vendor §7 第 2 条）
marker = "# dsh-control: memory-hub 由控制台铺设"
status = "nc -z {{memory_host}} 8096 && nc -z {{memory_host}} 8420"   # 主机级探针：Proxy 与 Core 都通（HTTP 探针待 P0 联调定）
healthcheck = "nc -z {{memory_host}} 8096"
uninstall = "reverse"
restart_required = true
compose = "compose.fragment.yaml"        # Core 8420 / Hub 8125 / Knowledge 8424 / Proxy 8096，tag 钉死在片段里
external_api = "memory_hub"              # 控制台第 4 个适配器：建用户、verify、停用（vendor §4）

[wiring]
# 实例侧只改配置：home 层补丁把模型出口指向 Proxy（不带 /v1，官方 adapter 自己拼 /v1/messages）
patch = { key = "baseURL", value = "http://{{memory_host}}:8096/dsh/{{space_id}}" }
secret_target = { file = ".credentials.yaml", mode = "600", key = "PROXY_USER_KEY" }

[params]
on_missing = "skipped"                   # 开关关 / 栈不通 → skipped + 警告，记忆没铺成不挡开通（vendor §4 第 3 条）

[params.memory_host]  type = "string"   required = true   secret = false
[params.space_id]     type = "string"   required = true   secret = false   # 粒度未决，见 U-1
[params.user_key]     type = "string"   required = true   secret = true    # sk-mem-… 32 位，控制台经 external_api 建好后从 stdin 注入
```

manifest 的解析规则（v1）：

- 未知字段 → 发布时就拒（`kit publish` 报错），不等到铺到一半才发现。宁可不装，不要半装。
- 参数名与值只认 manifest 的 `params` schema；引擎按 schema 取值、渲染、校验，不猜键名。
- `secret = true` 的参数只能从 stdin 进容器，任何出口（详情响应、`deploy_steps.detail`、日志）都不回显。
- `marker` 是安装与卸载之间的唯一契约：改 marker 文本 = 换了一次幂等判定，必须升 `version`。
- 探针只能依赖目标环境已有的命令（slim 镜像里有没有 `curl` / `nc` 属未验证事实，实现时逐个核，
  缺命令就在观测里记 `health = "unknown"` 并说明，不静默判健康）。
- 插值只允许 `{{param}}` 引用该能力 `params` 里声明过的参数，以及 `${DSH_HOME}`、`${DSH_PORT}`
  这类容器内环境变量；出现别的 `{{…}}` 视为未知字段，按第一条拒。

## 决策三：入口、探针与步骤结果

### 两层入口，一条执行路径

**a) 控制台管理 CLI**（并入 `cli.py`，与 `kit publish` / `fontlib publish` 同款：读目录、发工单）：

```
dsh-control capability ls                        # 列目录（capabilities 表）
dsh-control capability publish --dir <kit 仓库>  # 读包内 manifest 进目录（与 kit publish 同一事务）
dsh-control capability status <slug> [--json]    # 现采现报某实例观测态（排障用）
dsh-control capability install <slug> <id>[@ver]
dsh-control capability apply <slug>
dsh-control capability disable <slug> <id>
dsh-control capability enable <slug> <id>
dsh-control capability uninstall <slug> <id>
```

动作类子命令**只入队工单并打印 job id**，与页面点的是同一条路——不在 CLI 里另写一套执行。
参数不放在命令行上：引擎按 manifest 的 `params` schema 从站点配置与 `credentials` 现取
（现状即这两处：`image_api_url` 与 `credentials(kind=image_key)`、`team_max_members`），
命令行没有 `--token` 这类旗标，密钥永远现取现塞 stdin（硬规则第 3 条）。

**b) 页面 / API 的点击操作**（用户点名的点装、升级、停用、卸载）：
`POST /api/instances/{id}/capability`，body `{"action": "install|apply|enable|disable|uninstall",
"capability": "<id>", "version": "<可选>", "params": {…非敏感参数}}` → `{job_id}`；
站点级 service 升级用 `deploy_jobs(instance_id=0, kind=capability)`，沿用 `publish` 的先例。

**执行路径只有一条**：引擎按 manifest 拼出命令，经 SSH `docker exec` 打进目标容器执行，
stdout/stderr 逐行进 `deploy_steps`（§5.1 的 SSH 执行器不改）。缺料跳过、幂等短路、回读校验
都在引擎里按 manifest 判，不写死在某个能力的代码里。

### 观测：跑探针，不跑主机 CLI

watcher 每轮采集时顺带采能力观测态（与运行态采集同一批 SSH），落 `instance_capabilities`。
一条记录的结构：

```json
{
  "capability_id": "comfy-image",
  "kind": "skill",
  "installed": true,
  "enabled": true,
  "observed_version": "1.0.0",
  "marker_present": true,
  "health": "ok",
  "health_detail": "",
  "reason": "",
  "observed_at": "2026-10-29T06:31:12Z",
  "dsh_version": "0.2.1-rc.2",
  "dsh_version_source": "dump-config"
}
```

- 只报观测态，不报期望态：期望住在台账，比对在控制台进程里做。
- `marker_present` / `installed` 来自 manifest 的 `status` 探针；判据按 kind 定：`skill` 看目录与
  `SKILL.md`，`patch` 看 home 层里的 marker 段，`plugin` 看启用条目与 `node_modules/` 里的包，
  `service` 看实例 home 层的接线配置。
- `observed_version` 优先读落地文件里的版本戳（引擎铺的时候写，形态照 `kit/.dshctl-kit-version`：
  一行版本号），读不到留空并在 `reason` 里说明——**空不等于 0.0.0**，对账时按「版本未知」黄标而不是按漂移算。
- `health` ∈ `ok` / `failed` / `unknown`（未装、无探针、或探针命令在目标机不存在）。健康失败不改变 `installed`。
- `dsh_version` 现读口径见 U-2；`dsh_version_source` 必带，闸门与展示同一口径。
- 容器里 `${DSH_HOME}` 取生效值（现状 `_data_home` 那次 SSH），不猜路径；输出里不出现密钥明文。

### 步骤结果语义（不是退出码）

契约层只有 `deploy_steps.state` 已有的三态，不新造退出码表：

| 结果 | 触发 | 工单表现 | 先例 |
|---|---|---|---|
| `succeeded` | 装成 / 卸成 / 本来就对（幂等短路） | step `succeeded`，detail 写「铺了什么、落在哪」 | `_team_patch_script` 见 marker 就 `exit 0` |
| `skipped` | 缺料且该能力 `on_missing = skipped`：站点没配生图、镜像里找不到官方组合包、记忆开关关或栈不通 | step `skipped` + 一行警告，**工单不红** | **comfy 先例**：第 66 步「缺材料跳过，不挡住开通」；`skipped` 本来就在 `STEP_STATES` 里 |
| `failed` | 参数不合 schema、`on_missing = failed` 的缺料、容器不在跑、取不到 `$DSH_HOME`、变更中途挂、外部 API 调不通 | step `failed`，detail 取 stderr 末 800 字节（已脱敏）；紧接一次探针复算写进 detail | §5.1 约定 |
| `failed` + 台账 `health = failed` | 已落地但健康探针不过 | `failed`，**不自动回滚**（铺没铺成不推翻开通） | 第 66/67 步现状 |

`--dry-run` 保留在**管理 CLI** 上（API 层永远真执行，§4.1 的注），语义与适配器的 `dry_run` 一致——
报告本来要做什么，一个字节都不写：

- 不写文件、不动 compose、不调 `external_api` 的写动作、不入队工单。
- 逐条打印本来要做的动作：动作 + 目标路径或 marker + 依据（哪个参数、manifest 哪条字段）。
- 幂等判定照做，`already-in-desired-state` 与 `would-change` 分开报——这是页面「重跑一遍」会不会点空的依据。
- 探针照跑（只读），兼容闸门照判，照实返回结果。

## 决策四：控制台侧两张表

遵守 ARCHITECTURE §2.0 三条规矩：不用外键、不写联表、每列带 `comment`。
表注释与列注释在 Alembic 的 `op.create_table` 里同样带 `comment=`。

```python
class Capability(Base):
    """能力目录：一行一个「能力 + 版本」，正文来自 kit 包里的 manifest。"""

    __tablename__ = "capabilities"
    __table_args__ = (
        _in("kind", ("skill", "patch", "plugin", "service")),
        _in("state", ("draft", "published", "retired")),
        sa.UniqueConstraint("capability_id", "version"),
        {"comment": "能力目录：id/版本/kind/manifest/兼容区间/发布状态（发货台账的一类）"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    capability_id: Mapped[str] = mapped_column(String(64), index=True,
                                               comment="能力 id，等于 kit 里 capabilities/<id> 目录名")
    version: Mapped[str] = mapped_column(String(64),
                                         comment="包版本，取 manifest 的 version")
    kind: Mapped[str] = mapped_column(String(16),
                                      comment="能力类别：skill/patch/plugin/service，决定装法与卸法")
    title: Mapped[str] = mapped_column(String(128), default="", comment="详情页展示名")
    manifest: Mapped[dict] = mapped_column(JsonType,
                                           comment="manifest 解析结果全量（参数 schema 与 kind 专属字段都在里面）；"
                                                   "kit publish 时读包内文件解析，有未知字段就拒发")
    manifest_sha256: Mapped[str] = mapped_column(String(64), default="",
                                                 comment="manifest 正文 sha256：与目标机上换装落地的文件比对，发现货与台账不符")
    kit_version: Mapped[str] = mapped_column(String(64), default="",
                                             comment="这份 manifest 随哪个 kit 版本发货，逻辑指向 kit_releases.version，无外键")
    compatible_dsh: Mapped[str] = mapped_column(String(64), default="",
                                                comment="兼容的 DSH 版本区间原文，切镜像前的闸门读它")
    on_missing: Mapped[str] = mapped_column(String(16), default="failed",
                                            comment="缺料策略：skipped=步骤记 skipped（不挡开通）；failed=步骤失败")
    state: Mapped[str] = mapped_column(String(16), default="published", index=True,
                                       comment="发布状态：draft/published/retired——只有 published 可铺")
    current: Mapped[bool] = mapped_column(default=False,
                                          comment="是否现行版：同一 capability_id 最多一行为真，发布时代码换行（同 kit_releases）")
    note: Mapped[str] = mapped_column(String(512), default="", comment="发布说明")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow, comment="更新时间（UTC）")


class InstanceCapability(Base):
    """每实例每能力一行台账：desired ↔ observed。"""

    __tablename__ = "instance_capabilities"
    __table_args__ = (
        _in("desired_state", ("present", "absent")),
        _in("health", ("unknown", "ok", "failed")),
        sa.UniqueConstraint("instance_id", "capability_id"),
        {"comment": "能力台账：期望版本 ↔ 引擎探针采回的观测态"},
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, comment="主键")
    instance_id: Mapped[int] = mapped_column(BigInteger, index=True,
                                             comment="逻辑指向 instances.id，无外键")
    capability_id: Mapped[str] = mapped_column(String(64), index=True,
                                               comment="逻辑指向 capabilities.capability_id，无外键")
    desired_state: Mapped[str] = mapped_column(String(16), default="present",
                                               comment="期望：present=要装；absent=要卸（卸载工单先翻这列再发命令）")
    desired_version: Mapped[str] = mapped_column(String(64), default="",
                                                 comment="期望版本；空串=跟随目录的 current 行")
    params: Mapped[dict] = mapped_column(JsonType, default=dict,
                                         comment="下发给引擎的非敏感参数；secret 参数不进这列（密钥只住 credentials，"
                                                 "工单执行时现取现塞 stdin）")
    installed: Mapped[bool] = mapped_column(default=False, comment="观测：装没装（status 探针的结果）")
    enabled: Mapped[bool] = mapped_column(default=True, comment="观测：启没启；disable 之后资产还在，只是不生效")
    observed_version: Mapped[str] = mapped_column(String(64), default="",
                                                  comment="观测版本；空串=版本未知（不按漂移算，按黄标提示），与期望不等即漂移")
    marker_present: Mapped[bool] = mapped_column(default=False, comment="观测：铺设记号在不在——文件被人手删过的证据")
    health: Mapped[str] = mapped_column(String(16), default="unknown", index=True,
                                        comment="健康探针结果：unknown/ok/failed（failed 只黄标，不自动重启实例）")
    health_detail: Mapped[str] = mapped_column(Text, default="", comment="健康探针输出摘要（已脱敏）")
    reason: Mapped[str] = mapped_column(Text, default="",
                                        comment="上次动作的说明：skipped 原因 / 失败尾日志（已脱敏），详情页直接显示")
    last_job_id: Mapped[int] = mapped_column(BigInteger, default=0,
                                             comment="最近一次动作的工单 id，逻辑指向 deploy_jobs.id，无外键；0=没走过工单")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         comment="本行观测态采集时间（UTC）；空=从没采过")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow, comment="更新时间（UTC）")
```

配套的契约变更（同一篇评审一起过）：

- `deploy_jobs.kind` 增加 `capability`：升级 / 重试 / 禁用 / 卸载都是工单，享受 SSE 日志、失败续跑、
  events 留痕，与现有工单同构（vendor §3.3）。
- `assets.py` 的 kit 清单（`_KIT_DIRS` / `_KIT_TOP`）增加 `capabilities/`；`kit publish` 顺手读包内
  manifest 进 `capabilities` 表（同一次发布 = 同一版本的一批货，U-3 里「目录行由谁写」那一问就此消解）。
  第 20 步换装范围随之扩到 `capabilities/`，照旧只增盖、不动 `instances/`。
- ARCHITECTURE §6 步骤号不动：第 66 步 `capabilities` 与第 67 步 `fonts` 保留，正文从「拼 heredoc」
  改成「引擎按 manifest apply」；restart 第 5 步同理。
- 对账在程序里做，两张表不联：`WHERE capability_id IN (...)` 取目录，再逐行比 `observed_*`，
  漂移或健康失败 → 详情页黄标 + 「升级 / 重试 / 禁用 / 卸载」四个按钮。
- 新模块落点建议 `src/dsh_control/integrations/capabilities.py`：manifest 解释、命令拼装、探针回读
  都是「外部世界的动作」，与 §5 适配器同族；`jobs/capabilities.py` 收敛成调用它的编排薄层。

## 决策五：三层升级边界

三层各走既有通道，互不纠缠（vendor §3.3）：

| 层 | 载体 | 通道 | 谁触发 | 现状 |
|---|---|---|---|---|
| DSH 本体 | dsh-family 镜像 | 制品库（§6.1.5）→ `POST /api/instances/{id}/deploy` 带 `{image}`，同实例原地重建 | 运维点「升级/重建」 | 已有 |
| 能力包（货） | kit 目录（含 `capabilities/`） | `dsh-control kit publish` 入库 → 第 20 步比对 `kit/.dshctl-kit-version`，不符整包下发 → 第 66 步按 manifest apply | 开通/升级自动 | 通道已有，缺清单化 |
| 外部服务镜像 | 官方镜像 tag 钉在 `compose` 片段里 | 目录发布新 tag → 站点级 `capability` 工单跑 `compose up -d`，实例无感 | 运维在目录页发布 | 新建，薄 |

控制台本体（引擎自己）跟控制台镜像一起发，不算第四层：引擎只认 manifest 的字段，
货比引擎新时会撞「未知字段」并拒绝发布，不会出现「kit 新、引擎旧」的半套状态。

兼容闸门（拦在切镜像之前，不靠肉眼）：

1. 升级工单进入第 60 步（容器重建）之前，取该实例台账里所有 `desired_state = present` 的能力。
2. 逐个用目标镜像的 DSH 版本比 `compatible_dsh`（现读口径见 U-2）。
3. 任一能力不兼容 → 该步 `failed`，detail 点名是哪个能力、哪个区间、当前版本，
   给「先禁用它再升」的下一步；**绝不静默升上去**——file-download 的 README 自己写着官方插槽改名时
   按钮会「安静地不画」，这类失败不会让容器变红，只能靠闸门。
4. 镜像升级后 kit 换装照旧，`apply` 照旧补铺：home 层与技能目录重启后补验，缺则补铺。

kit 版本戳 `kit/.dshctl-kit-version`（现值 `kit-20261027-061737`）语义不变：一行版本号，
第 20 步拿它与 `kit_releases.current` 比。能力包随 kit 一起换，不给能力单开版本戳——
否则「kit 新、能力旧」这种半套状态没人能判定。

## 迁移路径

沿用 vendor §6 的分期，本篇只定「什么算能动手」：

- **P2 speckit 先行。** speckit 是唯一「纯新增、无历史包袱」的能力：`kind = skill`，base64 内联推送，
  实例不出 PyPI；站点开关默认开、实例级可关。用它把整条缝跑通一遍——manifest → kit 包 →
  `kit publish` 进目录 → 引擎 → 两张表 → 页面上的升级 / 禁用 / 卸载。验收：新开实例 `/speckit-specify` 直接可用。
- **P3 收编存量。** comfy-image、agent-team、fonts 迁进清单，`dsh-file-download` 两个自研包登记进目录，
  控制台删 heredoc。验收：`capabilities.py` 只剩编排。

P3 动手的信号（四条都满足才开干，否则继续留在 heredoc）：

1. speckit 在 P2 已经跑通「装 / 升 / 禁 / 卸」四个动作，且都是工单，不是人工 SSH。
2. 兼容闸门拦下过一次真实冲突，或有可执行的测试用例证明它会拦（闸门没咬过人就等于没有闸门）。
3. 迁移期双跑对账：同一实例上旧 heredoc 铺设一次、引擎探针采一次，两者的结论一致
   （marker 在/不在、目录在/不在），连续若干轮无差异。
4. agent-team 的 marker 文本与 `TEAM_MARKER` 逐字相同——它是现存实例卷里唯一的幂等依据，
   差一个空格就会把所有历史实例重铺一遍。

## 未决问题（2026-10-29 拍板记录）

运维者口径：「都是些你自己可以找到或者自己探究到结论的点」——能自行取证的一律自决。
以下每项给出处置；仍开放的只剩观察期项。

- **U-1 `spaceId` 粒度 → 定案：全家一个 space（`default`）**。Team/Agent/Task 已提供治理隔离，space
  再切分对家庭规模是重复结构。记忆质量若要对比，观察期内改一个实例的 baseURL 路径即可试验。
- **U-2 `compatible_dsh` 比对口径 → 定案：① 现读**（`read_dsh_config` 已在跑的 dump-config 与容器
  facts，多一趟 SSH 约 0.6 s），观测必带 `dsh_version_source`；台账 `instances.dsh_version` 只做展示，
  闸门不信它（换镜像后可能滞后一轮）。
- **U-3 能力包产物存放阈值 → 定案：单文件 > 2 MB 或整包 > 8 MB 走制品库文件账本**（fontlib 同款），
  其余随 kit JSONB 整包（现约 100 KB，file-download 两插件为百 KB 量级，留在 kit）。阈值写进
  `capability publish` 的发布校验，超了直接拒收并提示走制品库。
- **U-4 plugin 由谁铺 → 定案：① kit entrypoint**（现状即此）。插件源随 dsh-family **镜像**
  （`COPY plugins/ /opt/dsh/plugins/`），每次启动重拷防旧版残留；`laid_by = "entrypoint"`。
  控制台的 disable/uninstall 对 plugin 类只改 cordis 补丁里的启用条目，不动运行时副本；
  要根治「启动打回」就不给 plugin 类提供 console 铺法。
- **U-5 fonts 归类 → 定案：新增第五类 kind `asset`**（文件投递语义：`target` 目录、`cp -n` 不覆盖、
  无重启、健康判据 = 文件在位与计数）。skill 语义是「喂给 agent 的知识」，字体是「喂给系统的文件」，
  强行归 skill 会让 marker/覆盖语义两边打架。vendor §6 P3 的「fonts 迁进清单」按 asset 类执行。
- **U-6 `disable` / `enable` 实现**：按 vendor P2 验收（升级/禁用/卸载全在页面跑通）时一并定，
  原则已有：disable 必须可逆、重启型能力禁用后不追着重启（等自然重启，台账记「待重启生效」）。
- **U-7 purge 台账行**：P1 purge 记忆处置落地时一并定。倾向：行保留供审计，`desired_state` 翻
  `absent`（purge 即「不再期望」），restore 时按 events 恢复期望。
- **U-8 speckit 只铺技能不烙 CLI → 定案：接受**。实例交互用法技能文件已够；`specify` CLI 属镜像层
  决定，将来 P4 若做 workflow 自动化（bug 修复流水线）再提镜像议题。

vendor §7 清单取证结果（同日）：镜像内 dsh `0.2.1-rc.2` ≥ v0.2.0-rc.1，
`@deepseek-ai/dsh-llm-deepseek-api-key` 以 dsh 包**内嵌依赖**分发
（容器 `/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/` 实见），**无需升级**；
`.credentials.yaml` 合并 `refs:` 写法 + 600 已在四台实例跑活；三件套实测
core 606 MiB / hub 79 MiB / proxy 92 MiB，CPU 空闲个位数；元数据 API 同名 `user/create`
不去重（可传确定性 `user_id` 实现幂等），55 接口文档在 `MemoryCore/v3-api-memorycore-doc.md`。
