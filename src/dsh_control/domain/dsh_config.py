"""把 `dsh web --dump-config` 的原文读成"人能看懂的几张表"。

为什么要有这个模块：控制台原来只展示容器配置（端口、目录、镜像），而她真正会问的
是"她现在用的是哪个模型、能不能生图、有没有 PPT 技能、智能体团队开没开"。这些都
在 DSH 自己的配置里，不在容器配置里。

三条硬规矩（都是踩出来的）：

1. **不引 YAML 依赖**。dump 里有 `!!js` 自定义 tag（`pyproject.toml` 里没有
   pyyaml，safe_load 遇到直接报错），而且我们只要十几个标量，行扫描够用。见
   domain/safety.py 同样的做法。
2. **同一个 id 在 dump 里会出现多次**（bundle 一层、profile 层再覆盖一层），
   生效的是**最后**那条。取第一条会拿到出厂值，于是"改了没生效"永远查不出来。
3. **`!!js` 开头的是表达式不是值**（webserver.host、sandbox-policy.mode、
   approval.policy 都在这类）。把表达式当值显示就是在骗人，一律标成"运行期决定"。
"""

from __future__ import annotations

import json
import re

# ── dump 里我们要看的条目 ────────────────────────────────────────────────────

LLM_ENTRY = "llm-pi-ai"
DEFAULT_MODEL_ENTRY = "agent-default-model"
PRESET_REGISTRY_ENTRY = "agent-preset-registry"
PRESET_STANDARD_ENTRY = "preset-standard"
AGENT_TEAM_ENTRY = "agent-team"
SKILL_FS_ENTRY = "skill-filesystem"
SKILL_OFFICE_ENTRY = "skill-office"
SESSION_TITLE_ENTRY = "session-title-llm"
WORKFLOW_ENTRY = "workflow-ptc"
WEB_RUNTIME_ENTRY = "web-runtime"
CONNECTION_ENTRY = "connection"
PERMISSION_ENTRY = "permission"
PRUNER_ENTRY = "tool-result-pruner"

# 被关掉的入口：控制台要说实话，别让她以为"点了就能装插件"。
PLUGIN_UI_DISABLED = (
    "tool-plugin-manager",
    "ui-plugin-manager",
    "ui-settings-plugins",
)

# DSH 0.2.1-rc.2 全量扫过 170 个插件的 config schema：没有任何生图键，
# providers 的 api 枚举里也没有 images 通道。生图只能靠技能调 HTTP。
IMAGE_NOTE = "DSH 没有原生生图入口，生图由技能调 ComfyUI 接口完成"

_JS = "!!js"
_ID_RE = re.compile(r"^- id: (\S+)\s*$")
_SOURCE_RE = re.compile(r"^# == (.+?)\s*$")
_NAMED_CHILD_RE = re.compile(r"^- (id|name): (\S+)\s*$")


def _dedent_key(line: str) -> tuple[int, str, str] | None:
    """把 `  key: value` 拆成 (缩进, key, value)；注释与列表项返回 None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "- ")):
        return None
    if ":" not in stripped:
        return None
    key, _, value = stripped.partition(":")
    return len(line) - len(line.lstrip()), key.strip(), value.strip()


def _blocks(dump: str) -> dict[str, list[tuple[str, list[str]]]]:
    """id → [(来源标注, 该块的行)]，按 dump 出现顺序。"""
    out: dict[str, list[tuple[str, list[str]]]] = {}
    source = ""
    current_id: str | None = None
    current_source = ""
    current: list[str] = []
    for line in (dump or "").splitlines():
        m = _SOURCE_RE.match(line)
        if m:
            source = m.group(1)
            continue
        m = _ID_RE.match(line)
        if m:
            if current_id is not None:
                out.setdefault(current_id, []).append((current_source, current))
            current_id, current, current_source = m.group(1), [line], source
            continue
        if current_id is not None:
            current.append(line)
    if current_id is not None:
        out.setdefault(current_id, []).append((current_source, current))
    return out


def effective_entry(dump: str, entry_id: str) -> tuple[str, list[str]] | None:
    """取生效的那一条（最后一次出现），返回 (来源标注, 行)。"""
    blocks = _blocks(dump).get(entry_id)
    return blocks[-1] if blocks else None


def entry_present(dump: str, entry_id: str) -> bool:
    return effective_entry(dump, entry_id) is not None


def _config_scalars(lines: list[str], prefix: str = "config:") -> dict[str, str]:
    """取 `config:` 下**直属**的标量键（嵌套对象与列表跳过）。"""
    start: int | None = None
    for idx, line in enumerate(lines):
        if line.strip() == prefix:
            start = idx
            break
    if start is None:
        return {}
    parent_indent = len(lines[start]) - len(lines[start].lstrip())
    out: dict[str, str] = {}
    child_indent: int | None = None
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= parent_indent:
            break  # config 块结束
        parsed = _dedent_key(line)
        if parsed is None:
            continue
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        _ind, key, value = parsed
        if value in ("", "|", ">", "[]", "{}"):
            continue
        out[key] = value
    return out


def _scalar_or_js(value: str | None) -> str:
    """把值整理成人话；`!!js` 表达式绝不冒充真值。"""
    if value is None:
        return "未设置"
    if value.startswith(_JS):
        return "运行期决定（配置里是表达式）"
    return value.strip().strip("'\"")


def _entry_scalar(dump: str, entry_id: str, key: str) -> str:
    entry = effective_entry(dump, entry_id)
    if entry is None:
        return "无此条目（未启用）"
    return _scalar_or_js(_config_scalars(entry[1]).get(key))


# ── 嵌套取键：providers.local.baseURL / preset-standard.plugins.compaction ────


def _nested_scalar(lines: list[str], path: list[str]) -> str | None:
    """在块里按 `path` 逐级找标量。用缩进推进，不解析 YAML。"""
    cur = lines
    for i, want in enumerate(path):
        found_at: int | None = None
        picked: list[str] | None = None
        for idx, line in enumerate(cur):
            parsed = _dedent_key(line)
            if parsed is None:
                continue
            _ind, key, value = parsed
            if key != want:
                continue
            if i == len(path) - 1:
                return value or None
            found_at = idx
            base = len(line) - len(line.lstrip())
            tail: list[str] = []
            for nxt in cur[idx + 1:]:
                nst = nxt.strip()
                if nst and (len(nxt) - len(nxt.lstrip())) <= base:
                    break
                tail.append(nxt)
            picked = tail
            break
        if picked is None:
            return None
        assert found_at is not None
        cur = picked
    return None


def _provider_value(dump: str, key: str, provider: str = "local") -> str:
    entry = effective_entry(dump, LLM_ENTRY)
    if entry is None:
        return "未配置本地模型（llm-pi-ai 不在配置里）"
    raw = _nested_scalar(entry[1], ["config", "providers", provider, key])
    if raw is None:
        return "未设置"
    return _scalar_or_js(raw)


def _list_values(lines: list[str], parent_key: str) -> list[str]:
    """取 `parent_key:` 下面那一层的列表项文本。"""
    out: list[str] = []
    inside = False
    base: int | None = None
    for line in lines:
        parsed = _dedent_key(line)
        if parsed and parsed[1] == parent_key and not inside:
            inside, base = True, parsed[0]
            continue
        if not inside:
            continue
        stripped = line.strip()
        if stripped and (len(line) - len(line.lstrip())) <= base and not stripped.startswith("- "):
            break
        if stripped.startswith("- "):
            out.append(stripped[2:].strip().strip("'\""))
    return out


def _preset_child(dump: str, plugin_id: str, key: str) -> str | None:
    """preset-standard 的 config.plugins 里某个子插件的 config 标量。"""
    entry = effective_entry(dump, PRESET_STANDARD_ENTRY)
    if entry is None:
        return None
    lines = entry[1]
    base_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.strip() == f"- id: {plugin_id}":
            base_idx = idx
            break
    if base_idx is None:
        return None
    tail: list[str] = []
    for nxt in lines[base_idx + 1:]:
        nst = nxt.strip()
        if nst.startswith("- id: "):
            break
        tail.append(nxt)
    return _nested_scalar(tail, ["config", key]) or None


# ── 组装 ─────────────────────────────────────────────────────────────────────


def profile_bundles(manifest_json: str) -> list[str]:
    """profiles/web/package.json → bundles 列表（智能体团队就开在这里）。"""
    try:
        data = json.loads(manifest_json or "")
    except (TypeError, ValueError):
        return []
    bundles = (data.get("dsh") or {}).get("profile", {}).get("bundles") or []
    return [str(b) for b in bundles if isinstance(b, str)]


def skill_names(listing: str) -> list[str]:
    """`ls` 输出的技能目录名（一行一个，忽略非目录噪音）。"""
    out = []
    for line in (listing or "").splitlines():
        name = line.strip().rstrip("/").split("/")[-1].strip()
        if name and not name.startswith(("total", "ls:", "No such")):
            out.append(name)
    return out


def build_facts(
    dump: str,
    *,
    bundles: list[str] | None = None,
    skills: list[str] | None = None,
    dsh_version: str = "",
    captured_at: str = "",
    home_patch_present: bool | None = None,
) -> dict:
    """把 dump + 两条补充命令的结果，整理成分组的可见配置。"""
    bundles = bundles or []
    skills = skills or []
    team_on = entry_present(dump, AGENT_TEAM_ENTRY)

    model = _entry_scalar(dump, DEFAULT_MODEL_ENTRY, "model")
    provider = _entry_scalar(dump, DEFAULT_MODEL_ENTRY, "provider")
    reasoning = _entry_scalar(dump, DEFAULT_MODEL_ENTRY, "reasoningEffort")
    compaction_ratio = _preset_child(dump, "compaction-basic", "thresholdRatio") or "未设置"
    preset_default = _entry_scalar(dump, PRESET_REGISTRY_ENTRY, "default")
    api_keys_env = _provider_value(dump, "apiKeyEnv")

    def items(pairs: list[tuple[str, str, str]]) -> list[dict]:
        return [{"key": k, "value": v, "note": n} for k, v, n in pairs]

    groups: list[dict] = []

    groups.append({
        "title": "模型",
        "items": items([
            ("默认模型", model, "会话默认用的那一个；下拉里可换"),
            ("提供方", provider, f"地址 {_provider_value(dump, 'baseURL')}"),
            ("推理强度", reasoning, "家庭版压到 xhigh，别让她在界面上改坏"),
            ("上下文窗口", _provider_value(dump, "defaultContextWindow"), "超过就触发压缩"),
            ("单次回复上限", _provider_value(dump, "defaultMaxTokens"), ""),
            ("密钥来源", api_keys_env, "这里只是环境变量名，不是密钥本身"),
            ("超时", _provider_value(dump, "timeoutMs"), "毫秒；慢模型首字可能等很久"),
        ]),
    })

    groups.append({
        "title": "上下文压缩",
        "items": items([
            ("压缩触发比例", compaction_ratio, "用到窗口的这个比例就自动压缩"),
            ("压缩预算", _preset_child(dump, "compaction-basic", "maxTokens") or "未设置",
             "每次压缩产出多少 token 的摘要"),
            ("工具结果截断", _preset_child(dump, PRUNER_ENTRY, "thresholdChars")
             or _entry_scalar(dump, PRUNER_ENTRY, "thresholdChars"),
             "单条工具输出超过这个字符数会被裁"),
        ]),
    })

    groups.append({
        "title": "生图",
        "items": items([
            ("DSH 原生入口", "没有", IMAGE_NOTE),
            ("接入方式", "技能 + 单文件客户端" if any(
                "comfy" in s.lower() or "image" in s.lower() for s in skills) else "未接入",
             "接入后她说一句「画张图」就能出图"),
            ("技能是否就位", "已就位" if any("comfy" in s.lower() for s in skills) else "缺失",
             "技能放在实例数据卷的 skills/ 下，DSH 会自动发现"),
        ]),
    })

    team_note = "已开启（团队成员、共享任务板、团队工具都在）" if team_on else \
        "未开启（默认状态：只能单个智能体干活）"
    if home_patch_present is True and not team_on:
        team_note = "已写入 home 层补丁，等容器重启后生效"
    groups.append({
        "title": "插件与智能体团队",
        "items": items([
            ("智能体团队", "已开启" if team_on else "未开启", team_note),
            ("团队成员上限", _entry_scalar(dump, AGENT_TEAM_ENTRY, "maxMembers") if team_on
             else "—", "开启后由它管并发，不再受子代理上限约束"),
            ("profile 组合包", "、".join(b.rsplit("/", 1)[-1] for b in bundles) or "未读到",
             "镜像里的出厂组合包；智能体团队是实验包，默认关"),
            ("插件安装入口", "已关闭",
             "防止误装带云端调用的第三方插件；要装请走控制台，别开界面"),
            ("插件清单页", "开启", "她能在设置里看到装了哪些插件"),
        ]),
    })

    office_on = entry_present(dump, SKILL_OFFICE_ENTRY)
    groups.append({
        "title": "技能（Skills）",
        "items": items([
            ("办公技能（Word/PPT/Excel）", "已开启" if office_on else "未开启",
             "官方 office 技能 + LibreOffice Kit，能做可编辑的 docx/pptx/xlsx"),
            ("已发现技能", "、".join(skills) if skills else "无",
             "来自实例数据卷 skills/ 目录，改完不用重启"),
            ("技能目录", "$DSH_HOME/skills（容器内 /data/skills）",
             "控制台开通实例时会自动铺好生图技能"),
        ]),
    })

    groups.append({
        "title": "预设与并发",
        "items": items([
            ("会话预设", preset_default, "standard / ptc / minimal / cordis 四选一"),
            ("子代理上限", _entry_scalar(dump, "subagent", "maxActiveSubagents"),
             "开启智能体团队后改由团队成员上限管"),
            ("工作流并发", _entry_scalar(dump, WORKFLOW_ENTRY, "maxConcurrentAgents"), ""),
            ("工作流总量", _entry_scalar(dump, WORKFLOW_ENTRY, "maxTotalAgents"), ""),
        ]),
    })

    groups.append({
        "title": "准入与权限",
        "items": items([
            ("公网地址", _entry_scalar(dump, WEB_RUNTIME_ENTRY, "publicUrl"),
             "一键登录地址就是拼在它后面"),
            ("登录态有效期", _entry_scalar(dump, CONNECTION_ENTRY, "cookieMaxAgeDays"),
             "天数；家庭版给得很长，免得她老被登出"),
            ("文件权限档", _entry_scalar(dump, PERMISSION_ENTRY, "defaultPreset"),
             "danger-full-access = 她让改文件时不再逐次弹窗"),
            ("DSH 版本", dsh_version or "未读到", "跟镜像 tag 不是一回事，别混着看"),
            ("读取时间", captured_at or "未记录", "点「重新读取」按最新值刷新"),
        ]),
    })

    return {
        "captured_at": captured_at,
        "dsh_version": dsh_version,
        "model": model,
        "base_url": _provider_value(dump, "baseURL"),
        "agent_team": team_on,
        "office_skills": office_on,
        "skills": skills,
        "bundles": bundles,
        "groups": groups,
        "source": "dsh web --dump-config（容器内实际生效值）",
    }


def annotate_model_drift(facts: dict, env_model: str | None) -> dict:
    """把"实例 .env 写的模型名"和"容器里生效的模型名"对账。

    踩过的坑：改了 `.env` 里的 `LLM_MODEL_ID`，然后在控制台点重启，页面上模型名
    纹丝不动——`docker restart` 不重读 `.env`，环境变量是容器**创建**时由 compose
    注进去的，只有 compose 重建容器才会换。这个差值不写出来，下一个人一定再花
    半小时查"是不是控制台没生效"。
    """
    want = (env_model or "").strip()
    if not want or not facts:
        return facts
    facts["env_model"] = want
    if want == facts.get("model"):
        return facts
    facts["model_drift"] = (
        f"实例 .env 写的是 {want}，容器里生效的是 {facts.get('model') or '空'}。"
        "重启不会重读 .env（Docker 的行为），要 compose 重建容器才生效")
    for group in facts.get("groups") or []:
        if group.get("title") != "模型":
            continue
        for item in group["items"]:
            if item["key"] == "默认模型":
                item["value"] = f"{facts.get('model') or '空'}（与 .env 不一致）"
                item["note"] = facts["model_drift"]
    return facts


def container_facts(raw: str) -> dict:
    """解析 DOCKER_FACTS_SNIPPET 的分段输出（容器里 POSIX sh 打的 @@标记@@）。"""
    out: dict = {"dsh_home": "", "home_patch": False, "bundles": [], "skills": []}
    section = ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("@@") and "@@" in stripped[2:]:
            marker, _, rest = stripped[2:].partition("@@")
            section = marker
            if marker == "HOME":
                out["dsh_home"] = rest.strip()
            elif marker == "PATCH":
                out["home_patch"] = rest.strip() == "yes"
            continue
        if section == "SKILLS" and stripped:
            out["skills"].append(stripped)
        elif section == "BUNDLES":
            out["bundles_json"] = out.get("bundles_json", "") + line + "\n"
    if out.get("bundles_json"):
        out["bundles"] = profile_bundles(out.pop("bundles_json"))
    return out


def redact_dump_secrets(dump: str) -> str:
    """兜底：万一以后有条目把密钥写成明文，别让它进数据库或页面。"""
    return re.sub(r"(?i)((?:api[_-]?key|token|secret)\s*:\s*)\S+",
                  r"\1[已隐藏]", dump or "")
