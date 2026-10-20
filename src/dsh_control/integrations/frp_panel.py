"""frp-panel 隧道适配器（ARCHITECTURE §5.2）。

面板是内部服务，无公开文档；下表为实测形状（base_url、账号全部由构造参数传入，
代码里不出现任何站点地址）：

- 登录 ``POST /api/v1/auth/login {username,password}`` → Set-Cookie ``frp-panel-cookie``
  （JWT，httpOnly）；响应 ``{code,msg,body:{status,token}}``。
- 统一信封 ``{code,msg,body}``，``code != 200`` 一律抛 ``IntegrationError``。
- 列 ``POST /api/v1/proxy/list_configs {clientId?,page,pageSize}`` → ``body.total`` +
  ``body.proxy_configs[]``，记录形如
  ``{id,name,type,client_id,server_id,config,origin_client_id}``。
- 读回来的 ``config`` 是 JSON 字符串；写（update_config）时服务端该字段是 protobuf
  bytes，JSON 里必须传 **base64(JSON 字符串)** —— 两种形态本模块都能解（decode），
  编码固定走 base64（encode_config）。
- 建/改 ``POST /api/v1/proxy/update_config``；删 ``POST /api/v1/proxy/delete_config``。

安全边界（面板上跑着几十条别人的隧道）：
- ``ensure``/``delete`` 的名字必须以 ``dsh_`` / ``dshctl_`` 开头，否则 ``NotManaged``；
- ``delete`` 必须先读回、逐字核对 ``expect_name``，不匹配 ``NotManaged`` 且**不发**删除；
- 同名存在但端口不一致时抛 ``IntegrationError``，绝不静默改别人的门。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from .base import IntegrationError, NotManaged, Tunnel
from .dry_run import DryRunRecorder

DEFAULT_NAME_PREFIXES = ("dsh_", "dshctl_")

_LOGIN_PATH = "/api/v1/auth/login"
_LIST_PATH = "/api/v1/proxy/list_configs"
_GET_PATH = "/api/v1/proxy/get_config"
_UPDATE_PATH = "/api/v1/proxy/update_config"
_DELETE_PATH = "/api/v1/proxy/delete_config"


def encode_config(inner: dict[str, Any]) -> str:
    """隧道 config：内部 JSON → 字符串 → base64（服务端 protobuf bytes 字段）."""
    text = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_config(raw: Any) -> dict[str, Any]:
    """读回来的 config：优先按 JSON 字符串解（实测），兼容 base64(JSON) 形态。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        return json.loads(base64.b64decode(raw.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise IntegrationError("frp-panel 返回的 config 无法解析", code="bad_config") from exc


def tunnel_config_payload(
    *, name: str, local_ip: str, local_port: int, remote_port: int,
) -> dict[str, Any]:
    """面板期望的 config 内部形状（§5.2 逐字段照抄，键序也保持一致）."""
    return {
        "name": name,
        "type": "tcp",
        "transport": {"bandwidthLimit": ""},
        "loadBalancer": {"group": ""},
        "healthCheck": {"type": "", "intervalSeconds": 0},
        "localIP": local_ip,
        "localPort": local_port,
        "plugin": None,
        "remotePort": remote_port,
    }


class FrpPanel:
    """实现 base.FrpProvider。"""

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
        timeout: float = 15.0,
        name_prefixes: tuple[str, ...] = DEFAULT_NAME_PREFIXES,
    ) -> None:
        if not base_url:
            raise IntegrationError("frp-panel base_url 未配置", code="no_config")
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self.dry_run = dry_run
        self.recorder = recorder
        self.name_prefixes = name_prefixes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._logged_in = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- 会话

    async def _login(self) -> None:
        resp = await self._client.post(
            _LOGIN_PATH, json={"username": self._username, "password": self._password},
        )
        body = self._envelope(resp, _LOGIN_PATH)
        # cookie frp-panel-cookie 由 httpx cookie jar 自动保持；
        # body.token 同时留一份，可走 x-set-authorization 头。
        self._token = str((body or {}).get("token") or "")
        self._logged_in = True

    def _envelope(self, resp: httpx.Response, path: str) -> dict[str, Any]:
        if resp.status_code != 200:
            raise IntegrationError(
                f"frp-panel {path} HTTP {resp.status_code}", code=str(resp.status_code),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise IntegrationError(f"frp-panel {path} 返回非 JSON", code="bad_json") from exc
        code = data.get("code")
        if code != 200:
            raise IntegrationError(
                f"frp-panel {path}: {data.get('msg') or '未知错误'}", code=str(code),
            )
        return data.get("body") or {}

    async def _post(
        self, path: str, payload: dict[str, Any], *, write: bool, action: str,
    ) -> dict[str, Any]:
        if write and self.dry_run:
            if self.recorder is not None:
                self.recorder.record(
                    "frp", action, method="POST", target=self.base_url + path,
                    payload=payload, note="dry-run: 未发送",
                )
            return {}
        headers = {}
        if self._logged_in and getattr(self, "_token", ""):
            headers["x-set-authorization"] = self._token
        if not self._logged_in:
            await self._login()
        resp = await self._client.post(path, json=payload, headers=headers)
        if resp.status_code == 401:  # 会话过期：重登一次再试
            await self._login()
            resp = await self._client.post(path, json=payload, headers=headers)
        return self._envelope(resp, path)

    # ---------------------------------------------------------------- 读

    def _to_tunnel(self, rec: dict[str, Any]) -> Tunnel:
        cfg = decode_config(rec.get("config"))
        return Tunnel(
            proxy_id=int(rec.get("id") or 0),
            name=str(rec.get("name") or ""),
            client_id=str(rec.get("client_id") or ""),
            server_id=str(rec.get("server_id") or ""),
            type=str(rec.get("type") or "tcp"),
            local_ip=str(cfg.get("localIP") or ""),
            local_port=int(cfg.get("localPort") or 0),
            remote_port=int(cfg.get("remotePort") or 0),
            raw={"record": {k: v for k, v in rec.items() if k != "config"}, "config": cfg},
        )

    async def list(self, client_id: str | None = None) -> list[Tunnel]:
        out: list[Tunnel] = []
        page, page_size, total = 1, 200, None
        while total is None or len(out) < total:
            payload: dict[str, Any] = {"page": page, "pageSize": page_size}
            if client_id:
                payload["clientId"] = client_id
            body = await self._post(_LIST_PATH, payload, write=False, action="list_configs")
            recs = body.get("proxy_configs") or []
            out.extend(self._to_tunnel(r) for r in recs)
            total = int(body.get("total") or len(out))
            if not recs or len(recs) < page_size:
                break
            page += 1
            if page > 50:  # 防御：面板疯了也不无限翻
                break
        return out

    async def get(self, proxy_id: int) -> Tunnel | None:
        for t in await self.list():
            if t.proxy_id == proxy_id:
                return t
        return None

    async def find_by_name(self, name: str, client_id: str) -> Tunnel | None:
        for t in await self.list(client_id):
            if t.name == name:
                return t
        return None

    async def remote_port_taken(self, port: int) -> bool:
        # 面板里任何客户端声明了这个 remotePort 就算占用（无法从外部嗅 frps 的 listen）。
        return any(t.remote_port == port for t in await self.list())

    # ---------------------------------------------------------------- 写

    def _check_name(self, name: str, action: str) -> None:
        if not name.startswith(self.name_prefixes):
            raise NotManaged(
                f"拒绝{action}非本控制台命名的隧道 {name!r}"
                f"（前缀必须是 {'/'.join(self.name_prefixes)}）",
                code="not_managed",
            )

    async def ensure(
        self,
        *,
        name: str,
        client_id: str,
        server_id: str,
        local_ip: str,
        local_port: int,
        remote_port: int,
    ) -> Tunnel:
        self._check_name(name, "创建")
        existing = await self.find_by_name(name, client_id)
        if existing is not None:
            same = (existing.remote_port == remote_port
                    and existing.local_port == local_port
                    and (not local_ip or existing.local_ip == local_ip))
            if not same:
                raise IntegrationError(
                    f"隧道 {name} 已存在但端口不一致"
                    f"（面板: {existing.local_ip}:{existing.local_port}→"
                    f":{existing.remote_port}，期望: {local_ip}:{local_port}→:{remote_port}）；"
                    "拒绝改动既有隧道",
                    code="frp_conflict",
                    detail=f"proxy_id={existing.proxy_id}",
                )
            return existing

        inner = tunnel_config_payload(
            name=name, local_ip=local_ip, local_port=local_port, remote_port=remote_port,
        )
        payload = self._build_update_payload(
            None, name=name, client_id=client_id, server_id=server_id, inner=inner,
        )
        await self._post(_UPDATE_PATH, payload, write=True, action="update_config(create)")
        if self.dry_run:
            return Tunnel(
                proxy_id=0, name=name, client_id=client_id, server_id=server_id,
                local_ip=local_ip, local_port=local_port, remote_port=remote_port,
                raw={"manual": "dry-run"},
            )
        created = await self.find_by_name(name, client_id)
        if created is None:
            raise IntegrationError(
                f"隧道 {name} 创建后回读不到，请人工核对面板", code="frp_reread",
            )
        return created

    async def delete(self, proxy_id: int, *, expect_name: str) -> None:
        self._check_name(expect_name, "删除")
        tunnel = await self.get(proxy_id)
        if tunnel is None:
            raise NotManaged(
                f"面板上查不到 proxy_id={proxy_id}，拒绝盲删", code="not_managed",
            )
        self._check_name(tunnel.name, "删除")
        if tunnel.name != expect_name:
            raise NotManaged(
                f"删除被拦：proxy_id={proxy_id} 实际名字 {tunnel.name!r} ≠ 期望 {expect_name!r}",
                code="name_mismatch",
            )
        payload = self._build_delete_payload(tunnel)
        await self._post(_DELETE_PATH, payload, write=True, action="delete_config")

    # ------------------------------------------------- 待真机核对的报文构造
    # 建/删两个写接口的字段名以服务端报错为准（§5.2）。首发用 dry_run 打印，
    # 若报错只改这两个函数。
    @staticmethod
    def _build_update_payload(
        existing_id: int | None, *, name: str, client_id: str, server_id: str,
        inner: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "type": "tcp",
            "client_id": client_id,
            "server_id": server_id,
            "config": encode_config(inner),
        }
        if existing_id is not None:
            payload["id"] = existing_id
        return payload

    @staticmethod
    def _build_delete_payload(tunnel: Tunnel) -> dict[str, Any]:
        return {
            "id": tunnel.proxy_id,
            "name": tunnel.name,
            "client_id": tunnel.client_id,
            "server_id": tunnel.server_id,
        }
