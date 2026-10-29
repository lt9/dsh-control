"""控制台自己的安全门：从容器 dump 出的生效配置直接判"云上的路堵死了没有"。

为什么不拿 kit 的 verify.sh 当门：那是给人蹲在终端看的报告，历史上还出过
`set -uo pipefail` + `printf | grep -q` 的假警报——同一份配置在 192.0.2.10
上报 10 项不通过、在 NAS 上报 0 项（两台 dump-config 逐行相同）。**花钱**和
**把她的内容外传**这两条是红线，判定必须由控制台自己算，不能依赖外部脚本的
写法对不对。verify.sh 照跑，但只作为给人的参考。

判定用的是 `dsh web --dump-config` 的生效结果，不是仓库里的模板：模板对而
容器没生效，等于没配。
"""
from __future__ import annotations

import re

# 必须关掉：任何一条活着，她这台机器就还能往云端发请求（产生账单，或把内容传出去）
MUST_BE_DISABLED: tuple[str, ...] = (
    "llm-deepseek",                        # 官方 API key 的模型 provider
    "llm-deepseek-account",                # 官方账号登录
    "deepseek-account",                    # 账号体系
    "session-log-deepseek",                # 会话日志上云
    "plugin-package-inventory-deepseek",   # 插件清单上报
    "authorization",                       # 授权/登录远端
)
# 必须活着：官方模型禁用后，联网搜索走 api.deepseek.com 的工具链路还得能用
MUST_STAY_ON: tuple[str, ...] = ("web-deepseek-api",)

# 一个条目块取的行数：dump 里同一 id 的 name/disabled 等字段紧跟着，跨条目会误判
_BLOCK = 12
_DISABLED_RE = re.compile(r"^\s+disabled:\s*true\s*$")
_BASE_URL_RE = re.compile(r"^\s+baseURL:\s*(\S+)\s*$", re.MULTILINE)


def _block(lines: list[str], plugin_id: str) -> list[str] | None:
    """返回某个 `- id:` 条目的那一段，**到下一个条目为止**。

    只按固定行数开窗（kit 原来用 `grep -A8`）会串味：下一条目的 `disabled: true`
    会被当成这一条的，于是"没禁用"被读成"已禁用"——正是我们要堵的那类误判。
    """
    want = f"- id: {plugin_id}"
    for i, ln in enumerate(lines):
        if ln.strip() == want:
            out = [ln]
            for nxt in lines[i + 1:i + 1 + _BLOCK]:
                if nxt.startswith("- id:"):
                    break
                out.append(nxt)
            return out
    return None


def config_violations(dump: str, *, base_url: str = "",
                      memory_base_url: str = "") -> list[str]:
    """返回违规说明列表；空列表 = 安全门通过。

    宁可误报也不放行：条目**找不到**也算违规（"没有这一项"不等于"已禁用"）。

    ``memory_base_url``（记忆接线后的复核口径）：实例的模型出口被 home 层补丁
    切到自家 Proxy 后，llm-deepseek **必须活着**且 baseURL 就是本站记忆出口——
    它此时是「这张网内的路由器」，不再是云端路径；其余 MUST_BE_DISABLED 一条不让。
    全局 baseURL 核对放宽为「本站两个合法出口之一」（.env 的 LLM_BASE_URL 与记忆
    出口在 dump 里并存，谁是第一条取决于组合顺序，不押顺序）。传空 = 行为与
    引入记忆功能之前逐字节一致。
    """
    lines = (dump or "").splitlines()
    out: list[str] = []
    if not (dump or "").strip():
        return ["拿不到容器内配置（dsh web --dump-config 输出为空），无法判定安全门"]

    for pid in MUST_BE_DISABLED:
        if memory_base_url and pid == "llm-deepseek":
            continue  # 记忆接线态：下面按 baseURL 单独审它，见 docstring
        blk = _block(lines, pid)
        if blk is None:
            out.append(f"配置里没有 {pid} 这一项，无法确认云端路径已关闭")
        elif not any(_DISABLED_RE.match(x) for x in blk[1:]):
            out.append(f"{pid} 没有被禁用 —— 这台机器还能往云端发请求、产生账单")

    if memory_base_url:
        blk = _block(lines, "llm-deepseek")
        if blk is None:
            out.append("配置里没有 llm-deepseek 这一项——记忆接线后模型出口本经它走"
                       "自家 Proxy，现在出口不明")
        else:
            m = _BASE_URL_RE.search("\n".join(blk))
            got = m.group(1) if m else ""
            if got != memory_base_url:
                out.append(f"llm-deepseek 的 baseURL 是 {got or '（空）'}，不是本站记忆出口 "
                           f"{memory_base_url} —— 指向别处就意味着她的内容会离开这张网")

    for pid in MUST_STAY_ON:
        blk = _block(lines, pid)
        if blk is not None and any(_DISABLED_RE.match(x) for x in blk[1:]):
            out.append(f"{pid} 被禁用了 —— 她会失去联网搜索")

    if base_url:
        m = _BASE_URL_RE.search(dump)
        got = m.group(1) if m else ""
        allowed = {base_url} | ({memory_base_url} if memory_base_url else set())
        if got not in allowed:
            out.append(f"模型端点是 {got or '（空）'}，不是本站配置的 {base_url}"
                       + (f"（也不是记忆出口 {memory_base_url}）" if memory_base_url else "")
                       + " —— 指向别处就意味着她的内容会离开这张网")
    return out
