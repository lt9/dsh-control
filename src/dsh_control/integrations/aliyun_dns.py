"""阿里云解析适配器，RPC 签名 v1（HMAC-SHA1），ARCHITECTURE §5.3。

只用三个 Action：``DescribeDomainRecords`` / ``AddDomainRecord`` / ``DeleteDomainRecord``。
endpoint 是厂商公共入口（``alidns.aliyuncs.com``），不是站点专属值；zone、AK/SK、
RR 前缀全部来自构造参数。

签名流程（已真机只读验证）：公共参数 + 业务参数 → 按 key 排序 → RFC3986 百分号编码
拼 canonical query → ``StringToSign = POST&%2F&<enc(canon)>`` → ``Signature =
base64(HMAC-SHA1(key=SK + "&"))``。注意产品 API 版本是 ``2015-01-09``（写成
``2015-01-01`` 会被服务端拒 ``InvalidVersion``）。

硬白名单（违反一律 NotManaged，不发请求）：
- 操作的域固定为构造参数 ``zone``；
- RR 必须以 ``rr_prefixes``（默认 ``dsh-`` / ``dshctl``）开头；
- RR 不含点——通配证书 ``*.zone`` 只覆盖一级子域；
- 删除只按 RecordId，且该 id 必须能在本 zone 里回读到、RR 再过上面的白名单。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import urllib.parse
import uuid
from typing import Any

import httpx

from .base import DnsRecord, IntegrationError, NotManaged
from .dry_run import DryRunRecorder

DEFAULT_ENDPOINT = "https://alidns.aliyuncs.com/"
DEFAULT_VERSION = "2015-01-09"
DEFAULT_RR_PREFIXES = ("dsh-", "dshctl")
_T = datetime.UTC


def percent_encode(value: Any) -> str:
    """阿里云 RPC 签名 v1 要求的 RFC3986 编码：空格→%20、*→%2A、~ 不编码。"""
    return (
        urllib.parse.quote(str(value), safe="~")
        .replace("+", "%20")
        .replace("*", "%2A")
    )


def canonical_query(params: dict[str, Any]) -> str:
    return "&".join(
        f"{percent_encode(k)}={percent_encode(params[k])}" for k in sorted(params)
    )


def sign_v1(
    method: str, params: dict[str, Any], access_key_secret: str,
) -> tuple[str, str]:
    """返回 (string_to_sign, signature)。"""
    canon = canonical_query(params)
    string_to_sign = f"{method.upper()}&{percent_encode('/')}&{percent_encode(canon)}"
    digest = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return string_to_sign, base64.b64encode(digest).decode("ascii")


class AliyunDns:
    """实现 base.DnsProvider。"""

    manual_required = False  # 与 noop provider 对齐：jobs 层据此判 skipped

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        zone: str,
        dry_run: bool = False,
        recorder: DryRunRecorder | None = None,
        client: httpx.AsyncClient | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        api_version: str = DEFAULT_VERSION,
        rr_prefixes: tuple[str, ...] = DEFAULT_RR_PREFIXES,
        timeout: float = 20.0,
    ) -> None:
        if not zone:
            raise IntegrationError("AliyunDns 需要 zone（DSHC_DNS_ZONE）", code="no_config")
        self.zone = zone
        self._ak = access_key_id
        self._sk = access_key_secret
        self.dry_run = dry_run
        self.recorder = recorder
        self.endpoint = endpoint
        self.api_version = api_version
        self.rr_prefixes = rr_prefixes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- 白名单

    def _guard_rr(self, rr: str, action: str) -> None:
        lowered = rr.lower()
        if "." in rr:
            raise NotManaged(
                f"拒绝{action} RR {rr!r}：只允许 {self.zone} 的一级子域（不含点），"
                "通配证书不覆盖更深层级",
                code="not_managed",
            )
        if not lowered.startswith(self.rr_prefixes):
            raise NotManaged(
                f"拒绝{action} RR {rr!r}：前缀必须是 {'/'.join(self.rr_prefixes)}",
                code="not_managed",
            )

    # ---------------------------------------------------------------- 调用

    def _signed_params(self, action: str, biz: dict[str, Any]) -> dict[str, str]:
        params: dict[str, Any] = {
            "Action": action,
            "Version": self.api_version,
            "Format": "JSON",
            "AccessKeyId": self._ak,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": datetime.datetime.now(_T).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **biz,
        }
        _, signature = sign_v1("POST", params, self._sk)
        params["Signature"] = signature
        return {k: str(v) for k, v in params.items()}

    async def _call(self, action: str, biz: dict[str, Any], *, write: bool) -> dict[str, Any]:
        if write and self.dry_run:
            if self.recorder is not None:
                self.recorder.record(
                    "dns", action, method="POST", target=self.endpoint,
                    payload={"DomainName": self.zone, **biz}, note="dry-run: 未发送",
                )
            return {}
        params = self._signed_params(action, {"DomainName": self.zone, **biz})
        resp = await self._client.post(
            self.endpoint,
            content=urllib.parse.urlencode(params),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise IntegrationError(
                f"Aliyun {action} HTTP {resp.status_code} 且返回非 JSON", code="bad_json",
            ) from exc
        # 错误响应带 Code/Message；注意别把 AK/签名回显进错误信息
        if isinstance(data, dict) and data.get("Code"):
            raise IntegrationError(
                f"Aliyun {action} 失败: {data.get('Message') or ''}".strip(),
                code=str(data.get("Code")),
                detail=str(data.get("RequestId") or ""),
            )
        if resp.status_code != 200:
            raise IntegrationError(f"Aliyun {action} HTTP {resp.status_code}",
                                   code=str(resp.status_code))
        return data

    @staticmethod
    def _to_record(item: dict[str, Any]) -> DnsRecord:
        return DnsRecord(
            record_id=str(item.get("RecordId") or ""),
            rr=str(item.get("RR") or ""),
            type=str(item.get("Type") or ""),
            value=str(item.get("Value") or ""),
            status=str(item.get("Status") or ""),
        )

    # ---------------------------------------------------------------- 读

    async def find(self, rr: str) -> DnsRecord | None:
        data = await self._call(
            "DescribeDomainRecords",
            {"KeyWord": rr, "SearchMode": "EXACT", "PageNumber": 1, "PageSize": 50},
            write=False,
        )
        for item in (data.get("DomainRecords") or {}).get("Record") or []:
            record = self._to_record(item)
            if record.rr == rr:
                return record
        return None

    async def _iter_zone_records(self) -> list[DnsRecord]:
        out: list[DnsRecord] = []
        page = 1
        while page <= 20:
            data = await self._call(
                "DescribeDomainRecords",
                {"PageNumber": page, "PageSize": 100},
                write=False,
            )
            items = (data.get("DomainRecords") or {}).get("Record") or []
            out.extend(self._to_record(i) for i in items)
            if len(out) >= int(data.get("TotalCount") or 0) or not items:
                break
            page += 1
        return out

    # ---------------------------------------------------------------- 写

    async def create(self, rr: str, value: str, ttl: int = 600) -> DnsRecord:
        self._guard_rr(rr, "新增解析")
        data = await self._call(
            "AddDomainRecord",
            {"RR": rr, "Type": "A", "Value": value, "TTL": ttl},
            write=True,
        )
        record_id = str(data.get("RecordId") or "")
        if self.dry_run:
            return DnsRecord(record_id="", rr=rr, type="A", value=value, status="dry-run")
        if not record_id:
            # 建成功但响应里没有 id：回读兜底（§6.1 本来就要求回读）
            found = await self.find(rr)
            if found is None:
                raise IntegrationError(
                    f"A 记录 {rr}.{self.zone} 创建后回读不到", code="dns_reread",
                )
            return found
        return DnsRecord(record_id=record_id, rr=rr, type="A", value=value, status="")

    async def delete(self, record_id: str) -> None:
        if not record_id:
            raise NotManaged("拒绝删除空 RecordId", code="not_managed")
        # 删除只按 id，但该 id 必须能在本 zone 回读到、RR 过白名单——
        # 这样拿着别人的 RecordId 也删不动（域都不匹配，查不到即 NotManaged）。
        target = None
        for rec in await self._iter_zone_records():
            if rec.record_id == str(record_id):
                target = rec
                break
        if target is None:
            raise NotManaged(
                f"RecordId {record_id} 不在 {self.zone} 的记录里，拒绝删除",
                code="not_managed",
            )
        self._guard_rr(target.rr, "删除解析")
        await self._call("DeleteDomainRecord", {"RecordId": record_id}, write=True)
