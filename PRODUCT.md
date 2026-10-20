# Product

<!-- impeccable:product-schema 1 -->

> 本文件在无人类应答机制下由显式简报推断写成（ask_user_question 探测对子代理报错：
> human interaction is unavailable）。推断条目逐条标注「推断」；其余为简报与
> docs/ARCHITECTURE.md、README.md 的直接事实。

## Platform

web

## Stack

React 18 + Vite + TypeScript（简报钉死的用户决定）；不用 UI 组件库，手写组件；无
CSS-in-JS 库，用一份成体系的 CSS + 自定义属性做 token。构建产物 frontend/dist 由
后端 FastAPI 直接 serve（单容器，端口 13300）。

## Users

唯一用户：运维者本人（推断：单机主为家人朋友搭 DSH，控制台只有一把运维密钥）。
他在处理家人朋友的 AI 办公助手实例：开通、看状态、重启、回收。场景多为桌面浏览器，
常在实例不健康后应急打开（推断）；也可能经公网 frp 从手机偶尔扫一眼（简报：移动端
不特殊照顾但不花屏）。

## Product Purpose

把散落在多台机器 Docker 里的 DSH 实例的「开通 → 查看 → 重启 → 回收」收进一个中文
运维主控台。成功 = 运维者不问人、不 SSH 手敲，就能完成全部日常动作并在故障时看到
足够的错误与事件线索。

## Positioning

它编排既有的外部系统（frp-panel、nginxWebUI、阿里云解析、SSH + dsh-family 脚本），
把「一条实例 = 一条解析 + 一条隧道 + 一个反代站点 + 一个容器」的生命周期做成可回滚、
可审计的点对点步骤；只操作自己创建的对象。别的通用面板给不了这条链路的整体性。

## Operating Context

- 工作流：登录（单运维密钥 cookie）→ 列表扫读状态 → 详情看运行态/网络/一键登录地址
  → 新建 = 一张已自动填好的确认卡 → 部署（点对点 9 步 + 实时日志）→ 完成后拿一键地址
  → 必要时重启或释放（不可逆，**按住 1.5 秒确认**，confirm=slug 由前端填）。
- **交互做减法（用户硬要求，§9）**：用户有选择困难症。不许用户为系统输入数据：新建、
  释放、重启、探活一律零打字（confirm=slug 由前端填），命名完全服务端生成（`dsh-NN`
  顺序），用户不参与。允许的用户主动输入只有三处（Lead 终裁）：登录页粘贴密钥；
  列表页检索框（用户故事第一条要求域名搜索）；详情页铅笔就地改备注名 owner_label（可选）。
- 系统初始为空：空状态是第一屏，按「第一次进来该怎么走」设计，新建是唯一主行动。
- released 实例真实存在（dsh-probe 测试后释放）：次要分组必须真能用——灰色、可见
  当年域名与主机、无恢复按钮、详情只读不可操作。
- 数据面事实（来自契约）：状态机 pending|deploying|running|unhealthy|restarting|
  released|error；重启必然轮换 token，详情页必须现取最新 token；日志按 after= 增量
  或 SSE 流。
- 释放后果：删解析/隧道/nginx 站点，容器只停不删，数据保留，列表仍可见但不可恢复。

## Capabilities and Constraints

- 四个界面：登录、列表（表格+released 次要分组；筛选只许点击不许打字）、详情
  （运行态/网络/配置解释弹窗/一键复制/token 打码可展开/重启/释放按住确认/事件
  时间线/铅笔改备注名）、新建（单张自动确认卡 → 步骤进度+实时日志 → 一键地址）。
- 用不上的字段（image_id、状态机原始值）收进次要折叠区或不显示。
- 契约字段名逐字照 docs/ARCHITECTURE.md §4；前端唯一对接依据，不臆造字段。
- 写操作带头 `X-Requested-With: 1`；前端不存 token，靠 httpOnly cookie。
- 所有时间按 Asia/Shanghai 显示；密钥/token 默认打码，点击才展开，不进 URL/日志。
- 状态色语义固定（简报+契约钉死）：running 绿；unhealthy/restarting 琥珀；error 红；
  released 灰。
- 未决：主机管理界面（契约只有 GET /api/hosts，无增删改）——本界不做主机 CRUD。

## Brand Commitments

- 界面语言中文；中英混排排版要好看，数字用 tabular figures。
- 模式 Operate：扫读效率、一致性、原生交互预期优先，不做营销式花活（简报钉死）。
- 键盘可达：表格行 Tab/Enter 进详情；弹窗 Esc 关闭、焦点收拢。

## Evidence on Hand

- docs/ARCHITECTURE.md：完整接口契约与响应 JSON 形状（§4），前端约定（§9）。
- README.md：产品边界与安全边界。
- 无后端可跑（backend 近乎空）：自检用 frontend/mock/ 假接口按契约返回固定数据。
- 无品牌资产、无 logo——不臆造。

## Product Principles

1. 扫读优先：一屏之内先回答「谁活着、谁出事、在哪台机器」。
2. 不可逆动作要慢但不许打字：释放用 1.5 秒按住确认 + 一屏讲完后果（§9 交互做减法）。
3. 状态即语言：状态色、进行中指示、复制反馈全站同一套语义。
4. 密钥是敏感物：默认打码、按需展开、只在明确的 reveal 里出现。
5. 精确即品牌：品牌感藏在间距、对齐、排版与状态细节里，不在装饰里。

## Accessibility & Inclusion

键盘完整可达（焦点可见、弹窗焦点收拢、Esc 关闭）；状态不只靠颜色（色+形+文案）；
中文正文行高与混排基线按 CJK 排版校正。
