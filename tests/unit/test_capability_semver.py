"""semver 比对口径（U-2 定案的闸门用尺）。

预发布号语义是重点：`0.2.0-rc.1 < 0.2.0`（semver 2.0 §11），而
`0.2.1-rc.2 >= 0.2.0-rc.1`（核心段先比）——现网镜像 0.2.1-rc.2 必须过
speckit 的 `>=0.2.0-rc.1`，这是 P2 验收的一条硬账。
"""

from __future__ import annotations

import pytest

from dsh_control.integrations.capability_engine import (
    compare_versions,
    parse_version,
    satisfies,
)


def test_预发布小于正式版() -> None:
    assert compare_versions("0.2.0-rc.1", "0.2.0") == -1
    assert compare_versions("0.2.0", "0.2.0-rc.1") == 1


def test_预发布内部排序() -> None:
    assert compare_versions("0.2.0-rc.1", "0.2.0-rc.2") == -1
    assert compare_versions("0.2.0-rc.10", "0.2.0-rc.2") == 1   # 数字段按数值比
    assert compare_versions("0.2.0-alpha.1", "0.2.0-alpha.beta") == -1  # 数字段 < 字母段
    assert compare_versions("1.0.0-rc.1", "1.0.0-rc.1+build.5") == 0    # build 不参与


def test_核心段先比预发布不cross段() -> None:
    # 1.0.1-rc.1 的核心段已大于 1.0.0：预发布只在核心段相等时才压到正式版之下
    assert compare_versions("1.0.1-rc.1", "1.0.0") == 1


def test_speckit闸门现网镜像放行() -> None:
    # vendor §7 取证：镜像内 dsh 0.2.1-rc.2 必须过 >=0.2.0-rc.1
    assert satisfies("0.2.1-rc.2", ">=0.2.0-rc.1")
    assert satisfies("0.2.0-rc.1", ">=0.2.0-rc.1")
    assert satisfies("0.3.0", ">=0.2.0-rc.1")
    assert not satisfies("0.2.0-rc.0", ">=0.2.0-rc.1")
    assert not satisfies("0.1.9", ">=0.2.0-rc.1")


def test_合取区间与波浪尖帽() -> None:
    assert satisfies("1.4.2", ">=1.2.0,<2.0.0")
    assert not satisfies("2.0.0", ">=1.2.0,<2.0.0")
    assert satisfies("0.2.9", "^0.2.0-rc.1")          # 0.x 的 ^ 锁次版本
    assert not satisfies("0.3.0", "^0.2.0")
    assert satisfies("1.2.9", "~1.2.0")
    assert not satisfies("1.3.0", "~1.2.0")
    assert satisfies("1.3.0", "^1.2.0")               # major>0 的 ^ 只锁主版本
    assert not satisfies("2.0.0", "^1.2.0")


def test_空区间不设限_非法输入抛错() -> None:
    assert satisfies("9.9.9", "")
    with pytest.raises(ValueError):
        satisfies("0.2.1", ">=abc")
    with pytest.raises(ValueError):
        satisfies("x.y.z", ">=0.1.0")
    assert parse_version("0.2") is None               # 不是 semver 就别装
    assert parse_version("v1.2.3") == (1, 2, 3, ())   # 容忍 v 前缀
