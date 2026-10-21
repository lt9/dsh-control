"""时间与窗口工具：备份窗口解析、统一时区序列化。"""

from __future__ import annotations

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ..settings import get_settings

_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def tz_shanghai() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def in_window(window: str, now: datetime) -> bool:
    """window 形如 '03:20-04:00'（Asia/Shanghai）。支持跨午夜窗口。"""
    m = _WINDOW_RE.match(window.strip())
    if not m:
        return False
    start = time(int(m.group(1)), int(m.group(2)))
    end = time(int(m.group(3)), int(m.group(4)))
    local = now.astimezone(tz_shanghai()).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end  # 跨午夜，如 23:00-01:00


def iso(dt: datetime | None) -> str | None:
    """契约示例里所有时间都带 +08:00：统一按 Asia/Shanghai 序列化。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz_shanghai()).isoformat()
