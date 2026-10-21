"""overview 的 dns_zone（前端确认卡显示完整域名）与配置说明的镜像条目。"""

from __future__ import annotations

from dsh_control.domain.config_docs import config_doc_items


async def test_overview_includes_dns_zone(env):
    body = (await env.client.get("/api/overview")).json()
    assert body["dns_zone"] == "example.test"


def test_config_docs_has_image_entry_without_tag_literal():
    items = {i["key"]: i for i in config_doc_items()}
    assert "image" in items
    entry = items["image"]
    assert entry["label"] == "镜像"
    assert "控制台不构建" in entry["help"]
    # 守卫同款：说明文案里不许出现具体标签字面量
    assert "dsh-family:" not in entry["help"]
