---
name: dsh-control 控制台
description: 家庭 DSH 实例的港务理货台式运维台账
colors:
  dock-deep: "#0a1c24"
  dock: "#0f2731"
  dock-raised: "#16323d"
  dock-ink: "#e9f1f3"
  dock-muted: "#9dbac5"
  cool-paper: "#edf1f2"
  card-white: "#ffffff"
  hairline: "#d6dee1"
  ink: "#14232c"
  ink-soft: "#445a66"
  one-voice: "#0b6478"
  stamp-ok: "#0c7a4f"
  stamp-warn: "#8a5a00"
  stamp-err: "#b13124"
  stamp-off: "#5c6e77"
typography:
  wordmark:
    fontFamily: "ui-monospace, 'SF Mono', 'Cascadia Mono', 'DejaVu Sans Mono', monospace"
    fontSize: "15px"
    fontWeight: 700
    letterSpacing: ".01em"
  display:
    fontFamily: "-apple-system, 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif"
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.35
  body:
    fontFamily: "-apple-system, 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: "-apple-system, 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif"
    fontSize: "12.5px"
    fontWeight: 550
    letterSpacing: ".08em"
  machine:
    fontFamily: "ui-monospace, 'SF Mono', 'Cascadia Mono', 'DejaVu Sans Mono', 'Noto Sans Mono', monospace"
    fontSize: "12.5px"
    fontWeight: 400
rounded:
  hair: "3px"
  soft: "5px"
spacing:
  tight: "8px"
  row: "14px"
  block: "16px"
components:
  button-primary:
    backgroundColor: "{colors.one-voice}"
    textColor: "#f2f7f8"
    rounded: "{rounded.soft}"
    padding: "8px 18px"
  button-primary-hover:
    backgroundColor: "#0a5467"
  button-danger:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.stamp-err}"
    rounded: "{rounded.soft}"
    padding: "8px 18px"
  chip-state:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.stamp-ok}"
    rounded: "{rounded.hair}"
    padding: "2px 9px"
  card-surface:
    backgroundColor: "{colors.card-white}"
    rounded: "{rounded.soft}"
    padding: "16px"
---

# Design System: dsh-control 控制台

## Overview

**Creative North Star: "港务理货单"（the cargo manifest desk）**

这台控制台是一间深夜港务处：深蓝船坞壳（顶栏、登录夜港、日志舱）压在冷调浅青纸的工作面上，每一条 DSH 实例是一张待理货的单据。界面不为展示而存在——它是操作员扫描、核对、签放行的工作台，所以密度优先、色彩只服务于状态语义，装饰止步于印章与虚线。

一切表达走「先扫描、后操作」的节奏：列表页是理货台账，详情页是单据本体，新部署是逐条点亮的装卸步骤，而最重的动作（释放）展开成一张实体的放行条，操作员按住 1.5 秒在条上签字。字段的原始值（镜像 id、状态机枚举）收进次要折叠区——操作员看中文，不看出租机术语。

**Key Characteristics:**
- 双色分层：船坞壳只在顶栏 / 登录 / 日志舱出现，工作面永远是冷纸。
- 状态=印章：直角细框 + 几何符号 + 宽字距，颜色之外永远有形状冗余。
- 机器标识一律等宽（域名、token、端口、版本号），人话一律无衬线。
- 全站数字 tabular-nums；时间一律 Asia/Shanghai。
- 交互做减法：系统全站的输入只有登录粘贴、检索框与备注名铅笔改，其余全是点按与长按。

## Colors

冷调纸面 + 单一深青强调；四个状态色是法律，不随情绪漂移。

### Primary
- **One-Voice Deep Teal** (#0b6478): 唯一的声音。主行动按钮、选中态、焦点环、链接。任何一屏上的占比被刻意压小——它的稀有就是它的权威。

### Neutral
- **Dock Night** (#0f2731 / #0a1c24): 船坞壳面。顶栏、登录门、日志舱的深蓝夜。
- **Dock Ink / Muted** (#e9f1f3 / #9dbac5): 壳面上的文字，次级用 muted（对比 ≥7:1）。
- **Cool Paper** (#edf1f2): 工作面底色，冷青纸，不发暖。
- **Card White** (#ffffff) 与 **Hairline** (#d6dee1): 单据纸面与它的细线。
- **Ledger Ink** (#14232c / #445a66 / #647986): 正文 / 次级 / 仅大字或禁用态。

### Semantic stamps（契约钉死，永不换义）
- **Stamp Ok** (#0c7a4f on #e2f1e9): 运行中。
- **Stamp Warn** (#8a5a00 on #f5ecd6): 不健康 / 重启中，需要看一眼。
- **Stamp Err** (#b13124 on #f8e5e2): 错误 / 失败。
- **Stamp Off** (#5c6e77 on #e8edef): 已释放，档案灰。

**Named Rules**
- **The One Voice Rule.** 深青强调只给主行动、选中、焦点三件事；一屏至多一个主按钮。它落在哪，哪就是下一步。
- **The Two Docks Rule.** 深色壳面只允许出现在顶栏、登录门、日志舱。工作面上任何卡片永远白纸——夜是港口的，纸是理货的。

## Typography

**Display/Body Font:** 系统无衬线栈（PingFang SC / Noto Sans CJK SC / Microsoft YaHei 等，随系统就近取）
**Machine/Mono Font:** 系统等宽栈（SF Mono / Cascadia / DejaVu Sans Mono / Noto Sans Mono）

**Character:** 中文技术界面的正文交给系统无衬线以保证渲染稳态；所有"机器说的话"（slug、域名、token、端口、RecordId、时间戳、日志行）切换到等宽，形成一条肉眼可辨的机器墨水带。字重靠 650/550/400 三档拉层级，不靠字号尖叫。

### Hierarchy
- **Wordmark** (mono 700, 15px): 顶栏 dsh·control 标识，唯一的等宽标题。
- **Display** (650, 20px): 页级标题（工单号、任务名）。
- **Title** (650, 16px): 卡片头。
- **Body** (400, 14px/1.65): 一切正文；CJK 与 Latin 混排天然对齐。
- **Label** (550, 12.5px, .08em): 印章、卡头小标、统计条。
- **Machine** (mono 12.5px): 机器标识带。

**Named Rules**
- **The Machine Ink Rule.** 任何非人类输入的标识符必须等宽呈现；禁止用无衬线渲染域名或 token。
- **The Even Digits Rule.** 全站 tabular-nums；一切数字列、计数、时钟必须竖排对齐。

## Layout

单一 shell：max-width 约 1440 居中，左右 20px（移动端 12px）。列表页 = 统计条 → 密集表格（行高 ~38px）→ 虚线释放档案区；详情页 = 12 栅格感的两列 grid2（主列 7fr / 操作列 5fr，@1020 塌为单列）；工单页 = 步骤 5fr + 日志舱 7fr，日志舱固定高 clamp(260px, 52vh, 560px)。@720 顶栏收缩、隐藏登录态文字、kv 单列。移动端不重排语义，只降级为横向可滚的表格与单列卡片。

## Elevation & Depth

平面优先：纸面上的卡片用 1px 细线 + 极轻投影（shadow-1: `0 1px 2px rgba(10,28,36,.07), 0 1px 1px rgba(10,28,36,.05)`）区分，弹窗才允许 shadow-2。深度主要靠**色层**而非阴影：夜壳→纸面→白卡的三段明度就是全部地形。日志舱内部靠深色自成一舱。

**The Flat Ledger Rule.** 台账面永远接近平面；阴影只回应状态（弹窗、按压），不做装饰性浮起。

## Shapes

直角哲学：3px（印章、小块）与 5px（按钮、卡片）两档极小圆角，绝不出现大圆角胶囊。签名几何是**虚线**：放行条的裁切线、释放档案区的封套边、确认卡的分类线——虚线=这张单可以被处置。印章芯片保持直角细框。状态冗余形状：● 运行、▲ 不健康、✕ 错误、◌ 已释放、◐ 部署中——灰度打印也不丢语义。

## Components

### Buttons
- **Shape:** 近直角（5px），细边框，8–18px 内距。
- **Primary:** One-Voice 底 + 近白字；hover 加深一档（#0a5467）。一屏一个。
- **Secondary:** 白底细线墨字；hover 描边加深。
- **Ghost:** 无边透明底；hover 上 accent-tint 底。
- **Hold-to-confirm:** 危险红描边按钮内藏一条按压进度条（scaleX 0→1，1500ms），按住 1.5s 才触发，松手即停；文案中途变「继续按住…」，触发后变「已签字，正在执行…」。键盘 Space/Enter 按住等效。

### Chips（状态印章）
- **Style:** 白/浅底 + 语义色细框（3px 圆角）+ 几何符号 + 宽字距中文，2–9px 内距。
- **Rule:** 颜色之外必有形状；released 永远配删除线感的灰。

### Cards
- **Corner:** 5px；**背景:** Card White；**边框:** 1px Hairline；**内距:** 16px，卡头 10–16px。
- 卡头 = 标题 + 次要右注（采集时间、版本 mono）。

### Inputs / Search
- 38px 高，白底 Hairline 描边，focus 描边转 One-Voice；搜索框左侧放大镜、右侧清空 ×；placeholder 用 ink-3。
- 登录门是唯一的深色输入：深壳底 + 等宽字体 + 眼睛切换。

### Navigation
- 船坞壳顶栏（52px sticky）：wordmark + 两个中文导航（当前项 shell-700 底）+ 右挂登录态与退出。@720 隐藏副标与登录态。

### Signature: 放行条（release slip）
危险动作在原地展开一张编号单据（NO.{id} · slug）：四条虚线行逐条列出将被注销的外部对象与「容器只停不删」，后果一行说死（不可恢复），底部是 hold-to-sign 按钮。不弹窗套弹窗；再想想即收起，什么都没发生。

**The Slip Rule.** 不可逆动作不弹窗打断，而是当场摊开一张写明所有注销对象的放行条，由操作员按住 1.5 秒签字；前端自动填入服务端要求的确认词，用户永远不打字。

## Do's and Don'ts

### Do:
- 状态色只用四语义（绿/琥珀/红/灰）且恒配形状冗余；灰=档案态。
- 机器标识一律等宽、数字一律 tabular；时间一律 Asia/Shanghai 格式。
- 复制反馈必须双通道：文案翻「已复制」+ 图标变钩（1.8s 自动回位），失败也说「复制失败」。
- 敏感值默认打码，点眼睛才展开；每次进页现取最新 token 并写明「当前最新」。
- 键盘可达：表格行 tabindex+Enter 进详情，弹窗原生 dialog（Esc、焦点收拢），「/」聚焦搜索。

### Don't:
- 不要在工作面上用深色壳、大圆角、彩色投影或 emoji/字体图标——图标只有手绘 SVG 一种。
- 不要为省事把 image_id、状态机枚举、desired_state 摆在主视图；它们属于「台账明细」折叠区。
- 不要在释放前要求用户输入任何文字（确认词由前端代填）；也不要弹窗套弹窗。
- 不要用暖色纸（米黄/奶油）当工作面底——纸是冷青的。
- 不要在公开的文案、注释、mock 数据里出现真实域名、IP 或站点标识。
