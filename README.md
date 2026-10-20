# dsh-control

给一群分散在多台机器 Docker 里的 DSH 实例做**开通 → 查看 → 重启 → 回收**的运维主控台。
单容器部署，登录后是一个中文运维界面：列表、详情、新建向导（逐条呈现部署步骤与日志）。

- 契约与实现依据：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 被管实例本身的部署套件是另一个仓库（dsh-family），本控制台只编排它，不重复实现部署逻辑。

## 它管什么

一个实例 = 一个人的 DSH 容器 = 一条域名解析 + 一条 frp 隧道 + 一个 nginx 反代站点。

开通按 DNS → frp → nginx → 容器 → 抓登录密钥 → 公网探活 → 复核 的顺序做，每步可回滚；
回收按 nginx → frp → DNS 的顺序断开，然后**只停容器不删容器**，数据保留，列表仍可见但不可恢复。

## 依赖的外部系统

| 系统 | 用途 | 接入方式 |
|---|---|---|
| PostgreSQL | 元数据与密钥 | 复用 ai-seo-platform 那个实例，独立库 `dsh_control` |
| frp-panel | 开/关公网隧道 | `POST /api/v1/proxy/{list_configs,get_config,update_config,delete_config}` |
| nginxWebUI | 建/删反代站点 | `/api/*` + `token` 头 |
| 阿里云解析 | 建/删 A 记录 | DNS RPC 签名 v1 |
| 被管主机 | 部署与取状态 | SSH（控制台专用密钥）+ dsh-family 现成脚本 |

## 安全边界

只操作本控制台创建的对象；frp/nginx/DNS 上的既有配置一律只读。释放是不可逆动作，
必须逐字输入实例 slug 二次确认。密钥集中存库，日志与错误信息里不出现明文。
