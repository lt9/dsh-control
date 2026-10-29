"""kind 白名单迁移必须与模型里的集合**逐字一致**。

教训来自 0004：从 0003 复制迁移时只改了 `_KINDS_NEW`，忘了 `_KINDS_OLD` 里也要带上
上一个版本新增的 kind。结果新集合变成了"替换"而不是"追加"，现网 `credentials` 里
已经有一行 `llm_key`，`ADD CONSTRAINT` 当场报"violated by some row"——报错信息完全
看不出是漏了谁，靠人肉查表才知道。这个测试就是让那种事在提交前炸。
"""

from __future__ import annotations

import importlib.util
import pathlib

from dsh_control.db.models import CREDENTIAL_KINDS

_VERSIONS = pathlib.Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _load(name: str):
    path = next(_VERSIONS.glob(f"*_{name}.py"))
    spec = importlib.util.spec_from_file_location(f"mig_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_最后一个_kind_迁移的集合等于模型集合():
    # 0012 追加了 memory_user_key/memory_admin_key（记忆中枢 P1），它是最新一个改
    # credentials.kind 的迁移；0004（image_key）与 0010（mirror_token）作为历史
    # 钉桩继续留着不动。
    mod = _load("memory_key_kinds")
    assert mod._KINDS_NEW == CREDENTIAL_KINDS
    assert mod._KINDS_OLD == tuple(
        k for k in CREDENTIAL_KINDS if k not in ("memory_user_key", "memory_admin_key"))
    # 0012 的 _KINDS_OLD 必须接得上 0010 的 _KINDS_NEW——链不能断（0004 的漏项教训）。
    assert _load("image_channels")._KINDS_NEW == mod._KINDS_OLD


def test_old_必须是_new_的子集():
    """_OLD 少写一个 kind，就等于把那条从白名单里删了——现网必炸。"""
    mod = _load("memory_key_kinds")
    assert set(mod._KINDS_OLD) < set(mod._KINDS_NEW)
    assert set(mod._KINDS_NEW) - set(mod._KINDS_OLD) == {
        "memory_user_key", "memory_admin_key"}


def test_列注释列全了所有_kind():
    mod = _load("memory_key_kinds")
    for kind in mod._KINDS_NEW:
        assert kind in mod._KIND_COMMENT


def test_deploy_jobs_kind_链接得上():
    """deploy_jobs.kind 同一条规矩：0012 的 OLD 必须逐字等于 0008 的 NEW，
    NEW 只多 "memory"；模型 JOB_KINDS 必须覆盖 0012 的全部权值。"""
    from dsh_control.db.models import JOB_KINDS

    jobs0008 = _load("image_artifacts")
    jobs0012 = _load("memory_key_kinds")
    assert jobs0012._JOBS_OLD == jobs0008._KINDS_NEW
    assert set(jobs0012._JOBS_NEW) - set(jobs0012._JOBS_OLD) == {"memory"}
    assert set(jobs0012._JOBS_NEW) <= set(JOB_KINDS)
    for kind in jobs0012._JOBS_NEW:
        assert kind in jobs0012._JOBS_KIND_COMMENT
