"""安全门解析：判定必须自己算，且"缺条目"按不放行处理。"""
from __future__ import annotations

from dsh_control.domain.safety import MUST_BE_DISABLED, config_violations

BASE = "https://llm.example.test:8000/v1"


def _dump(**over: str | None) -> str:
    """拼一份接近真实 dump 的配置：providers.local 的 baseURL + 各插件条目。"""
    parts = ["config:", "  providers:", "    local:", f"      baseURL: {BASE}"]
    for pid in MUST_BE_DISABLED:
        v = over.get(pid, "true")
        if v is None:            # 条目不存在
            continue
        parts += [f"- id: {pid}", "  name: '@x/y'", f"  disabled: {v}"]
    web = over.get("web-deepseek-api", "absent")
    if web is not None and web != "absent":
        parts += ["- id: web-deepseek-api", f"  disabled: {web}"]
    elif web == "absent":
        parts += ["- id: web-deepseek-api", "  name: '@x/web'"]
    return "\n".join(parts) + "\n"


def test_safe_dump_passes():
    assert config_violations(_dump(), base_url=BASE) == []


def test_missing_entry_is_not_the_same_as_disabled():
    """条目不在配置里 ≠ 已禁用。放行这种就是把没验证过的东西当成安全。"""
    bad = config_violations(_dump(**{"llm-deepseek": None}), base_url=BASE)
    assert any("llm-deepseek" in m and "无法确认" in m for m in bad)


def test_enabled_cloud_provider_blocks():
    bad = config_violations(_dump(**{"deepseek-account": "false"}), base_url=BASE)
    assert any("deepseek-account" in m and "账单" in m for m in bad)


def test_search_tool_disabled_blocks():
    """官方模型禁了，联网搜索这条工具链还得活着，否则她的 web_search 静默失效。"""
    bad = config_violations(_dump(**{"web-deepseek-api": "true"}), base_url=BASE)
    assert any("web-deepseek-api" in m and "联网搜索" in m for m in bad)


def test_model_endpoint_pointing_elsewhere_blocks():
    """端点不是本站配的那个，等于她的内容要离开这张网。"""
    d = _dump().replace(BASE, "https://api.deepseek.com/v1")
    bad = config_violations(d, base_url=BASE)
    assert any("api.deepseek.com" in m for m in bad)


def test_empty_dump_blocks():
    assert config_violations("", base_url=BASE) != []
    assert any("拿不到容器内配置" in m for m in config_violations("   \n", base_url=BASE))


def test_adjacent_entry_cannot_borrow_disabled_flag():
    """`disabled: true` 必须落在自己那一块里，不能从下一条借过来。"""
    d = _dump(authorization=None)
    d += "- id: authorization\n  name: '@x/auth'\n- id: something-else\n  disabled: true\n"
    bad = config_violations(d, base_url=BASE)
    assert any("authorization" in m for m in bad)
