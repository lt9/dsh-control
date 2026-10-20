# 初始化与端到端验证手册

2026-10-20 与用户确认：**她那条实例不纳管，直接彻底删掉，用新项目从零重建**，
把「构建 → 开通 → 运维 → 释放」整条链子当测试用例跑一遍，最后留一个建好的给她用。
域名规则见 ARCHITECTURE §6.1.1：一级子域 `dsh-<标签>.pipboy.cn`（证书只覆盖一层）。

## 1. 主机种子（照抄，别编）

| name | ssh | 用户 | kit_path | data_root | frpc 客户端 | 备注 |
|---|---|---|---|---|---|---|
| `nas-8` | `192.168.3.8:2333` | `tuan` | `/home/tuan/dsh-family` | `/mnt/hdd2/dsh-family` | `tuan.c.TuanCloud@1` | 机械盘 `sdb1`，剩 1.7 T；系统盘 `sda2` 不许放实例数据 |
| `host-109` | `192.168.3.109:22` | `twan` | `/home/twan/src/dsh-family` | `/home/twan/dsh-data` | `tuan.c.TuanCloud@1` | 主控台自己这台 |

两台用户名不同（`tuan` / `twan`），NAS 的 22 是关的只有 2333。两台都已装控制台专用密钥
（注释 `dsh-control-console`，指纹 `SHA256:Qa8hgi7yWe/ZSg1LOxwiBIRSQGVQpD2EMQjWreO1HYs`），
`BatchMode` 免密可用，两个用户都有 docker 权限，均已验证。隧道一律挂
`tuan.c.TuanCloud@1`（它能用内网 IP 直连 NAS）。

实例种子：**空**。系统里一开始一条实例都没有。

## 2. 历史遗留 `dshsub1`：由我用手工步骤拆除，不进控制台代码

控制台的纪律是「只操作自己创建的对象」，所以拆旧东西不能做成控制台功能（否则它就有权删
别人建的东西了）。这一步由我按下面清单手工做，用户已明确批准删除这三处外部对象。
拆的顺序是**先断外网入口，最后才动容器**。

| 处 | 对象 | 标识 | 动作 |
|---|---|---|---|
| nginxWebUI | 站点 `dshsub1.pipboy.cn` | server_id `1310020104314032128`，location `1310020856361127937` | 删站点 |
| nginxWebUI | upstream `DSH_SUB_1` | id `1310019928367173632`，后端 `172.19.4.1:4892` | 站点删完再删 |
| frp-panel | 隧道 `dsh_1` | proxy_config id `2197`，client `tuan.c.TuanCloud@1` | 删（公网 4892 → 192.168.3.8:13080） |
| 阿里云 | A 记录 `dshsub1` | RecordId `2112523148163198976` | 删 |
| NAS | 容器 `dsh-wife` | 镜像 `5023c780` | 先 `backup.sh wife` 出一份归档并验 `.sha256`，再 stop |
| NAS | 数据目录 `/mnt/hdd2/dsh-family/wife/dsh-data` | — | **等新实例验证通过之后**再删；归档 `wife-*.tar.gz` 永久保留 |

删完核对：`list_configs` 里查不到 `dsh_1`、DNS 里查不到 `dshsub1`、
`getPage` 里查不到那个站点、其他 57 条隧道和其他站点数量一条不少。
**动手前先备份**：NG 的 conf 目录整份 tar、frp 的 `data.db` 拷一份、DNS 记录列表导出。

## 3. 端到端验证顺序（每步都留证据）

| # | 做什么 | 验收 |
|---|---|---|
| 1 | 控制台自身上线（`http://192.168.3.109:13300`） | 登录、空列表、无异常 |
| 2 | 控制台给自己开公网入口 `dshctl.pipboy.cn`（DNS → frp `dshctl_console` remotePort 4893 → NG 站点） | 公网 HTTPS 打开登录页；这是 frp/NG/DNS 三处第一次真写，每处先 dry_run 打印再执行，做完回读核对 |
| 3 | 建一次性测试实例 `dsh-probe`（放 `host-109`，别占她的机械盘） | 开通 9 步全绿；`verify.sh` 通过；公网一键地址能进 |
| 4 | 运维动作：重启它 | 重启后详情的 token/URL **变了且可用**（旧的应失效） |
| 5 | 释放它 | 二次确认词不匹配时拒绝；成功后 DNS/frp/NG 三处都查不到，**容器还在只是 stopped**，数据目录还在 |
| 6 | 拆掉历史遗留 `dshsub1`（§2 清单） | 见 §2 的核对项 |
| 7 | 给她建正式实例 `dsh-wife`（`nas-8`） | 开通全绿 + `verify.sh` 全绿 + 用她的手机路径真打开一次、真让它生成一个 Word 文档 |
| 8 | 采集运行态一轮、跑一次夜间备份链路 | 列表与详情显示真实状态；`backup.sh wife` 出归档 |

第 5 步和第 7 步之间不要插别的写操作，避免释放测试的残留污染她的实例。

## 4. 最终交付状态（跑完之后应该长这样）

- `dshctl.pipboy.cn` → 主控台（`192.168.3.109:13300`，frp remotePort 4893）
- `dsh-wife.pipboy.cn` → 她的 DSH（`nas-8` 上的 `dsh-wife`，健康，数据在机械盘）
- 库里：2 台主机、2 条实例（`dsh-probe` 状态 `released`、`dsh-wife` 状态 `running`）
- 外部：只有这两条新增的 DNS/frp/NG 记录；`dshsub1` 相关三处已删净；
  其余 57 条隧道与其他站点原样未动
- 归档：`/mnt/hdd2/dsh-family/backup/`（含迁移前的 `wife-*.tar.gz`，永久保留）
- 交给她的一句话：打开一键地址，一年后内不用再登录

## 5. 端口与名字分配表

| 用途 | 公网端口 | 内网 | 名字 |
|---|---|---|---|
| 历史遗留（待删） | 4892 | `192.168.3.8:13080` | 隧道 `dsh_1` |
| 主控台 | **4893** | `192.168.3.109:13300` | 隧道 `dshctl_console` |
| 测试实例 | 4894 | `127.0.0.1:<容器端口>` | 隧道 `dsh_probe` |
| 她的实例 | 4895 | `192.168.3.8:<容器端口>` | 隧道 `dsh_wife` |

`frp_remote_port` 由服务端从 `DSHC_FRP_PORT_START=4893` 起找第一个未被占用的，分配后写库，
不复用已释放实例的端口（避免别人手工建的东西正好接上）。

## 6. credentials 条目（`name` 用这些，值从环境变量读，别写进代码）

| kind | name | secret |
|---|---|---|
| `operator_key` | `default` | `{"key": "..."}`，init 生成并只打印一次 |
| `ssh_key` | `console-ed25519` | `{"private_key_pem": "..."}`，两台共用 |
| `aliyun_ak` | `pipboy-dns` | `{"access_key_id","access_key_secret","zone":"pipboy.cn"}` |
| `frp_panel` | `vps-9000` | `{"base_url":"http://8.134.168.14:9000","username","password"}` |
| `ng_panel` | `vps-18180` | `{"base_url":"http://8.134.168.14:18180","username","password"}` |

明文入库是用户明确要求（可读回），但只在 reveal 接口出网，日志与事件一律先过 `redact()`。

## 7. `.109` 上另外三个 dsh 容器不纳管

`dsh-office`(127.0.0.1:13097)、`dsh-uid`(13098)、`dsh-smoke`(13099) 是开发用的，没有域名
也没有隧道。列表页将来若要提示「主机上有未纳管的 dsh 容器」，做成只读提示即可，不自动收编。
