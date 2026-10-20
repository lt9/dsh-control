"""DSH 配置解析（domain/dsh_config）：三条最容易骗人的地方都要钉住。

地址一律用 RFC 5737 的文档段（192.0.2.x），不要把现网值写进测试。
"""

from __future__ import annotations

from dsh_control.domain import dsh_config as dc

DUMP = """
# == @deepseek-ai/dsh-base
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      local:
        baseURL: http://192.0.2.10:8080/v1
        apiKeyEnv: LLAMA_API_KEY
        defaultContextWindow: 131072
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: local
    model: 出厂别名
    reasoningEffort: low
# == @deepseek-ai/dsh-base, patched by /data/profiles/web/cordis.patch.yml
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: local
    model: 真实模型名
    reasoningEffort: xhigh
# == @deepseek-ai/dsh-web-app
- id: webserver
  name: '@deepseek-ai/dsh-webserver'
  config:
    host: !!js ctx.webStartup.host ?? '127.0.0.1'
    port: !!js ctx.webStartup.port ?? 3080
- id: sandbox-policy
  name: '@deepseek-ai/dsh-sandbox'
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
"""


def test_取最后一条而不是出厂值():
    """同一个 id 在 dump 里出现多次：生效的是后面那条（profile 层覆盖过）。"""
    facts = dc.build_facts(DUMP)
    assert facts["model"] == "真实模型名"
    assert facts["base_url"] == "http://192.0.2.10:8080/v1"


def test_表达式不许冒充生效值():
    """!!js 是表达式，把 'ctx.webStartup.host' 当值显示就是在骗人。"""
    entry = dc.effective_entry(DUMP, "sandbox-policy")
    assert entry is not None
    assert dc._config_scalars(entry[1])["mode"].startswith("!!js")
    assert dc._scalar_or_js(dc._config_scalars(entry[1])["mode"]) == "运行期决定（配置里是表达式）"


def test_没有的条目说不是一句假话():
    facts = dc.build_facts(DUMP)
    team = next(g for g in facts["groups"] if g["title"] == "插件与智能体团队")
    智能体团队 = next(i for i in team["items"] if i["key"] == "智能体团队")
    assert 智能体团队["value"] == "未开启"
    assert "默认状态" in 智能体团队["note"]


def test_home层写了但还没重启要说明白():
    facts = dc.build_facts(DUMP, home_patch_present=True)
    team = next(g for g in facts["groups"] if g["title"] == "插件与智能体团队")
    智能体团队 = next(i for i in team["items"] if i["key"] == "智能体团队")
    assert "重启后生效" in 智能体团队["note"]


def test_团队开启后按官方配置展示():
    dump = DUMP + """- id: agent-team
  name: '@deepseek-ai/dsh-experimental-agent-team'
  config:
    maxMembers: 4
    maxTasks: 256
"""
    facts = dc.build_facts(dump)
    assert facts["agent_team"] is True
    team = next(g for g in facts["groups"] if g["title"] == "插件与智能体团队")
    assert next(i for i in team["items"] if i["key"] == "团队成员上限")["value"] == "4"


def test_profile清单里的组合包读得出来():
    bundles = dc.profile_bundles(
        '{"dsh":{"profile":{"bundles":["@deepseek-ai/dsh-base",'
        '"@deepseek-ai/dsh-experimental-agent-team-profile"]}}}')
    assert bundles == ["@deepseek-ai/dsh-base",
                       "@deepseek-ai/dsh-experimental-agent-team-profile"]
    assert dc.profile_bundles("") == []       # 读不到别崩，空数组就是空数组
    assert dc.profile_bundles("{不是 json") == []


def test_容器侧分段输出解析():
    facts = dc.container_facts(
        "@@HOME@@ /data\n"
        "@@PATCH@@ yes\n"
        "@@BUNDLES@@\n"
        '{"dsh":{"profile":{"bundles":["@deepseek-ai/dsh-base"]}}}\n'
        "@@SKILLS@@\ncomfy-image\noffice-notes\n@@END@@\n")
    assert facts["dsh_home"] == "/data"
    assert facts["home_patch"] is True
    assert facts["bundles"] == ["@deepseek-ai/dsh-base"]
    assert facts["skills"] == ["comfy-image", "office-notes"]


def test_生图那一格要说人话():
    """DSH 没有原生生图入口，这格不能留空，也不能让她以为点了就有生图按钮。"""
    facts = dc.build_facts(DUMP, skills=["comfy-image"])
    image = next(g for g in facts["groups"] if g["title"] == "生图")
    assert next(i for i in image["items"] if i["key"] == "DSH 原生入口")["value"] == "没有"
    assert next(i for i in image["items"] if i["key"] == "技能是否就位")["value"] == "已就位"

    没铺 = dc.build_facts(DUMP, skills=[])
    image2 = next(g for g in 没铺["groups"] if g["title"] == "生图")
    assert next(i for i in image2["items"] if i["key"] == "技能是否就位")["value"] == "缺失"


def test_万一有条目写了明文密钥也别外流():
    red = dc.redact_dump_secrets("  apiKey: sk-abcdefghijklmnop\n  model: ok")
    assert "sk-abcdefghijklmnop" not in red
    assert "model: ok" in red


def test_env写的模型名和生效值不一致要说破():
    """`docker restart` 不重读 .env，只有 compose 重建容器才换环境变量。

    这个差值不在页面上写出来，下一个人一定以为是控制台没生效。
    """
    facts = dc.build_facts(DUMP)
    一致 = dc.annotate_model_drift(dc.build_facts(DUMP), "真实模型名")
    assert "model_drift" not in 一致

    不一致 = dc.annotate_model_drift(facts, "Qwen3.9-新名字")
    assert "Qwen3.9-新名字" in 不一致["model_drift"]
    assert "compose 重建" in 不一致["model_drift"]
    model = next(g for g in 不一致["groups"] if g["title"] == "模型")
    默认 = next(i for i in model["items"] if i["key"] == "默认模型")
    assert "与 .env 不一致" in 默认["value"]
    assert "compose" in 默认["note"]

    # .env 没记值（旧快照）时不要凭空造一条"不一致"
    assert "model_drift" not in dc.annotate_model_drift(dc.build_facts(DUMP), "")
    assert "model_drift" not in dc.annotate_model_drift(dc.build_facts(DUMP), None)
