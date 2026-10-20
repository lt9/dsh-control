---
version: 1
slug: "frontend-src-main-tsx"
primary_target: "frontend/src/main.tsx"
related_targets: []
---

# Surface brief — dsh-control 主控台（Operate）

## Scope & mode
- 主目标：frontend/src/main.tsx（四个界面：登录 / 列表 / 详情 / 新建+任务）。模式 Operate（简报钉死）。
- 用户：唯一运维者，桌面为主，深夜应急 + 白天巡检（推断）。系统初始为空；空状态是第一屏。
- 硬约束（§9 交互做减法）：新建=单张自动确认卡；释放=按住 1.5s；无弹窗套弹窗；机器字段收
  次要区；确认词等系统字段前端代填。输入白名单（Lead 22:5x 终裁）：登录粘贴、列表检索、
  备注名铅笔改——检索是用户主动发起的动作，不算为系统输入数据，搜索框恢复（?q= 进 URL，
  250ms 防抖，/ 键聚焦）。

## Audience / job / task
运维者为家人朋友开通、查看、重启、回收 DSH 实例；动作全部走 HTTP 契约（ARCHITECTURE §4），
释放不可逆（confirm=slug 由前端填），token 只在明确动作里现取、默认打码。

## Chosen direction — 港务理货单（cargo manifest）
- 系统机制一句话：把「一条实例 = 一条解析 + 一条隧道 + 一个站点 + 一个容器」的进出港全生命周期
  做成可审计的点对点账目，只操作自己创建的对象。

## DIRECTION CONTRACT

THESIS: 这个控制台是「家人朋友实例港」的理货账台——每条实例是一件有名字、有泊位、有进出港记录的
件货；拒绝灰底单强调色的通用 dashboard 默认排版。

OWN-WORLD: 深蓝船坞壳（顶栏/登录页/日志面板）压冷调浅青纸工作区；水蓝强调色只出现在主行动、
选中与焦点；状态四色语义固定（running 绿 / unhealthy·restarting 琥珀 / error 红 / released 灰），
色之外再叠形状冗余；状态 chip 是印章式（直角细框、宽字距、几何点）；slug/域名/端口/token/时间戳
一律等宽字 + tabular figures；中文正文用系统 CJK 黑体栈，密度向理货单看齐（38px 行、多列密排）。

STORY: 扫一眼谁活着谁出事 → 进详情拿最新一键地址（打码可展、复制有反馈）→ 开通只看一张自动填好
的确认卡，然后盯 9 步点对点进度与滚动日志 → 不可逆的释放走放行条仪式。

FIRST VIEWPORT: 深色顶栏横贯（wordmark、实例/新建、会话剩余、登出）；其下浅色工作区：概览状态条
（可点击的计数筛选，零打字）、右侧「新建实例」主按钮、七列密集表格，released 折叠为次要分组；
空系统时该屏即首跑引导：一句人话解释管什么 + 唯一主行动「新建实例」。

FORM: 港务理货单（grounded candidate 7，seed:35bd3d9278963e9a）。世界只借字体、色板、密度与一个
签名动作，布局与控件全部是 web 标准件。签名动作 = 放行条：释放以一张编号放行单在原地展开，逐行
列出将被注销的三个外部对象（解析记录/隧道/站点）与「容器只停不走」，运维者按住 1.5 秒即签字
（按钮上是按压进度），松手即撤销。诚实风险：港口隐喻禁止 literal 插画与纸纹皮肤，世界绝不接管
控件与布局。

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Unresolved
- 列表项 / hosts / events / /token 响应形状未在契约逐字给出——前端防御性解析。
- overview.by_host[] 形状未定义——防御性解析，仅做展示与点击筛选。
- GET /api/jobs/{id} 无 instance_id——终态一键地址靠路由参数兜底。
