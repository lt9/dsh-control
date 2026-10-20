"""服务端自动选主机（契约 §6.1.1）：enabled 主机里 data_root 剩余空间最大的那台，
机械盘优先（DSHC_PREFER_HDD）。这里只有纯解析与排序，SSH 探测在调用方。"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 一行探测输出："<可用KB> <rotational 0/1>"
_PROBE_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s*$")


@dataclass(slots=True)
class HostProbe:
    host_id: int
    avail_bytes: int | None   # data_root 剩余字节；None = 没探到
    rotational: bool | None   # True=机械盘；None=没探到


def parse_probe_output(text: str) -> tuple[int | None, bool | None]:
    """df -P + /sys/block/*/queue/rotational 的组合输出。解析失败给 None。"""
    if not text:
        return None, None
    m = _PROBE_RE.search(text)
    if not m:
        return None, None
    return int(m.group(1)) * 1024, m.group(2) == "1"


def probe_command(data_root: str) -> str:
    """单条 shell：输出 '<可用KB> <rotational>'。rotational 探不到按 0。"""
    root = data_root.replace("'", "")
    return (
        f"avail=$(df -P '{root}' 2>/dev/null | awk 'NR==2 {{print $4}}'); "
        f"dev=$(df -P '{root}' 2>/dev/null | awk 'NR==2 {{gsub(/[0-9]+$/,\"\",$1); print $1}}'); "
        f"rota=$(cat /sys/block/$(basename \"$dev\")/queue/rotational 2>/dev/null || echo 0); "
        f"echo \"$avail $rota\""
    )


def rank(probes: list[HostProbe], prefer_hdd: bool = True) -> HostProbe | None:
    """排序取最优：可达 > （prefer_hdd 时机械盘优先）> 剩余空间大 > host_id 小。
    全部不可达返回 None。"""
    reachable = [p for p in probes if p.avail_bytes is not None]
    if not reachable:
        return None

    def key(p: HostProbe):
        hdd = 1 if (prefer_hdd and p.rotational) else 0
        return (hdd, p.avail_bytes, -p.host_id)

    return max(reachable, key=key)
