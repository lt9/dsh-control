"""replace 前的库盘体检分类：只拦"会把好盘洗坏"的那种，其余大声提醒不拦。"""

from __future__ import annotations

from dsh_control.jobs.deploy import classify_drift


def row(name: str, verdict: str, proven: bool = True) -> dict:
    return {"server_name": name, "verdict": verdict, "dsh_proven": proven}


def test_only_db_dirty_blocks():
    """db_dirty = 库脏盘净：replace 会把正在服务的站点洗坏，必须拦。"""
    abort, warn = classify_drift([row("dsh-a.example.com", "db_dirty")])
    assert [r["server_name"] for r in abort] == ["dsh-a.example.com"]
    assert warn == []


def test_dirty_both_and_disk_dirty_warn_but_never_block():
    """这两种与本次开通无关：拦了就会让一个坏站点把所有开通永久卡死。"""
    abort, warn = classify_drift([
        row("office.example.com", "dirty_both"),
        row("dsh-b.example.com", "disk_dirty"),
    ])
    assert abort == []
    assert sorted(r["server_name"] for r in warn) == ["dsh-b.example.com",
                                                      "office.example.com"]


def test_unproven_sites_are_never_evidence():
    """证明不了后端是 DSH 的，状态码再"脏"也不是证据（现网误报的教训）。"""
    rows = [row("tierconsole.example.com", "db_dirty", proven=False),
            row("my.example.com", "dirty_both", proven=False),
            row("llm.example.com", "disk_dirty", proven=False)]
    assert classify_drift(rows) == ([], [])


def test_non_verdict_shapes_do_not_crash():
    """体检结果字段缺失/为 None 时不能抛——它跑在开通主流程里。"""
    rows = [{"server_name": "x"}, {"verdict": None, "dsh_proven": None},
            {"verdict": "unreachable", "dsh_proven": True},
            {"verdict": "inconclusive", "dsh_proven": True},
            {"verdict": "consistent", "dsh_proven": True}]
    assert classify_drift(rows) == ([], [])
