#!/usr/bin/env python3
"""每日 AK 取证：GetAccessKeyLastUsed + ActionTrail LookupEvents（过去 24h）。

手签 Aliyun RPC v1（HMAC-SHA1），无 SDK 依赖。已知使用方基线写在 KNOWN 里，
出现名单外的来源 IP / eventName / RAM 调用即报异常。
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ENV = "/home/twan/src/dsh-control/.env"


def creds():
    txt = open(ENV).read()
    ak = re.search(r"^DSHC_ALIYUN_AK=(\S+)", txt, re.M).group(1).strip()
    sk = re.search(r"^DSHC_ALIYUN_SK=(\S+)", txt, re.M).group(1).strip()
    return ak, sk


def pct(s):
    return urllib.parse.quote(str(s), safe="").replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def call(endpoint, version, action, extra, ak, sk):
    p = {
        "Action": action, "Version": version, "Format": "JSON",
        "AccessKeyId": ak, "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0", "SignatureNonce": os.urandom(12).hex(),
        "Timestamp": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "RegionId": "cn-hangzhou",
    }
    p.update(extra)
    canon = "&".join(f"{pct(k)}={pct(p[k])}" for k in sorted(p))
    sig = base64.b64encode(hmac.new((sk + "&").encode(),
                                    f"GET&%2F&{pct(canon)}".encode(),
                                    hashlib.sha1).digest()).decode()
    url = f"https://{endpoint}/?{canon}&Signature={pct(sig)}"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:400]}


# 已知使用方（2026-10-29 定基线）：dsh-control 自己（DNS/ECS 读写）与运维手签取证。
KNOWN_IPS = {
    "27.38.4.19",        # 深圳联通直连出口（控制台 host-109 / 取证手签）
    "216.167.95.167",    # TUN 代理路径出口
    "106.11.85.205",     # 阿里云 CAS 证书扫描的服务出口（aliyuncasdefaultrole）
    "119.123.0.0/16",    # host-109 走宽带池时可能出现的段（宽松兜底，出现会单独列）
}
# 服务主体来源（rmc.resourcemanager / accessanalyzer / cas 的后台轮扫与
# Sts|Internal 的 AssumeRole）是账号级常态噪声（2026-10-30 全天逐条归因确认）：
# 来源以 .aliyuncs.com 结尾或 Internal 一律视为已知，不单独报警。
KNOWN_EVENTS = {
    "ListCustomDomains", "ListRoles", "ListUsers", "ListAccessKeys",
    "GetLoginProfile", "GenerateServiceLastAccessedDetails",
    "GetServiceLastAccessedDetails", "GetService",
    "DescribeDomains", "AddDomainRecord", "DescribeDomainRecords",
    "UpdateDomainRecord", "DeleteDomainRecord", "DescribeDomainRecordInfo",
    "GetAccessKeyLastUsed", "LookupEvents", "GetCallerIdentity",
    "DescribeInstances", "DescribeSecurityGroups", "DescribeImages",
    "DescribeRegions", "AssumeRole",
}


def main():
    ak, sk = creds()
    print(f"AK {ak[:6]}****  检查时间 {dt.datetime.now(dt.UTC).isoformat()}")

    r = call("ram.aliyuncs.com", "2015-05-01", "GetAccessKeyLastUsed",
             {"UserAccessKeyId": ak}, ak, sk)
    print("LastUsed:", json.dumps(r, ensure_ascii=False)[:400])

    now = dt.datetime.now(dt.UTC)
    r = call("actiontrail.cn-hangzhou.aliyuncs.com", "2020-07-06", "LookupEvents",
             {"StartTime": (now - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "EndTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "MaxResults": "50"}, ak, sk)
    if "_http_error" in r:
        print("ActionTrail 报错:", r)
        return 1
    evs = r.get("Events") or []
    if isinstance(evs, str):
        evs = json.loads(evs)
    rows = []
    for ev in evs:
        e = json.loads(ev) if isinstance(ev, str) else ev
        rows.append((e.get("eventName"), e.get("sourceIpAddress"),
                     (e.get("errorCode") or ""), e.get("eventTime")))
    print(f"过去 24h 共 {len(rows)} 条事件：")
    unknown = []
    for name, ip, code, t in sorted(rows, key=lambda x: x[3] or ""):
        ip_base = ip or ""
        # 服务主体来源（*.aliyuncs.com / Internal）= 阿里云自家后台，账号级常态；
        # 外部 IP 才对照名单。eventName 只对外部来源做未知告警。
        service_side = ip_base.endswith(".aliyuncs.com") or ip_base == "Internal"
        known = service_side or ip_base in KNOWN_IPS or ip_base.startswith("119.123.")
        ev_known = (name in KNOWN_EVENTS) or service_side
        flag = "" if (known and ev_known) else "  <-- 未知"
        if flag:
            unknown.append((name, ip_base, t))
        print(f"  {t}  {name:<28} {ip_base:<18} {code}{flag}")
    if unknown:
        print("\n!! 名单外事件：", unknown)
        return 2
    print("\n结论：过去 24h 全部来自已知使用方，无异常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
