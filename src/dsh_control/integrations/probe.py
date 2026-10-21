"""公网探活（ARCHITECTURE §6.1 seq=80 / §3.4 判定表）。

打两次请求，全部不带 token：
1. 首页 ``GET <base>/`` —— 期望 2xx（TLS、反代、容器都活着才会 200）；
2. ``GET <base>/api/remote.mux`` 带浏览器同款 ``Origin`` ——
   401 = 反代头形状正确（拦在没登录）；
   403 = Host 与 Origin 又不相等了（反代把端口拼进了 Host），给出修正提示；
   连不上 = unreachable。

Origin 必须归一化成浏览器会给的样子（默认端口不带 :443/:80），否则自己就造出了
一个"不等"，探测结果不可信。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .base import ProbeResult

_MUX_PATH = "/api/remote.mux"

_FIX_HINT = (
    "反代把端口写进了上游 Host：去 nginxWebUI 打开该站 location（不是服务器级反代），"
    "把 Header Host 改成 $host（不能是 $host:$proxy_port），保存并 reload。"
    "症状对照：页面能开、REST 能用、只有 /api/remote.mux 一直 403。"
)


def browser_origin(base_url: str) -> str:
    """把 base_url 变成浏览器会发的 Origin（scheme://host，默认端口去掉）。"""
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"不是合法的 base_url: {base_url!r}")
    host = parts.hostname
    port = parts.port
    default = (parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)
    if port is not None and not default:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}"


class HttpPublicProbe:
    """实现 base.PublicProbe。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self.timeout = timeout

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def check(self, base_url: str, *, expect: str = "dsh") -> ProbeResult:
        origin = browser_origin(base_url)
        root = origin
        try:
            home = await self._client.get(root + "/", timeout=self.timeout)
        except httpx.HTTPError:
            return ProbeResult(
                ok=False, status_code=0, verdict="unreachable",
                hint=f"连不上 {root} 的首页（DNS/反代/frp/容器逐层查）",
            )
        try:
            mux = await self._client.get(
                root + _MUX_PATH, timeout=self.timeout, headers={"Origin": origin},
            )
        except httpx.HTTPError:
            return ProbeResult(
                ok=False, status_code=home.status_code, verdict="unreachable",
                hint=f"首页可达但 {_MUX_PATH} 连不上",
            )

        if expect != "dsh":
            # 门后面不是 DSH（比如主控台自己的入口）：那里没有准入墙，拿 DSH 的
            # "mux 必须 401" 去量它，只会报出一条指着别的服务的假警报。
            if 200 <= home.status_code < 300:
                return ProbeResult(ok=True, status_code=home.status_code, verdict="ok")
            return ProbeResult(
                ok=False, status_code=home.status_code, verdict="unexpected",
                hint=f"首页 HTTP {home.status_code}，不是 2xx——查容器在不在跑、"
                     f"NG 上游地址填的是不是隧道真正监听的那一个",
            )

        # DSH 开着准入墙：不带 token 时连首页都是 401（这是设计行为）。
        # 之前把"首页 2xx"当成唯一活着的证据，结果两次开通的最后一步都被判成
        # unexpected（工单上写着 status=401 verdict=unexpected，事后公网实测一切
        # 正常）——标准错了就会天天假警报，真出事反而没人看。
        if home.status_code == 403:
            return ProbeResult(
                ok=False, status_code=403, verdict="host_origin_mismatch", hint=_FIX_HINT,
            )
        if mux.status_code == 403:
            return ProbeResult(
                ok=False, status_code=403, verdict="host_origin_mismatch", hint=_FIX_HINT,
            )
        if 200 <= mux.status_code < 300:
            # 没带 token 却能打通受保护接口 = 准入墙没生效，绝不能当"开通成功"
            return ProbeResult(
                ok=False, status_code=mux.status_code, verdict="auth_open",
                hint=f"{_MUX_PATH} 没带 token 却返回 {mux.status_code}：准入墙没生效，"
                     "先别把这台当已开通交付",
            )
        if mux.status_code == 401:
            if 200 <= home.status_code < 300:
                return ProbeResult(
                    ok=True, status_code=mux.status_code, verdict="ok",
                    hint=f"首页 {home.status_code}、{_MUX_PATH} 401（拦在没登录，"
                         "反代 Host 头形状正确）",
                )
            if home.status_code == 401:
                return ProbeResult(
                    ok=True, status_code=mux.status_code, verdict="protected",
                    hint="首页和接口都拦在登录墙外（401）：站点活着，"
                         "带 token 的一键地址才是入口",
                )
            return ProbeResult(
                ok=False, status_code=home.status_code, verdict="unexpected",
                hint=f"{_MUX_PATH} 正常（401 拦在没登录），但首页是 HTTP "
                     f"{home.status_code}，看一眼容器状态",
            )
        return ProbeResult(
            ok=False, status_code=mux.status_code, verdict="unexpected",
            hint=f"{_MUX_PATH} 期望 401/403，实际 {mux.status_code}"
                 f"（首页 {home.status_code}）——确认打到的确实是 DSH 而不是别的服务",
        )
