"""aliyun_dns.py：RPC v1 签名金样例、白名单、防误删。全部 MockTransport。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import urllib.parse

import httpx
import pytest

from dsh_control.integrations.aliyun_dns import (
    DEFAULT_VERSION,
    AliyunDns,
    canonical_query,
    percent_encode,
    sign_v1,
)
from dsh_control.integrations.base import IntegrationError, NotManaged
from dsh_control.integrations.dry_run import DryRunRecorder

ZONE = "example.com"
ENDPOINT = "https://alidns.aliyuncs.com/"


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 签名（纯函数）

def test_percent_encode_rfc3986_rules():
    assert percent_encode("a b*c~d/e:f+g%h") == "a%20b%2Ac~d%2Fe%3Af%2Bg%25h"
    assert percent_encode("2026-01-01T00:00:00Z") == "2026-01-01T00%3A00%3A00Z"


def test_canonical_query_sorted_and_encoded():
    q = canonical_query({"B": "2", "a": "1", "C c": "x y"})
    assert q == "B=2&C%20c=x%20y&a=1"


def test_sign_v1_golden_string_to_sign():
    params = {
        "Action": "DescribeDomainRecords",
        "AccessKeyId": "testid",
        "DomainName": ZONE,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": "fixed-nonce",
        "SignatureVersion": "1.0",
        "Timestamp": "2026-01-01T00:00:00Z",
        "Version": DEFAULT_VERSION,
    }
    expected_sts = (
        "POST&%2F&"
        "AccessKeyId%3Dtestid%26Action%3DDescribeDomainRecords%26DomainName%3Dexample"
        ".com%26Format%3DJSON%26SignatureMethod%3DHMAC-SHA1%26SignatureNonce%3Dfixed"
        "-nonce%26SignatureVersion%3D1.0%26Timestamp%3D2026-01-01T00%253A00%253A00Z"
        "%26Version%3D2015-01-09"
    )
    sts, sig = sign_v1("POST", params, "testsecret")
    assert sts == expected_sts
    # 签名独立复算：key = SK + "&"，算法 HMAC-SHA1 → base64
    expect = base64.b64encode(
        hmac.new(b"testsecret&", expected_sts.encode(), hashlib.sha1).digest()
    ).decode()
    assert sig == expect


def test_api_version_is_the_one_the_service_accepts():
    assert DEFAULT_VERSION == "2015-01-09"


# ------------------------------------------------------------------ 传输层

FIXTURE = {
    "RecordId": "9000000000000000001", "RR": "dsh-alpha", "Type": "A",
    "Value": "203.0.113.10", "Status": "ENABLE", "TTL": 600, "DomainName": ZONE,
}


class FakeAliyun:
    def __init__(self, records=(FIXTURE,), error=None):
        self.records = list(records)
        self.error = error
        self.requests: list[dict] = []

    def actions(self):
        return [urllib.parse.parse_qs(r.content.decode())["Action"][0]
                for r in self.requests]

    def params(self, action):
        for r in self.requests:
            p = urllib.parse.parse_qs(r.content.decode())
            if p.get("Action", [""])[0] == action:
                return p
        return None

    def _handler(self, request: httpx.Request) -> httpx.Response:
        params = urllib.parse.parse_qs(request.content.decode())
        self.requests.append(request)
        action = params.get("Action", [""])[0]
        assert "Signature" in params and "SignatureNonce" in params
        assert params["Version"][0] == DEFAULT_VERSION
        if self.error:   # error=(Code, Message)：任何写请求都按面板报错回
            return httpx.Response(400, json={"Code": self.error[0], "Message": self.error[1],
                                             "RequestId": "req-1"})
        if action == "DescribeDomainRecords":
            kw = params.get("KeyWord", [None])[0]
            mode = params.get("SearchMode", [None])[0]
            rows = self.records
            if kw:
                rows = [r_ for r_ in rows if (r_["RR"] == kw if mode == "EXACT"
                                              else kw in r_["RR"])]
            return httpx.Response(200, json={
                "DomainRecords": {"Record": rows}, "TotalCount": len(rows),
                "PageNumber": 1, "PageSize": 100, "RequestId": "req-2"})
        if action == "AddDomainRecord":
            rec = dict(FIXTURE, RR=params["RR"][0], Value=params["Value"][0],
                       RecordId="9000000000000000002")
            self.records.append(rec)
            return httpx.Response(200, json={"RecordId": rec["RecordId"], "RequestId": "req-3"})
        if action == "DeleteDomainRecord":
            rid = params["RecordId"][0]
            self.records = [r_ for r_ in self.records if r_["RecordId"] != rid]
            return httpx.Response(200, json={"RequestId": "req-4"})
        return httpx.Response(400, json={"Code": "NoPermission", "Message": "deny"})

    def dns(self, dry_run=False, recorder=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
        return AliyunDns(access_key_id="testid", access_key_secret="testsecret",
                         zone=ZONE, dry_run=dry_run, recorder=recorder, client=client)


# ------------------------------------------------------------------ 白名单与行为

def test_find_exact_rr():
    api = FakeAliyun()
    rec = run(api.dns().find("dsh-alpha"))
    assert rec is not None and rec.record_id.endswith("001") and rec.value == "203.0.113.10"
    p = api.params("DescribeDomainRecords")
    assert p["SearchMode"][0] == "EXACT" and p["DomainName"][0] == ZONE
    assert run(api.dns().find("nothing-here")) is None


@pytest.mark.parametrize("bad", ["www", "legacy-door", "deep.child", "dsh", ""])
def test_create_rejects_non_whitelisted_rr_without_request(bad):
    api = FakeAliyun()
    with pytest.raises(NotManaged):
        run(api.dns().create(bad, "203.0.113.10"))
    assert api.requests == []


def test_create_sends_fixed_shape_and_returns_id():
    api = FakeAliyun()
    rec = run(api.dns().create("dsh-beta", "203.0.113.10", ttl=600))
    assert rec.record_id.endswith("002") and rec.rr == "dsh-beta"
    p = api.params("AddDomainRecord")
    assert p["Type"][0] == "A" and p["TTL"][0] == "600" and p["RR"][0] == "dsh-beta"


def test_create_allows_dshctl_console_rr():
    api = FakeAliyun()
    rec = run(api.dns().create("dshctl", "203.0.113.10"))
    assert rec.rr == "dshctl"


def test_delete_requires_id_visible_in_zone_with_ok_rr():
    api = FakeAliyun()
    run(api.dns().delete("9000000000000000001"))
    assert "DeleteDomainRecord" in api.actions()
    assert api.records == []


def test_delete_unknown_id_blocked_and_no_delete_action():
    api = FakeAliyun()
    with pytest.raises(NotManaged):
        run(api.dns().delete("8888888888888888888"))
    assert "DeleteDomainRecord" not in api.actions()


def test_delete_known_id_but_foreign_rr_blocked():
    foreign = dict(FIXTURE, RR="mail")
    api = FakeAliyun(records=(foreign,))
    with pytest.raises(NotManaged):
        run(api.dns().delete(foreign["RecordId"]))
    assert "DeleteDomainRecord" not in api.actions()


def test_error_envelope_maps_to_integration_error_without_secrets():
    api = FakeAliyun(error=("InvalidDomainName", "domain not exist"))
    with pytest.raises(IntegrationError) as ei:
        run(api.dns().create("dsh-x", "203.0.113.10"))
    msg = str(ei.value)
    assert ei.value.code == "InvalidDomainName"
    assert "testsecret" not in msg and "Signature" not in msg


def test_dry_run_records_writes_only():
    rec = DryRunRecorder()
    api = FakeAliyun()
    dns = api.dns(dry_run=True, recorder=rec)
    out = run(dns.create("dsh-z", "203.0.113.10"))
    run(dns.delete("9000000000000000001"))
    assert out.status == "dry-run" and out.record_id == ""
    assert "DeleteDomainRecord" not in api.actions()
    assert "AddDomainRecord" not in api.actions()
    assert "DescribeDomainRecords" in api.actions()  # 读照常真发
    assert [e.adapter for e in rec.entries] == ["dns", "dns"]
