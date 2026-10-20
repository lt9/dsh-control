"""nginxWebUI 适配器（ARCHITECTURE §5.4 + docs 里的 NG 接口面）。

鉴权：``GET /token/getToken?name=&pass=``（明文口令，**不在 /api/ 下**）换
``obj.token``，之后所有 ``/api/*`` 带 ``token`` 请求头；401 自动重取一次。
面板地址与账号全部来自构造参数。

必须写对的点（都已在代码里强制）：
- 反代必须走 location（``insertOrUpdateLocation``，``type=2`` 指 upstream），
  服务器级反代生成的 conf 固定是 ``Host: $host:$proxy_port``，DSH 的 Origin/Host
  准入会把它 403 掉（表现为"页面能开、remote.mux 一直重连"）。
- location 的 ``headerHost`` 固定 ``$host``：任何含 ``$proxy_port`` /
  ``$server_port`` / ``$host:`` 的值都在 ``_guard_header_host`` 处直接拒写。
- ``websocket=1``：``/api/remote.mux`` 是 WebSocket。

幂等：站点按 ``server_name`` 精确查（getPage 是模糊查，查回后再逐字比对），
存在则带原 id 更新；upstream 同理。

写接口的请求体字段全部集中在 ``_build_payload()`` 一处构造——厂商 smart-doc 说
这些接口是 GET+query 形式，个别可选字段（证书路径模板、location 附加参数）以
真机首发核对为准（回报里列了清单）。

安全边界：ensure/delete 只认第一标签以 ``dsh-`` / ``dshctl`` 开头的 FQDN
（实例域名规范 ``dsh-<标签>.<zone>`` 与主控台自己的 ``dshctl``），删除必须逐字
对上 ``expect_name``，否则 NotManaged。这台面板上跑着别人的一堆站点，
一条都不许碰。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import IntegrationError, NgSite, NotManaged
from .dry_run import DryRunRecorder

DEFAULT_SITE_PREFIXES = ("dsh-", "dshctl")
DEFAULT_UPSTREAM_PREFIXES = ("DSH_", "DSHCTL_", "dsh_", "dshctl_")

# nginxWebUI 容器里 acme.sh 证书的固定目录（面板自身布局，非站点值）；
# 证书名由调用方传入，如 "*.example.com"。 —— 待真机核对
_ACME_DIR = "/home/nginxWebUI/.acme.sh"

# location 里 Host 头绝对禁止出现的写法（§5.4 的坑）
_FORBIDDEN_HOST_PATTERNS = ("$proxy_port", "$server_port", "$host:")


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
    ) -> None:
        if not base_url:
            raise IntegrationError("nginxWebUI base_url 未配置", code="no_config")
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self.dry_run = dry_run
        self.recorder = recorder
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
            return {
                "upstreamId": kw["upstream_id"],
                "server": kw["server"],
                "port": kw["port"],
                "status": "none",
                "param": "",
            }
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
            return {
                "serverId": kw["server_id"],
                "path": kw.get("path", "/"),
                "type": 2,                      # 2 = 负载均衡（反代到 upstream）
                "upstreamId": kw["upstream_id"],
                "upstreamType": kw.get("upstream_type", "http"),
                "upstreamPath": "",
                "header": 1,
                "headerHost": kw["header_host"],  # 恒为 $host，见 _guard_header_host
                "websocket": 1 if kw.get("websocket", True) else 0,
                "cros": 0,
                "descr": kw.get("descr", "dsh-control"),
                **({"id": kw["id"]} if kw.get("id") else {}),
            }
        raise IntegrationError(f"未知报文类型 {kind}", code="bad_payload_kind")

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

    async def find_site(self, server_name: str) -> NgSite | None:
        for rec in await self._get_page("/api/server/getPage", server_name):
            if rec.get("serverName") == server_name:
                up = rec.get("proxyUpstreamId")
                return NgSite(
                    server_id=int(rec["id"]),
                    server_name=server_name,
                    upstream_id=int(up) if up else None,
                    # 真正的指向在 location 上，这里不展开读，省一次请求
                    location_id=None,
                )
        return None

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
        exists = any(str(b.get("server")) == host and int(b.get("port") or 0) == int(port)
                     for b in backends)
        if not exists:
            await self._api("/api/upstream/insertOrUpdateServer",
                            self._build_payload("upstream_server",
                                                upstream_id=upstream_id,
                                                server=host, port=int(port)),
                            write=True, action="upstream.insertOrUpdateServer")
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

        site = await self.find_site(server_name)
        server_payload = self._build_payload(
            "server", server_name=server_name, listen_port=listen_port,
            cert_name=cert_name, id=site.server_id if site else None,
        )
        await self._api("/api/server/insertOrUpdate", server_payload,
                        write=True, action="server.insertOrUpdate")
        if not self.dry_run:
            site = await self.find_site(server_name)
            if site is None:
                raise IntegrationError(
                    f"站点 {server_name} 保存后回读不到", code="ng_reread")
        server_id = site.server_id if site else 0

        locations = await self._locations(server_id) if (server_id and not self.dry_run) else []
        existing_loc = next((l for l in locations if l.get("path") == "/"), None)
        loc_payload = self._build_payload(
            "location",
            server_id=server_id,
            upstream_id=upstream_id or ((site.upstream_id if site else None) or 0),
            header_host=header_host,
            websocket=websocket,
            id=existing_loc.get("id") if existing_loc else None,
        )
        await self._api("/api/server/insertOrUpdateLocation", loc_payload,
                        write=True, action="server.insertOrUpdateLocation")
        if self.dry_run:
            return NgSite(server_id=0, server_name=server_name,
                          upstream_id=upstream_id, location_id=0,
                          header_host=header_host, websocket=websocket)
        locations = await self._locations(server_id)
        loc = next((l for l in locations if l.get("path") == "/"), None)
        return NgSite(
            server_id=server_id, server_name=server_name,
            upstream_id=upstream_id,
            location_id=int(loc["id"]) if loc and loc.get("id") else None,
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

    async def reload(self) -> None:
        await self._api("/api/nginx/reload", write=True, action="nginx.reload")
