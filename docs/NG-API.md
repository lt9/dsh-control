# nginxWebUI 对接依据（实测 + 面板自带文档）

面板：`http://8.134.168.14:18180`（容器名 `ng`）。`/api/*` 全部要 `token` 头，登录换 token 的
接口见 `TokenController`。厂商文档是 jar 里的 smart-doc 静态页，已抓到
[docs/vendor/nginxwebui/](vendor/nginxwebui/)，字段以它为准，别猜。

## 接口

| 用途 | 路径 |
|---|---|
| 站点增改 | `POST /api/server/insertOrUpdate` |
| 站点列表 / 查 | `POST /api/server/getPage` |
| 站点删除 | `POST /api/server/delete` |
| 位置（location）增改 | `POST /api/server/insertOrUpdateLocation` |
| 位置列表 | `POST /api/server/getLocationByServerId` |
| 位置删除 | `POST /api/server/deleteLocation` |
| upstream 增改 | `POST /api/upstream/insertOrUpdate` |
| upstream 后端增改 | `POST /api/upstream/insertOrUpdateServer` |
| upstream 列表 | `POST /api/upstream/getPage` |
| upstream 后端列表 | `POST /api/upstream/getServerByUpstreamId` |
| upstream 删除 | `POST /api/upstream/delete` |

有 `insertOrUpdateLocation`，所以**不要**退回"服务器级反代"——见下面那条坑。

## location 字段

`serverId, path, type, value, upstreamType, upstreamId, upstreamPath, rootPath, rootPage,
rootType, header, headerHost, websocket, cros, descr`（反向代理是 `type=2`，后端指向
`upstreamId`）。upstream 后端字段：`upstreamId, server, port, weight, failTimeout, maxFails,
maxConns, status, param`。

## 必须写对的一条：headerHost

DSH 的准入判定要求请求的 `Origin` authority 与 `Host` authority **完全相等**（默认端口会被
归一化，非默认端口不会）。浏览器对 WebSocket 握手必带 `Origin`，而 REST 的 GET 不带，所以
配错的症状是"页面能打开、REST 能用、只有 `/api/remote.mux` 一直 403，前端显示重连中"。

- 反代必须让上游收到 `Host: $host`，**不能是 `$host:$proxy_port`**。
- 面板里"服务器级反向代理"（不建 location）生成的 conf 固定写 `$host:$proxy_port`，
  所以必须建 location，并在 location 上把 `headerHost` 设成 `$host`、`websocket` 设成 `true`。
- DSH 侧没有任何 `trustProxy` / `X-Forwarded-Host` / 跳过 Origin 校验的开关（全树 grep 确认过），
  只能在反代这里对齐。

## 现存的 dshsub1 站点（只读参考，别改）

sqlite 里的真实形状（`mode=ro` 查的）：

```
server    id=1310020104314032128  server_name=dshsub1.pipboy.cn  listen=443  ssl=1
location  id=1310020856361127937  path=/  type=2  websocket=1
          header_host='$host:$proxy_port'      ← 与生效的 conf 不一致，见 SEED.md §2.1
upstream  id=1310019928367173632  name=DSH_SUB_1
upstream_server  172.19.4.1:4892               ← frps 的公网端口，从被管机视角是内网可达地址
```

生效的 conf 文件里已经是 `proxy_set_header Host $host;`（手工改的，备份在
VPS `/root/ng-backup-20261020-2115-dshsub1.conf`），所以现在是好的；但库里的值没跟上，
他在面板里重新保存就会回退。控制台把它做成详情页上显式的「修复反代头」动作，不自动做。

`header_host` 用 `$host` 的站点：`back.pipboy.cn`、`www.pipboy.cn`。用 `$host:$proxy_port`
且同样埋着这个雷的：`chat.pipboy.cn`（它现在 502 是上游没起，跟这个无关）。新建时默认
`$host` 就避开了。

## 幂等与删除纪律

- 创建前先按 `server_name` 查（`getPage`），存在就带上原 `id` 更新，不存在才建。
- 只删 `instances` 表里登记过 `ng_server_id` 且 `server_name` 与 `dns_name` 逐字相等的站点，
  否则抛 `NotManaged`。删站点前先记下 location 与 upstream 的 id 写进 `events`。
- 不要为了"顺手清理"去动任何未被登记的站点。这台面板上跑着他几十个其他站点的配置。
