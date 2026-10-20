"""单元：slug / 派生命名 / 主机挑选 / 备份窗口 / 日志脱敏。"""

from __future__ import annotations

from datetime import datetime

from dsh_control.domain import host_pick
from dsh_control.domain.slug import derive, is_valid_label, next_slug, token_mask
from dsh_control.domain.timeutil import in_window, iso
from dsh_control.jobs.runner import redact_text


def test_next_slug_sequential_zero_padded():
    assert next_slug([], "dsh-") == "dsh-01"
    assert next_slug(["dsh-01", "dsh-02", "dsh-03"], "dsh-") == "dsh-04"
    assert next_slug(["dsh-09"], "dsh-") == "dsh-10"
    # released 的也占号：slug 永不复用
    assert next_slug(["dsh-11", "legacy1", "other"], "dsh-") == "dsh-12"


def test_derive_names():
    n = derive("dsh-07", "example.test")
    assert n.container == "dsh-07"
    assert n.tunnel == "dsh_07"
    assert n.upstream == "DSH_07"
    assert n.server_name == "dsh-07.example.test"


def test_is_valid_label_single_level():
    assert is_valid_label("dsh-01")
    assert not is_valid_label("a.b")   # 不许点：一级子域
    assert not is_valid_label("-bad")
    assert not is_valid_label("")


def test_token_mask_shape():
    assert token_mask("M1jqabcdIV0") == "M1jq…IV0"
    assert token_mask("") == ""


def test_host_pick_parse_and_rank():
    avail, rota = host_pick.parse_probe_output("10485760 1\n")
    assert avail == 10485760 * 1024 and rota is True
    assert host_pick.parse_probe_output("garbage") == (None, None)

    probes = [
        host_pick.HostProbe(1, 10 * 1024**3, True),    # 机械盘 10G
        host_pick.HostProbe(2, 100 * 1024**3, False),  # 固态 100G
    ]
    assert host_pick.rank(probes, prefer_hdd=True).host_id == 1
    assert host_pick.rank(probes, prefer_hdd=False).host_id == 2
    assert host_pick.rank([host_pick.HostProbe(1, None, None)]) is None


def test_in_window_backup_and_cross_midnight():
    inside = datetime(2026, 10, 20, 3, 30, tzinfo=iso_tz())
    outside = datetime(2026, 10, 20, 4, 1, tzinfo=iso_tz())
    assert in_window("03:20-04:00", inside)
    assert not in_window("03:20-04:00", outside)
    # 跨午夜窗口
    late = datetime(2026, 10, 20, 0, 10, tzinfo=iso_tz())
    assert in_window("23:50-00:30", late)


def iso_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Shanghai")


def test_iso_serializes_to_shanghai():
    dt = datetime(2026, 10, 20, 0, 0, tzinfo=iso_tz())
    assert iso(dt).endswith("+08:00")
    assert iso(None) is None


def test_redact_text_masks_secrets():
    line = ("open https://x.test/?token=SECRET123 token=abc password: hunter2 "
            "Authorization: Bearer a.b.c access_key_id=LTAI5t")
    out = redact_text(line)
    for leak in ("SECRET123", "hunter2", "a.b.c", "LTAI5t", "token=abc"):
        assert leak not in out
    assert "https://x.test/?token=***" in out
