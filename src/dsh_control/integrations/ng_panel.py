"""nginxWebUI 适配器（ARCHITECTURE §5.4）。

鉴权：``GET /token/getToken?name=&pass=``（明文口令，**不在 /api/ 下**）换
``obj.token``，之后所有 ``/api/*`` 带 ``token`` 请求头；401 自动重取一次。
面板地址与账号全部来自构造参数。

**写报文形状已按面板自身源码核定**（上游 cym1102/nginxWebUI 的
``ServerApiController`` / ``Location`` 实体 / ``ConfService``，对照部署 4.2.5
容器内嵌的 smart-doc 与前端 JS 逐项一致）：

- location 参数名按 smart-doc 全集给全（camelCase，见 ``LOCATION_PARAM_FIELDS``
  白名单，越界字段本地拒绝）。``type=2`` + ``upstreamId`` 是负载均衡反代的
  正确写法：ConfService 该分支生成 ``proxy_pass http://<upstream>`` +
  ``proxy_set_header Host <headerHost>`` + websocket 块。
- 读回用 ``getLocationByServerId?serverId=``（返回该站全部 location）。
- **API 面不生成 conf**：``/api/nginx/reload`` 只执行 ``nginx -s reload``。
  DB 到磁盘的物化入口是 ``/api/nginx/replace``（无参，内部 buildConf(从 DB)），
  校验入口是 ``/api/nginx/check``（把 DB 构出的配置写进 temp 跑 ``nginx -t``，
  不碰真实文件）。所以 ``reload()`` = check → replace → reload 三步，任何一步
  失败都抛错且不动磁盘（replace 会清空重建 conf.d，与面板 UI「覆盖配置」同义；
  面板把旧配置记入操作日志。在面板里直接手改过 conf.d 文件的会被重建掉）。

必须写对的点（都在代码里强制）：
- location 的 ``headerHost`` 固定 ``$host``：任何含 ``$proxy_port`` /
  ``$server_port`` / ``$host:`` 的值都在 ``_guard_header_host`` 处直接拒写
  （服务器级反代的 conf 固定 ``Host: $host:$proxy_port``，DSH 的 Origin/Host
  准入会把 WebSocket 403 掉）。
- ``websocket=1``：``/api/remote.mux`` 是 WebSocket。
- **写后必须回读**：``ensure_site`` 只有在 ``getLocationByServerId`` 里看到指向
  正确 upstream 的 ``path=/`` location 才算成功；看不到抛 ``ng_verify_failed``，
  错误里写明已建了什么、缺什么——绝不产生「reload 完成」式谎报。

幂等/续建：站点按 ``server_name`` 精确查；server/upstream/location 三段各自
「已一致就不发写请求」，半成品状态（站点在、location 缺）重跑只补 location
一个写请求。``find_site`` 会展开读 location，``location_id=None`` 即半成品。

单字段精修 ``patch_location``：面板写接口收的是整条实体，"只发改动字段"的半量
请求会把 location 上的自定义参数（descr/rootPath/…）按面板默认值洗掉——所以
先 ``getLocationByServerId`` 读回整条原样回填、只盖 changes 点名的字段，写后
回读逐字段核对。非本控制台前缀的站点默认拒绝（ng_foreign_site_refused），
用户自己名下老站点须显式 ``allow_foreign=True`` 才放行，且写请求带 foreign
标记进 recorder/审计。

``drift_scan``（纯只读）：replace 是全面板按库重建，库盘不一致会被打回坏值；
这版 nginxWebUI 没有读 conf 文件内容的 API（见 docs/05），磁盘侧只能黑盒旁证
（``https://<站点>/api/healthz`` 带 Origin）。**状态码仅在后端可证明是 DSH 时
才是证据**（known_dsh 登记名单或家族 §4.1 响应指纹），证明不了一律
inconclusive——非 DSH 后端（jellyfin/Gogs/onlyOffice…）的 200/401/403/502
都不能拿来判 Host。判定分 consistent / db_dirty / disk_dirty / inconclusive /
unreachable / no_location（+ all_sites=True 时 foreign_report 只报库值）。
真一致性凭据是运维只读比对 conf.d 与库值；replace 会**删掉库里没有的 conf
文件**（§5.4 红线）。

安全边界：ensure/delete 只认第一标签以 ``dsh-`` / ``dshctl`` 开头的 FQDN
（实例域名规范 ``dsh-<标签>.<zone>`` 与主控台自己的 ``dshctl``），删除必须逐字
对上 ``expect_name``，否则 NotManaged。这台面板上跑着别人的一堆站点，
一条都不许碰。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any

import httpx

from .base import IntegrationError, LineSink, NgSite, NotManaged
from .dry_run import DryRunRecorder

DEFAULT_SITE_PREFIXES = ("dsh-", "dshctl")
DEFAULT_UPSTREAM_PREFIXES = ("DSH_", "DSHCTL_", "dsh_", "dshctl_")

# nginxWebUI 容器里 acme.sh 证书的固定目录（面板自身布局，非站点值）；
# 证书名由调用方传入，如 "*.example.com"。4.2.5 真机记录已证实此模板。
_ACME_DIR = "/home/nginxWebUI/.acme.sh"

# location 里 Host 头绝对禁止出现的写法（§5.4 的坑）
_FORBIDDEN_HOST_PATTERNS = ("$proxy_port", "$server_port", "$host:")

#: insertOrUpdateLocation 的参数全集。来源：部署版 smart-doc
#: （``/api/server/insertOrUpdateLocation`` 的 curl 示例）逐项照抄。
#: 白名单外的字段本地拒绝——不把没把握的参数发给生产面板。
LOCATION_PARAM_FIELDS = frozenset({
    "serverId", "path", "type", "value", "upstreamType", "upstreamId",
    "upstreamPath", "rootPath", "rootPage", "rootType", "header",
    "headerHost", "websocket", "cros", "returnUrl", "returnPath", "descr", "id",
})


def _guard_header_host(header_host: str) -> None:
    compact = header_host.replace(" ", "")
    for bad in _FORBIDDEN_HOST_PATTERNS:
        if bad in compact:
            raise IntegrationError(
                f"headerHost={header_host!r} 会把端口写进上游 Host，"
                "DSH 会因 Origin/Host 不相等 403 掉 WebSocket；只允许 $host 这类不带端口的写法",
                code="header_host_port",
            )


def _first_label(server_name: str) -> str:
    return server_name.split(".", 1)[0].lower()


def _norm(value: object) -> str:
    """回读比对归一：面板回包 int/str/bool 混用（websocket 是 int，id 是 str）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


#: 家族 §4.1 的 401 机器码（ARCHITECTURE.md 错误清单）。响应里出现它们 +
#: ``{"ok": false, ...}`` 信封 = 回话的是 DSH 系后端，状态码才能当 Host 证据。
_DSH_ERROR_CODES = frozenset({
    "bad_key", "unauthenticated", "missing_x_requested_with", "rate_limited",
})


def _looks_like_dsh(resp: httpx.Response) -> bool:
    """DSH 响应指纹：只认自家信封，别的一律不认（宁 inconclusive 不误判）。

    - 200 且体为 ``{"ok": true}``（/api/healthz 的固定形状）；
    - 401 且体为 ``{"ok": false, "error": <机器码>}``（准入墙拦在没登录）。
    DSH 脏 Host 的 403 是裸文本（现网实测 9 字节 forbidden），**不能**靠体证明
    ——那条通道是编排层传 known_dsh（库里登记过的实例域名）。
    """
    try:
        body = resp.json()
    except ValueError:
        return False
    if not isinstance(body, dict) or "ok" not in body:
        return False
    if resp.status_code == 200 and body.get("ok") is True:
        return True
    return (resp.status_code == 401 and body.get("ok") is False
            and body.get("error") in _DSH_ERROR_CODES)


class NgPanel:
    """实现 base.NgProvider。"""

    manual_required = False  # 与 noop provider 对齐：jobs 层据此判 skipped

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        dry_run: bool = False,
        recorder: DryRunRecorder | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        site_prefixes: tuple[str, ...] = DEFAULT_SITE_PREFIXES,
        upstream_prefixes: tuple[str, ...] = DEFAULT_UPSTREAM_PREFIXES,
        line_sink: LineSink | None = None,
        retry_delays: tuple[float, ...] = (4.0, 8.0),
    ) -> None:
        if not base_url:
            raise IntegrationError("nginxWebUI base_url 未配置", code="no_config")
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self.dry_run = dry_run
        self.recorder = recorder
        self.line_sink = line_sink    # 编排层接前端点对点日志用（可选，没接走 recorder）
        self.retry_delays = retry_delays   # 有界重试的递增间隔（最多 len+1 次尝试）
        self.site_prefixes = site_prefixes
        self.upstream_prefixes = upstream_prefixes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._token = ""
        self._token_exp = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- 鉴权

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        # 注意：这条 URL 带明文口令，永远不进日志、不进异常信息。
        resp = await self._client.get(
            "/token/getToken", params={"name": self._username, "pass": self._password},
        )
        data = self._json(resp, "/token/getToken")
        if not data.get("success"):
            raise IntegrationError("nginxWebUI 取 token 失败（账号/口令？）",
                                   code=str(data.get("status") or "401"))
        obj = data.get("obj") or {}
        self._token = str(obj.get("token") or "")
        timeout_ms = obj.get("tokenTimeout")
        self._token_exp = (float(timeout_ms) / 1000.0) if timeout_ms else time.time() + 3600
        if not self._token:
            raise IntegrationError("nginxWebUI token 响应为空", code="empty_token")
        return self._token

    @staticmethod
    def _json(resp: httpx.Response, path: str) -> dict[str, Any]:
        try:
            return resp.json()
        except ValueError as exc:
            raise IntegrationError(
                f"nginxWebUI {path} HTTP {resp.status_code} 且返回非 JSON", code="bad_json",
            ) from exc

    async def _api(
        self, path: str, params: dict[str, Any] | None = None, *,
        write: bool, action: str,
    ) -> Any:
        if write and self.dry_run:
            if self.recorder is not None:
                self.recorder.record(
                    "ng", action, method="GET", target=self.base_url + path,
                    payload=dict(params or {}), note="dry-run: 未发送",
                )
            return {}
        token = await self._ensure_token()
        resp = await self._client.get(path, params=params or {},
                                      headers={"token": token})
        data = self._json(resp, path)
        if not data.get("success") and str(data.get("status")) == "401":
            self._token = ""  # 过期重取，重试一次
            token = await self._ensure_token()
            resp = await self._client.get(path, params=params or {},
                                          headers={"token": token})
            data = self._json(resp, path)
        if not data.get("success"):
            raise IntegrationError(
                f"nginxWebUI {path}: {data.get('msg') or '失败'}",
                code=str(data.get("status") or "500"),
            )
        return data.get("obj")

    # ---------------------------------------------------------------- 白名单

    def _guard_site_name(self, server_name: str, action: str) -> None:
        if not _first_label(server_name).startswith(self.site_prefixes):
            raise NotManaged(
                f"拒绝{action}站点 {server_name!r}：第一标签必须以 "
                f"{'/'.join(self.site_prefixes)} 开头（本控制台只操作自己创建的站点）",
                code="not_managed",
            )

    def _guard_upstream_name(self, name: str, action: str) -> None:
        if not name.startswith(self.upstream_prefixes):
            raise NotManaged(
                f"拒绝{action} upstream {name!r}：前缀必须是 DSH_/DSHCTL_",
                code="not_managed",
            )

    # ---------------------------------------------------- 报文构造（唯一出口）

    def _build_payload(self, kind: str, **kw: Any) -> dict[str, Any]:
        """所有写接口请求体的唯一构造点。真机首发若被服务端挑字段，只改这里。

        —— 待真机核对清单见回报；读接口（getPage/getLocationByServerId 等）已实测。
        """
        if kind == "upstream":
            return {
                "name": kw["name"],
                "proxyType": 0,      # http
                "tactics": "",       # 无负载策略
                "monitor": 0,
                "descr": kw.get("descr", "dsh-control"),
                **({"id": kw["id"]} if kw.get("id") else {}),
            }
        if kind == "upstream_server":
            payload = {
                "upstreamId": kw["upstream_id"],
                "server": kw["server"],
                "port": kw["port"],
                "status": "none",
                "param": "",
            }
            # 带 id 就是原地改这条后端。地址变了必须改而不是再加一条：
            # 两条后端是轮询关系，留着旧地址就等于一半请求打到死地址上。
            if kw.get("id") not in (None, "", 0):
                payload["id"] = kw["id"]
            return payload
        if kind == "server":
            cert_name = kw.get("cert_name")
            payload: dict[str, Any] = {
                "serverName": kw["server_name"],
                "listen": str(kw["listen_port"]),
                "def": 0, "ipv6": 0, "proxyProtocol": 0, "proxyType": 0,
                "enable": "true", "denyAllow": 0,
                "ssl": 1 if cert_name else 0,
                **({"rewrite": 1, "rewriteListen": "80", "http2": 2} if cert_name else {}),
                "descr": kw.get("descr", "dsh-control"),
                **({"id": kw["id"]} if kw.get("id") else {}),
            }
            if cert_name:
                payload["pem"] = f"{_ACME_DIR}/{cert_name}/fullchain.cer"
                payload["key"] = f"{_ACME_DIR}/{cert_name}/{cert_name}.key"
            return payload
        if kind == "location":
            payload_loc: dict[str, Any] = {
                "serverId": kw["server_id"],
                "path": kw.get("path", "/"),
                "type": 2,            # 负载均衡反代：ConfService 该分支生成
                                      # proxy_pass <upstreamType>://<upstream名>
                "value": "",          # type=0 直连才用；显式给空，别留面板默认
                "upstreamType": kw.get("upstream_type", "http"),
                "upstreamId": kw["upstream_id"],
                "upstreamPath": "",
                "rootPath": "",
                "rootPage": "",
                "rootType": 0,
                "header": 1,
                "headerHost": kw["header_host"],  # 恒为 $host，见 _guard_header_host
                "websocket": 1 if kw.get("websocket", True) else 0,
                "cros": 0,
                "returnUrl": "",
                "returnPath": 0,      # 真机首发时被面板落成 1，显式压回 0
                "descr": kw.get("descr", "dsh-control"),
                **({"id": kw["id"]} if kw.get("id") else {}),
            }
            unknown = sorted(set(payload_loc) - LOCATION_PARAM_FIELDS)
            if unknown:
                raise IntegrationError(
                    f"location 参数含白名单外字段 {unknown}，拒绝发送",
                    code="location_fields_not_allowed",
                )
            return payload_loc
        raise IntegrationError(f"未知报文类型 {kind}", code="bad_payload_kind")

    @staticmethod
    def _server_matches(rec: dict[str, Any], *, listen_port: int,
                        cert_name: str | None) -> bool:
        """站点关键参数与期望一致才允许跳过写（证书路径按面板 4.2.5 模板比对）."""
        if str(rec.get("listen")) != str(listen_port):
            return False
        want_ssl = 1 if cert_name else 0
        if int(rec.get("ssl") or 0) != want_ssl:
            return False
        if cert_name:
            if rec.get("pem") != f"{_ACME_DIR}/{cert_name}/fullchain.cer":
                return False
            if rec.get("key") != f"{_ACME_DIR}/{cert_name}/{cert_name}.key":
                return False
        return True

    @staticmethod
    def _location_matches(loc: dict[str, Any], *, upstream_id: int,
                          header_host: str, websocket: bool) -> bool:
        return (str(loc.get("upstreamId") or "") == str(upstream_id)
                and str(loc.get("headerHost") or "") == header_host
                and str(loc.get("websocket") or "") == ("1" if websocket else "0"))

    # ---------------------------------------------------------------- 读

    async def _get_page(self, path: str, keywords: str = "") -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        current = 1
        while current <= 10:
            obj = await self._api(
                path, {"current": current, "limit": 100, "keywords": keywords},
                write=False, action="getPage",
            )
            page = (obj or {}).get("records") or []
            records.extend(page)
            if len(records) >= int((obj or {}).get("count") or 0) or not page:
                break
            current += 1
        return records

    async def _server_rec(self, server_name: str) -> dict[str, Any] | None:
        for rec in await self._get_page("/api/server/getPage", server_name):
            if rec.get("serverName") == server_name:
                return rec
        return None

    async def find_site(self, server_name: str) -> NgSite | None:
        rec = await self._server_rec(server_name)
        if rec is None:
            return None
        sid = int(rec["id"])
        up = rec.get("proxyUpstreamId")
        site = NgSite(
            server_id=sid, server_name=server_name,
            upstream_id=int(up) if up else None,
            location_id=None,
        )
        # 真正的指向在 location 上：展开读回来。location_id=None 即
        # 「站点在、反代没建成」的半成品——编排层据此必须走补建，不许当已存在。
        for loc in await self._locations(sid):
            if loc.get("path") == "/":
                site.location_id = int(loc["id"]) if loc.get("id") else None
                lu = loc.get("upstreamId")
                if lu and not site.upstream_id:
                    site.upstream_id = int(lu)
                site.header_host = str(loc.get("headerHost") or "$host")
                site.websocket = str(loc.get("websocket") or "") == "1"
                break
        return site

    async def _find_upstream(self, name: str) -> dict[str, Any] | None:
        for rec in await self._get_page("/api/upstream/getPage", name):
            if rec.get("name") == name:
                return rec
        return None

    async def _locations(self, server_id: int) -> list[dict[str, Any]]:
        return list(await self._api("/api/server/getLocationByServerId",
                                    {"serverId": server_id}, write=False,
                                    action="locations") or [])

    # ---------------------------------------------------------------- 写

    async def _ensure_upstream(self, upstream_name: str, upstream_server: str) -> int:
        """返回 upstream id。dry_run 下返回 0（读不到新 id）。"""
        self._guard_upstream_name(upstream_name, "创建/修改")
        rec = await self._find_upstream(upstream_name)
        if rec is None:
            await self._api("/api/upstream/insertOrUpdate",
                            self._build_payload("upstream", name=upstream_name),
                            write=True, action="upstream.insertOrUpdate")
            if self.dry_run:
                return 0
            rec = await self._find_upstream(upstream_name)
            if rec is None:
                raise IntegrationError(
                    f"upstream {upstream_name} 建后回读不到", code="ng_reread")
        upstream_id = int(rec["id"])

        host, sep, port = upstream_server.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise IntegrationError(
                f"upstream_server 需要 host:port 形式，收到 {upstream_server!r}",
                code="bad_upstream_server",
            )
        backends = await self._api("/api/upstream/getServerByUpstreamId",
                                   {"upstreamId": upstream_id}, write=False,
                                   action="upstreamServers") or []
        def _match(b: dict[str, Any]) -> bool:
            return str(b.get("server")) == host and int(b.get("port") or 0) == int(port)

        ours = [b for b in backends if _match(b)]
        others = [b for b in backends if not _match(b)]

        if ours and others:
            # 轮询语义：多挂一条死地址，就是一半请求随机打空。不猜，交回人处理。
            raise IntegrationError(
                f"upstream {upstream_name} 同时挂着 {len(ours)} 条 {upstream_server} 与 "
                f"{len(others)} 条其它后端 "
                f"{[(b.get('server'), b.get('port')) for b in others]}；"
                "轮询会把一半请求打到死地址，请先在面板里删掉多余后端再重跑",
                code="ng_backend_drift",
            )
        if not ours:
            if len(others) > 1:
                raise IntegrationError(
                    f"upstream {upstream_name} 下已有 {len(others)} 条非本控制台后端 "
                    f"{[(b.get('server'), b.get('port')) for b in others]}，不敢自动合并",
                    code="ng_backend_drift",
                )
            payload = self._build_payload("upstream_server", upstream_id=upstream_id,
                                          server=host, port=int(port))
            action = "upstream.insertOrUpdateServer"
            if len(others) == 1:
                # 只是地址换了（改了 frps 上游地址、端口重分配）：原地改这一条
                payload["id"] = others[0].get("id")
                action = "upstream.insertOrUpdateServer(改地址)"
            await self._api("/api/upstream/insertOrUpdateServer", payload,
                            write=True, action=action)
            if not self.dry_run:
                after = await self._api("/api/upstream/getServerByUpstreamId",
                                        {"upstreamId": upstream_id}, write=False,
                                        action="upstreamServers") or []
                if not any(_match(b) for b in after):
                    raise IntegrationError(
                        f"upstream {upstream_name} 的后端写成 {upstream_server} 后回读不到，"
                        f"面板现回 {[(b.get('server'), b.get('port')) for b in after]}",
                        code="ng_verify_failed",
                    )
        return upstream_id

    async def ensure_site(
        self,
        *,
        server_name: str,
        upstream_name: str,
        upstream_server: str,
        listen_port: int = 443,
        cert_name: str | None = None,
        header_host: str = "$host",
        websocket: bool = True,
    ) -> NgSite:
        _guard_header_host(header_host)
        self._guard_site_name(server_name, "创建/修改")

        upstream_id = await self._ensure_upstream(upstream_name, upstream_server)

        # ---- 站点：已一致就不写；不一致带原 id 更新；缺就新建 ----
        rec = await self._server_rec(server_name)
        server_ok = (rec is not None
                     and self._server_matches(rec, listen_port=listen_port,
                                              cert_name=cert_name))
        if not server_ok:
            await self._api("/api/server/insertOrUpdate", self._build_payload(
                "server", server_name=server_name, listen_port=listen_port,
                cert_name=cert_name, id=int(rec["id"]) if rec else None,
            ), write=True, action="server.insertOrUpdate")
        if self.dry_run:
            # dry-run：读面是真状态照常用；写只进 recorder，最后返回全合成 id
            server_id = int(rec["id"]) if rec else 0
        else:
            server_id = int(rec["id"]) if server_ok else None
            if server_id is None:
                rec = await self._server_rec(server_name)
                if rec is None:
                    raise IntegrationError(
                        f"NG 半成品：upstream {upstream_name}(id={upstream_id}) 已确认，"
                        f"但站点 {server_name} 保存后 getPage 回读不到——location 未写，"
                        f"重跑可续建", code="ng_reread")
                server_id = int(rec["id"])

        # ---- location：已一致不写；指向不对带 id 更新；缺就补建 ----
        locations = await self._locations(server_id)
        loc = next((l for l in locations if l.get("path") == "/"), None)
        if loc is None or not self._location_matches(
                loc, upstream_id=upstream_id, header_host=header_host,
                websocket=websocket):
            await self._api("/api/server/insertOrUpdateLocation", self._build_payload(
                "location", server_id=server_id, upstream_id=upstream_id,
                header_host=header_host, websocket=websocket,
                id=loc.get("id") if loc else None,
            ), write=True, action="server.insertOrUpdateLocation")
        if self.dry_run:
            # 写意图已全部送进 recorder；不回读、不物化，id 全合成
            return NgSite(server_id=0, server_name=server_name,
                          upstream_id=upstream_id, location_id=0,
                          header_host=header_host, websocket=websocket)

        # ---- 写后回读：getLocationByServerId 看不到就不算建成 ----
        locations = await self._locations(server_id)
        back = next((l for l in locations
                     if l.get("path") == "/" and self._location_matches(
                         l, upstream_id=upstream_id, header_host=header_host,
                         websocket=websocket)), None)
        if back is None:
            saw = "; ".join(
                f"path={l.get('path')!r}->upstream {l.get('upstreamId')!r}"
                for l in locations) or "一条都没有"
            raise IntegrationError(
                f"NG 半成品：upstream {upstream_name}(id={upstream_id})、"
                f"站点 {server_name}(server_id={server_id}) 都已确认，"
                f"但 location 写后回读不到指向 upstream {upstream_id} 的 path=/ "
                f"（面板现回 {saw}）。缺 location，重跑可续建；"
                f"conf 未物化前请勿 reload",
                code="ng_verify_failed")
        return NgSite(
            server_id=server_id, server_name=server_name,
            upstream_id=upstream_id,
            location_id=int(back["id"]) if back.get("id") else None,
            header_host=header_host, websocket=websocket,
        )

    async def delete_site(self, server_id: int, *, expect_name: str) -> None:
        self._guard_site_name(expect_name, "删除")
        site = None
        for rec in await self._get_page("/api/server/getPage", expect_name):
            if int(rec["id"]) == server_id:
                site = rec
                break
        if site is None or site.get("serverName") != expect_name:
            raise NotManaged(
                f"删除被拦：server_id={server_id} 查不到 / 名字不是 {expect_name!r}"
                "（拒绝按 id 盲删）",
                code="not_managed",
            )
        self._guard_site_name(str(site.get("serverName")), "删除")
        await self._api("/api/server/delete", {"id": server_id},
                        write=True, action="server.delete")

    async def delete_upstream(self, upstream_id: int, *, expect_name: str) -> None:
        """不在 Protocol 里，但释放流程要把 upstream 一起收掉（站点先删、再删它）。"""
        self._guard_upstream_name(expect_name, "删除")
        rec = None
        for row in await self._get_page("/api/upstream/getPage", expect_name):
            if int(row["id"]) == upstream_id:
                rec = row
                break
        if rec is None or rec.get("name") != expect_name:
            raise NotManaged(
                f"删除被拦：upstream_id={upstream_id} 查不到 / 名字不是 {expect_name!r}",
                code="not_managed",
            )
        await self._api("/api/upstream/delete", {"id": upstream_id},
                        write=True, action="upstream.delete")

    # ------------------------------------------------- 单字段精修（全量回填）

    async def patch_location(
        self,
        server_name: str,
        *,
        path: str = "/",
        changes: dict[str, Any] | None = None,
        allow_foreign: bool = False,
    ) -> dict[str, Any]:
        """改某条 location 的个别字段——**全量回填，只覆盖点名字段**。

        为什么必须全量：面板的 insertOrUpdateLocation 收的是整条实体，"只发
        改动字段"的半量 payload 会把它没带的字段按面板默认值处理——等于把别人
        location 上的自定义参数（descr/rootPath/returnUrl……）洗掉。所以这里
        先把 getLocationByServerId 读回的整条记录原样回填，再盖 changes。

        守卫（一个都不松）：
        - changes 越出 ``LOCATION_PARAM_FIELDS`` 白名单 → 本地拒写，零请求；
          ``id``/``serverId`` 只读（身份字段不许借道改）。
        - 合并后的 headerHost 仍过 ``_guard_header_host``：库里本来就带端口的
          脏记录，必须同一次把 headerHost 修成干净值才放行（拒写消息会说明）。
        - 站点前缀不是本控制台的：``allow_foreign=False`` 直接拒
          （ng_foreign_site_refused）；``True`` 才放行——这是用户自己名下老站点
          的唯一通道，写请求带 foreign 标记进 recorder/审计，且仅在显式传参时
          可达，默认永远走不到。
        - 写后回读：changes 每个字段逐一对得上才算成功，否则 ng_verify_failed
          并列出期望值与现值。dry-run 一个写请求都不发（读照常）。

        返回：回读到的整条 location 记录（dry-run 返回将要发送的 payload）。
        """
        changes = dict(changes or {})
        own = _first_label(server_name).startswith(self.site_prefixes)
        if not own and not allow_foreign:
            raise NotManaged(
                f"拒绝对站点 {server_name!r} 的 location 做修改：第一标签不是本控制台"
                f"前缀（{'/'.join(self.site_prefixes)}）。确属用户自己名下的老站点，"
                "才允许显式传 allow_foreign=True 走 foreign 通道",
                code="ng_foreign_site_refused",
            )
        if not changes:
            raise IntegrationError("patch_location 需要至少一个改动字段",
                                   code="empty_changes")
        unknown = sorted(set(changes) - LOCATION_PARAM_FIELDS)
        if unknown:
            raise IntegrationError(
                f"location 参数含白名单外字段 {unknown}，拒绝发送",
                code="location_fields_not_allowed",
            )
        borrowed = sorted({"id", "serverId"} & set(changes))
        if borrowed:
            raise IntegrationError(
                f"{borrowed} 是身份字段，patch 不许改（换 location 请删建，换站请走建站）",
                code="location_field_readonly",
            )
        if "headerHost" in changes:
            # 点名的就是 headerHost：本地当场拒脏值，连读都不发（零请求）
            _guard_header_host(str(changes["headerHost"]))

        rec = await self._server_rec(server_name)
        if rec is None:
            raise IntegrationError(f"站点 {server_name!r} 在面板里不存在",
                                   code="ng_site_missing")
        server_id = int(rec["id"])
        locations = await self._locations(server_id)
        loc = next((l for l in locations if l.get("path") == path), None)
        if loc is None:
            saw = "; ".join(repr(l.get("path")) for l in locations) or "一条都没有"
            raise IntegrationError(
                f"站点 {server_name}(server_id={server_id}) 里没有 path={path!r} 的 "
                f"location（面板现回 {saw}），无从回填整条记录，拒写",
                code="ng_location_missing",
            )

        # 全量回填：读回什么带什么（None/缺字段不发——面板 updateById 对缺省字段
        # 保持原值，反而显式空串会把人家自定义的东西洗掉）
        payload: dict[str, Any] = {
            key: loc[key] for key in LOCATION_PARAM_FIELDS
            if key not in ("id", "serverId")
            and key in loc and loc[key] is not None
        }
        payload["serverId"] = server_id
        payload["id"] = loc["id"]
        payload.update(changes)
        _guard_header_host(str(payload.get("headerHost", "$host")))

        action = ("server.insertOrUpdateLocation" if own
                  else "server.insertOrUpdateLocation#foreign")
        await self._api("/api/server/insertOrUpdateLocation", payload,
                        write=True, action=action)
        if not own and not self.dry_run and self.recorder is not None:
            # foreign 通道的真实写也要留审计痕（dry-run 时 _api 已记过）
            self.recorder.record(
                "ng", action, method="GET",
                target=self.base_url + "/api/server/insertOrUpdateLocation",
                payload=dict(payload), note="allow_foreign 通道：非本控制台前缀站点")
        if self.dry_run:
            return payload

        verify_path = str(changes.get("path", path))
        back = next((l for l in await self._locations(server_id)
                     if l.get("path") == verify_path), None)
        if back is None:
            raise IntegrationError(
                f"location 写后回读不到 path={verify_path!r}"
                f"（站点 {server_name} server_id={server_id}，id={loc.get('id')}）",
                code="ng_verify_failed")
        diffs = [
            f"{key}: 期望 {want!r} 面板现值 {back.get(key)!r}"
            for key, want in changes.items()
            if _norm(back.get(key)) != _norm(want)
        ]
        if diffs:
            raise IntegrationError(
                f"location 写后回读不一致（站点 {server_name} id={loc.get('id')}）："
                + "；".join(diffs),
                code="ng_verify_failed")
        return dict(back)

    # ------------------------------------------------ 库盘一致性黑盒体检（只读）

    async def drift_scan(
        self,
        *,
        probe_client: httpx.AsyncClient | None = None,
        timeout: float = 6.0,
        only_managed: bool = True,
        all_sites: bool = False,
        known_dsh: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """replace 之前的体检：**库里的 header_host vs 磁盘的真实生效值**。

        能力边界（先读这段，别拿它当一致性证明）：
        - 这一版 nginxWebUI **没有任何读配置文件内容的 API**（getFileContent /
          getFile / fileContent / confContent 全部 404），磁盘真相只有运维侧
          只读比对 conf.d 一条路（docs/05）。这里做的是黑盒旁证。
        - HTTP 状态码**只有能证明后端是 DSH 时才是证据**（DSH 准入墙实测：
          Host 干净→401/200，Host 带端口→403）。jellyfin/Gogs 这类后端带端口
          的 Host 照样回 200/401/502；onlyOffice 自家就回 403——拿它们的状态码
          判 Host 是错的。证明通道只有两条：
          ① ``known_dsh``：编排层从自己的实例表里传进来的已登记 DSH 实例域名
             （前缀像我们的站**不算**证明，前缀只是改删守卫）；
          ② 响应指纹：家族 §4.1 信封——200 且体是 ``{"ok": true}``（healthz），
             或 401 且体是 ``{"ok": false, "error": <机器码>}``。
          证明不了的一律 ``inconclusive``：既不判干净也不判脏，不进危险列表。

        判定（逐站点一条，供编排层打表；``dsh_proven`` 标明这条是不是证据）：
        - ``db_dirty``：仅已确认为 DSH + 库里 headerHost 带端口 + 探到 401/200
          （**盘净**）——replace 会把好盘洗坏，危险，**必须先 patch_location
          改库**再 replace；
        - ``dirty_both``：仅已确认为 DSH + 库脏 + 探到 403（**盘同脏**）——库盘
          一致，replace 是中性的、不会改变现状；但该站点当前公网被准入墙拦住，
          同样是危险，只是不急：改库或改盘（走面板）都能解；
        - ``disk_dirty``：同样仅在已证明 DSH 时——库净而盘脏（403），人手改过
          文件，replace 能纠正回干净；
        - ``consistent``：已证明 DSH，库净且盘净（401/200）；
        - ``inconclusive``：后端证明不了是 DSH（含 403 但无 DSH 特征、任何状态
          码的非 DSH 形状）——不是证据，不是危险；
        - ``unreachable``：连不上/超时/DNS 没生效——不下结论；
        - ``no_location``：站点没有可判定的 location（半成品/纯静态）；
        - ``foreign_report``：``all_sites=True`` 时列出的他人站点，**只报库值、
          不发探测、不给判定**。

        危险列表 = ``db_dirty`` / ``dirty_both`` / ``disk_dirty`` 三值，且都以
        ``dsh_proven=True`` 为前提；``dsh_proven=False`` 一律 ``inconclusive``。

        范围：默认 ``only_managed=True`` 只体检我们前缀内（或 ``known_dsh`` 登记过）
        的站点；他人站点仅当显式 ``all_sites=True``（或 ``only_managed=False``，
        等价）才列出来——``foreign_report`` 只报库值、不发探测、不给判定。
        enable=0 的站点面板不发布 conf，跳过。全程只读：一个写请求都不发。
        """
        known = {str(n).lower() for n in known_dsh}
        list_foreign = all_sites or not only_managed
        sites = await self._get_page("/api/server/getPage")
        owns_probe = probe_client is None
        client = probe_client or httpx.AsyncClient(timeout=timeout)
        results: list[dict[str, Any]] = []
        try:
            for rec in sites:
                name = str(rec.get("serverName") or "")
                if not name or str(rec.get("enable")).lower() not in ("true", "1"):
                    continue
                managed = (_first_label(name).startswith(self.site_prefixes)
                           or name.lower() in known)
                if not managed:
                    if not list_foreign:
                        continue
                    server_id = int(rec["id"])
                    locations = await self._locations(server_id)
                    loc = next((l for l in locations if l.get("path") == "/"),
                               None) or (locations[0] if locations else None)
                    results.append({
                        "server_name": name,
                        "header_host": str((loc or {}).get("headerHost") or ""),
                        "probe_status": None, "dsh_proven": False,
                        "verdict": "foreign_report",   # 只报库值，不判
                    })
                    continue
                server_id = int(rec["id"])
                locations = await self._locations(server_id)
                loc = next((l for l in locations if l.get("path") == "/"),
                           None) or (locations[0] if locations else None)
                db_host = str((loc or {}).get("headerHost") or "")
                row: dict[str, Any] = {"server_name": name, "header_host": db_host,
                                       "probe_status": None, "dsh_proven": False}
                if loc is None or not db_host:
                    row["verdict"] = "no_location"
                    results.append(row)
                    continue
                try:
                    resp = await client.get(
                        f"https://{name}/api/healthz",
                        headers={"Origin": f"https://{name}"})
                    row["probe_status"] = resp.status_code
                except (httpx.HTTPError, OSError):
                    row["verdict"] = "unreachable"   # 连不上≠脏≠干净
                    results.append(row)
                    continue
                proven = name.lower() in known or _looks_like_dsh(resp)
                row["dsh_proven"] = proven
                code = resp.status_code
                if not proven or code not in (200, 401, 403):
                    row["verdict"] = "inconclusive"   # 不是证据就别当证据
                elif any(pat in db_host for pat in _FORBIDDEN_HOST_PATTERNS):
                    # 两种病必须分开：盘净时 replace 会把好的洗坏（急，先改库）；
                    # 盘同脏时库盘一致，replace 中性，但站点此刻正被准入墙拦着。
                    row["verdict"] = "db_dirty" if code in (200, 401) \
                        else "dirty_both"
                elif code == 403:
                    row["verdict"] = "disk_dirty"     # 库净盘脏：手改的文件，replace 能救
                else:
                    row["verdict"] = "consistent"     # 库净盘净（401/200）
                results.append(row)
        finally:
            if owns_probe:
                await client.aclose()
        return results

    # ---------------------------------------------------- 有界重试（面板会忙）

    def _progress(self, line: str) -> None:
        """面向用户的一行中文：优先走编排层接的 line_sink（前端点对点日志），
        没接就落 recorder（on_record 也通向日志）；两个都没有就沉默。"""
        if self.line_sink is not None:
            self.line_sink("info", line)
        elif self.recorder is not None:
            self.recorder.record("ng", "progress", method="", target=self.base_url,
                                 payload={}, note=line)

    async def _retry_step(self, path: str, *, action: str, verb: str,
                          tail: str) -> Any:
        """check / reload 这类可重复步骤的有界重试（默认 3 次尝试，间隔 4s、8s）。

        实测背景：用户同时在面板点「重载配置」时，面板的 replace 会撞上我们
        正在跑的 nginx -t，check 只回四个字「效验失败」——面板忙是正常工况，
        不该把一次正常开通判死。只重试 5xx（忙/临时故障）；401/4xx 是确定性的
        配置或权限问题，重试没有意义，原样立抛。
        """
        delays = self.retry_delays
        attempts = len(delays) + 1
        last: IntegrationError | None = None
        for i in range(attempts):
            try:
                return await self._api(path, write=True, action=action)
            except IntegrationError as exc:
                if exc.code not in ("500", "502", "503", "504"):
                    raise
                last = exc
                if i < attempts - 1:
                    delay = delays[i]
                    self._progress(
                        f"nginxWebUI {verb}未通过（面板可能正被其他操作占用），"
                        f"{delay:g} 秒后重试 {i + 2}/{attempts}：{exc}")
                    await asyncio.sleep(delay)
        gaps = "、".join(f"{d:g}s" for d in delays)
        raise IntegrationError(
            f"nginxWebUI {path} 连续 {attempts} 次失败（间隔 {gaps}）："
            f"面板原文「{last}」。nginxWebUI 可能正被其他操作占用或库与磁盘存在"
            f"漂移；{tail}",
            code="ng_busy_or_drift",
        ) from last

    async def reload(self) -> None:
        """把库里的配置推到 nginx。顺序是 2026-10-21 现网实测纠正过的：

        0. ``check``（**预检只提醒、不拦**）：校验的是**磁盘当前**配置。它过不了
           通常意味着库里已有还没落盘的对象（第 1 步会补上），也可能是有人面板外
           手改坏了 conf。这里只记一行警告——拿它当闸门会把所有开通永久卡死。
        1. ``replace``：conf.d 整目录按库重建（面板 UI「覆盖配置」同义，旧配置进
           面板操作日志与备份表；面板外手改过的 conf.d 会被重建掉）——**绝不重试**。
        2. ``check``（**硬闸**）：这时磁盘文件齐了，校验才有意义。不过就**不 reload**：
           nginx 还在跑内存里的旧配置，用户正在服务的东西一根汗毛都没动。
        3. ``reload``：``nginx -s reload``，幂等，允许有界重试。

        为什么 ``check`` 不能排在 ``replace`` 前面当闸门：面板 check 把库拼进临时主
        配置，而主配置里的 ``include conf.d/upstreams.<name>.conf`` 指的是**磁盘**文件；
        新建对象只有 replace 之后才有文件。所以"库里多一条还没落盘的对象"必然让 check
        失败——当晚用「只建一条 upstream → check 失败 → 删掉 → check 成功」可逆实验
        坐实，此前两次真实开通都卡在这上面（当时的报错把它误读成"面板正被占用"）。

        真要把配置退回去：面板每次操作都把配置快照进自己的备份表，UI「备份恢复」
        一键回到操作前，见 docs/05-故障排查.md。
        """
        try:
            # dry-run 不发预检：它不写库也不落盘，跑一次 nginx -t 只是白占面板。
            if not self.dry_run:
                await self._api("/api/nginx/check", write=False, action="nginx.check")
        except IntegrationError as exc:
            self._progress(f"[体检警告] replace 前磁盘配置校验未过：{exc}。"
                           "常见原因是库里已有尚未落盘的对象（下一步 replace 会补上），"
                           "也可能是面板外手改过 conf；继续按库重建")
        await self._api("/api/nginx/replace", write=True, action="nginx.replace")
        await self._retry_step(
            "/api/nginx/check", action="nginx.check", verb="校验",
            tail="conf 已按库重建落盘但校验不过，本次**没有 reload**：nginx 仍跑旧配置，"
                 "要回退用面板的「备份恢复」")
        await self._retry_step(
            "/api/nginx/reload", action="nginx.reload", verb="重载",
            tail="conf 已按库重建落盘且校验通过，但 nginx -s reload 未成功，新配置尚未生效")
