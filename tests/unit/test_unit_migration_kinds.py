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
    # 0010 追加了 mirror_token（§6.1.6），它是最新一个改 credentials.kind 的迁移；
    # 0004（image_key）作为历史钉桩继续留着不动。
    mod = _load("image_channels")
    assert mod._KINDS_NEW == CREDENTIAL_KINDS
    assert mod._KINDS_OLD == tuple(
        k for k in CREDENTIAL_KINDS if k != "mirror_token")
    # 0010 的 _KINDS_OLD 必须接得上 0004 的 _KINDS_NEW——链不能断。
    assert _load("image_key_kind")._KINDS_NEW == mod._KINDS_OLD


def test_old_必须是_new_的子集():
    """_OLD 少写一个 kind，就等于把那条从白名单里删了——现网必炸。"""
    mod = _load("image_channels")
    assert set(mod._KINDS_OLD) < set(mod._KINDS_NEW)
    assert set(mod._KINDS_NEW) - set(mod._KINDS_OLD) == {"mirror_token"}


def test_列注释列全了所有_kind():
    mod = _load("image_channels")
    for kind in mod._KINDS_NEW:
        assert kind in mod._KIND_COMMENT
