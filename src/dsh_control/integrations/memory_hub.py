"""记忆中枢（TencentDB-Agent-Memory）适配器——控制台的第 4 个外部适配器。

为什么用户生命周期必须长在控制台这边（MEMORY-PANEL-EMBED.md ②的取证结论）：
core 的鉴权分层里，**用户级权限全部在 Panel 侧**，而本部署 Panel 自带的建号链路
是坏的——它拿实例 api_key（长度 5 的回落值）当 admin 身份，core 一律 401。
唯一能过 `assertCanManageUsers` 的是真 admin key。所以建用户、发 key、verify、
回收这些动作只有控制台（持 admin key 直连 `/v3/meta/*`）做得动。

三条硬规矩（ADR-0001 决策一）：

1. **admin key 只住 credentials（kind=memory_admin_key, name=memory-admin）**，
   从不出现在命令行、日志与错误信息里；业务 key 同样只住 credentials
   （kind=memory_user_key, name=dshctl-<slug>），进容器一律 stdin。
2. **幂等靠确定性 user_id**：`user_id = dshctl-<slug>`。core 的同名 `user/create`
   **不去重**（2026-10-29 实测，vendor §7），没有确定性 user_id 就没有幂等，
   重跑一次多一批孤儿用户。已存在时 core 回 409 `duplicate_entry`，按幂等短路。
3. **停用＝回收（U-7 口径）**：`revoke` 业务用户全部 active key；记忆数据永远
   留在 Hub 卷里，本适配器**没有任何删数据的动作**。

出站请求形状照 MemoryCore/v3-api-memorycore-doc.md §3.7（55 接口里只用 6 个）：
元数据面 `/v3/meta/*` 带 `x-tdai-service-id` + `x-tdai-user-key`（admin key），
信封 `{code, message, data}`，`code != 0` 即失败（message 形如 `duplicate_entry: …`，
前缀是稳定机器码）；`auth/verify` 免 user-key、非法 key 也回 200/code=0，
判据必须看 `data.valid`，不能按 401 分支。

dry_run 语义与 frp_panel 同德性：写动作（建用户/发 key/加成员/回收）一个字节
都不发出去，只记 recorder；读动作（verify/get/health/探针）照跑——都是只读。
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from typing import Any

import httpx

from .base import IntegrationError
from .dry_run import DryRunRecorder

#: home 层补丁的铺设记号（marker 是安装与卸载之间的唯一契约，ADR-0001 决策二）。
MEMORY_MARKER = "# dsh-control-memory"
#: 段尾记号：摘除按「起记号行 → 止记号行」整段删，绝不吞掉后面别人铺的段。
MEMORY_MARKER_END = MEMORY_MARKER + ":end"
#: P0 手工接线留下的旧记号（docs/site/MEMORY-P0.md）。status 里见到它同样算
#: 「已接线」——兼容判据：grep -qF 命中 "# dsh-control-p0" 即为 legacy 已接线。
P0_MARKER = "# dsh-control-p0"
#: 业务 key 在容器 refs: 段里的键名（home 层补丁的 apiKeyEnv 就指它）。
REFS_KEY_NAME = "PROXY_USER_KEY"

_ADMIN_CRED_NAME = "memory-admin"

#: 能安全穿过「sh 变量赋值 + sed 替换 + yaml 值」三关的 key 形状。
#: core 的 key 实测 `sk-mem-` 前缀 + base62；Panel 侧约束是 8–256 位无空白。
_KEY_SHAPE_RE = re.compile(r"[A-Za-z0-9_-]{8,256}")


def mask_key(key: str) -> str:
    """打码：留前 12 位（`sk-mem-hXG0****`），与 MEMORY-P0.md 的现场写法一致。"""
    if not key:
        return ""
    return f"{key[:12]}****" if len(key) > 16 else "****"


def key_shape_ok(key: str) -> bool:
    """key 能不能安全穿过「sh 变量 + sed 替换 + yaml 值」三关（不在此放宽）。"""
    return bool(key) and bool(_KEY_SHAPE_RE.fullmatch(key))


def user_id_for(slug: str) -> str:
    """幂等的根：确定性 user_id。改这个函数 = 换一次幂等判定，所有存量用户失配。"""
    return f"dshctl-{slug}"


def credential_name_for(slug: str) -> str:
    """业务 key 在 credentials 里的行名（kind=memory_user_key）。"""
    return f"dshctl-{slug}"


def resolve_proxy_base(settings: Any, host_name: str = "",
                       host_ssh_host: str = "") -> str:
    """这台主机到 Proxy 的出口前缀（不带 /v1，proxy 的 anthropic 转发自拼 /v1/messages）。

    命中顺序：memory_host_proxy[hosts.name] → [hosts.ssh_host] → memory_proxy_url。
    为什么必须有按主机的出口（MEMORY-P0 现场）：记忆栈在 192.0.2.0/24 这类内网段，
    云上机器拨不到内网地址，得走自家的 frp 隧道口——同一条 baseURL 写不进所有机器。
    """
    table = getattr(settings, "memory_host_proxy", None) or {}
    for key in (host_name, host_ssh_host):
        if key:
            hit = str(table.get(str(key)) or "").strip()
            if hit:
                return hit.rstrip("/")
    return str(getattr(settings, "memory_proxy_url", "") or "").strip().rstrip("/")


def memory_base_url(settings: Any, host_name: str = "",
                    host_ssh_host: str = "") -> str:
    """实例的生效 baseURL：出口前缀 + space（U-1 定案：全家一个 space，默认 default）。"""
    base = resolve_proxy_base(settings, host_name, host_ssh_host)
    if not base:
        return ""
    space = str(getattr(settings, "memory_space_id", "") or "default").strip() or "default"
    return f"{base}/{space}"


# --------------------------------------------------------------- 容器侧脚本（纯函数，可单测）

def script_patch_state(dsh_home: str) -> str:
    """只读探针：home 层与 refs 的接线状态。输出三行固定前缀，编排层解析。"""
    f = shlex.quote(f"{dsh_home}/cordis.patch.yml")
    g = shlex.quote(f"{dsh_home}/.credentials.yaml")
    return "\n".join([
        (f"[ -f {f} ] && grep -qF {shlex.quote(MEMORY_MARKER)} {f} 2>/dev/null "
        f"&& echo '@@MEM@@ yes' || echo '@@MEM@@ no'"),
        (f"[ -f {f} ] && grep -qF {shlex.quote(P0_MARKER)} {f} 2>/dev/null "
        f"&& echo '@@MEM_P0@@ yes' || echo '@@MEM_P0@@ no'"),
        (f"[ -f {g} ] && grep -q '^  {REFS_KEY_NAME}:' {g} 2>/dev/null "
        f"&& echo '@@REFS@@ yes' || echo '@@REFS@@ no'"),
    ])


def parse_patch_state(stdout: str) -> dict[str, bool]:
    out = {"memory": False, "p0": False, "refs": False}
    for line in (stdout or "").splitlines():
        ln = line.strip()
        if ln.startswith("@@MEM@@"):
            out["memory"] = ln.endswith("yes")
        elif ln.startswith("@@MEM_P0@@"):
            out["p0"] = ln.endswith("yes")
        elif ln.startswith("@@REFS@@"):
            out["refs"] = ln.endswith("yes")
    return out


def script_refs_merge(dsh_home: str) -> str:
    """把业务 key **合并**写进 .credentials.yaml 的 refs: 段（600），key 从 stdin 读。

    红线：这文件本是 DSH 的凭据库（`records:` 段是她的登录票据与别的 refs 键），
    **绝不整写覆盖**——P0 四台实例就是这么跑活的（MEMORY-P0.md）。这里只：
    删我们这一个键的历史行 → refs: 段在就把新行插到段头后，不在就在文件尾建段
    → 落盘 chmod 600。幂等：重跑只是把同一行删了再写回，别的键零改动。
    """
    f = shlex.quote(f"{dsh_home}/.credentials.yaml")
    return "\n".join([
        "set -e",
        f"f={f}",
        "umask 077",
        '[ -f "$f" ] || : > "$f"',
        "read -r user_key",   # stdin 只此一行：不进 argv、不进目标机 ps、不进日志
        f"grep -v '^  {REFS_KEY_NAME}:' \"$f\" > \"$f.dshc\" || true",
        "if grep -q '^refs:' \"$f.dshc\"; then",
        # 只替换第一个 refs: 段头（GNU sed 的空 RE 复用上一条 /^refs:/）
        "  sed \"0,/^refs:/s//refs:\\\\n  " + REFS_KEY_NAME + ": $user_key/\" "
        "\"$f.dshc\" > \"$f.dshc.n\"",
        "else",
        (f"  {{ cat \"$f.dshc\"; printf '\\nrefs:\\n  {REFS_KEY_NAME}: %s\\n' "
         "\"$user_key\"; } > \"$f.dshc.n\""),
        "fi",
        'grep -q "^  ' + REFS_KEY_NAME + ': " "$f.dshc.n" '
        f'|| {{ echo "[mem] 合并写失败（refs.{REFS_KEY_NAME} 没落进去）"; exit 1; }}',
        'mv "$f.dshc.n" "$f"',
        'rm -f "$f.dshc"',
        'chmod 600 "$f"',
        f"echo '[mem] refs.{REFS_KEY_NAME} 已合并写入（600，值不回显）'",
    ])


def _home_patch_lay(f: str, base_url: str, model_id: str, display_name: str) -> list[str]:
    """铺段共用体：heredoc 正文 + 追加 + 报事实。"""
    body = "\n".join([
        "- id: llm-deepseek",
        '  name: "@deepseek-ai/dsh-llm-deepseek-api-key"',
        "  disabled: false",
        "  config:",
        f"    apiKeyEnv: {REFS_KEY_NAME}",
        f"    baseURL: {base_url}",
        "    reasoningEffort: xhigh",
        "    models:",
        f"      - id: {model_id}",
        f"        name: {display_name}",
        # 窗口/上限/推理档与 kit 现值对齐（P0 接线当日实测值）
        "        contextWindow: 262144",
        "        maxTokens: 32768",
        "- id: agent-default-model",
        "  config:",
        "    provider: deepseek-official",
        f"    model: {model_id}",
        "    reasoningEffort: xhigh",
        MEMORY_MARKER_END,
    ])
    return [
        f"{{ echo; echo {shlex.quote(MEMORY_MARKER)}; cat <<'DSHC_MEM_EOF'",
        body,
        "DSHC_MEM_EOF",
        "} >> \"$f\"",
        "echo '[mem] PATCH_WIRED'",
        "wc -c \"$f\"",
    ]


def script_home_patch(dsh_home: str, base_url: str, *, model_id: str,
                      display_name: str = "家庭模型（经记忆层）") -> str:
    """home 层补丁：模型出口指向 Proxy。见新记号就短路（幂等）；见 P0 旧记号也短路
    并报 P0_LEGACY——兼容判据在编排层，脚本只报事实。接管走 script_home_takeover。

    补丁正文照 P0 四台实机逐字对齐（2026-10-29 从 dsh-02 容器读回）：
    llm-deepseek 启用 + apiKeyEnv 指 refs 键 + baseURL 不带 /v1；
    agent-default-model 切到 provider deepseek-official。
    """
    f = shlex.quote(f"{dsh_home}/cordis.patch.yml")
    return "\n".join([
        "set -e",
        f"f={f}",
        (f"if [ -f \"$f\" ] && grep -qF {shlex.quote(MEMORY_MARKER)} \"$f\"; then "
         "echo '[mem] PATCH_ALREADY'; exit 0; fi"),
        (f"if [ -f \"$f\" ] && grep -qF {shlex.quote(P0_MARKER)} \"$f\"; then "
         "echo '[mem] PATCH_P0_LEGACY'; exit 0; fi"),
        *_home_patch_lay(f, base_url, model_id, display_name),
    ])


def script_home_takeover(dsh_home: str, base_url: str, *, model_id: str,
                         display_name: str = "家庭模型（经记忆层）") -> str:
    """接管（详情页点「接管启用」专用）：把手写 P0 段换成控制台段。

    边界口径：从 P0 记号行起，到下一条顶格 `# dsh-control` 记号行之前（没有就
    到文件尾）。切出来的旧段必须含 P0 段特征（apiKeyEnv/baseURL 两样），不符就
    PATCH_TAKEOVER_UNSAFE 拒绝动手——宁可拒，不切错别人的段。切完走与
    script_home_patch 完全相同的铺段正文（单源，不出现两种段）。
    """
    f = shlex.quote(f"{dsh_home}/cordis.patch.yml")
    p0 = shlex.quote(P0_MARKER)
    return "\n".join([
        "set -e",
        f"f={f}",
        ("if [ ! -f \"$f\" ]; then echo '[mem] TAKEOVER_NOFILE'; exit 0; fi"),
        (f"if grep -qF {shlex.quote(MEMORY_MARKER)} \"$f\"; then "
         "echo '[mem] PATCH_ALREADY'; exit 0; fi"),
        (f"if ! grep -qF {p0} \"$f\"; then echo '[mem] TAKEOVER_NO_P0'; exit 0; fi"),
        (f"n=$(grep -nF {p0} \"$f\" | head -1 | cut -d: -f1); "
         "next=$(awk -v n=\"$n\" 'NR>n && /^# dsh-control/{print NR; exit}' \"$f\"); "
         "last=$([ -n \"$next\" ] && echo $((next-1)) || echo '$'); "
         "seg=$(sed -n \"${n},${last}p\" \"$f\"); "),
        ("printf %s \"$seg\" | grep -q apiKeyEnv && printf %s \"$seg\" | grep -q baseURL "
         "|| { echo '[mem] PATCH_TAKEOVER_UNSAFE'; exit 0; }"),
        "sed -i \"${n},${last}d\" \"$f\"",
        "echo '[mem] TAKEOVER_P0_REMOVED'",
        *_home_patch_lay(f, base_url, model_id, display_name),
    ])


def script_home_strip(dsh_home: str) -> str:
    """摘除容器接线（停用的容器侧）：整段删 home 层补丁 + 删 refs 键，输出事实行。

    - 控制台铺的段：按「MEMORY_MARKER 行 → MEMORY_MARKER_END 行」整段删（awk 精确
      匹配整行，绝不吞后面的段——先例：capabilities 的 marker 契约）。
    - P0 手工段（无段尾记号）：只有「从记号行到文件尾只剩我们那两条 - id:」才敢照
      P0 撤法删尾；段后面还压着别的内容就留着不碰，只报 PATCH_P0_LEGACY——
      宁可少删（refs 键已删，记忆通路已断），也绝不删错别人的段。
    - refs：只删我们这一个键的行，records: 与别的 refs 键原样。
    """
    f = shlex.quote(f"{dsh_home}/cordis.patch.yml")
    g = shlex.quote(f"{dsh_home}/.credentials.yaml")
    m = shlex.quote(MEMORY_MARKER)
    e = shlex.quote(MEMORY_MARKER_END)
    return "\n".join([
        "set -e",
        f"f={f}",
        f"g={g}",
        "changed=no",
        ("if [ ! -f \"$f\" ]; then echo '@@STRIP@@ patch=absent';"
        " else"),
        (f"  if grep -qxF {m} \"$f\" && grep -qxF {e} \"$f\"; then"),
        (f"    awk -v m={m} -v e={e} '$0==m{{s=1}} !s{{print}} $0==e{{s=0}}' "
        "\"$f\" > \"$f.dshc\"; mv \"$f.dshc\" \"$f\"; changed=yes; "
        "echo '@@STRIP@@ patch=removed';"),
        "  elif grep -qF " + shlex.quote(P0_MARKER) + " \"$f\"; then",
        (f"    n=$(grep -n {shlex.quote(P0_MARKER)} \"$f\" | head -1 | cut -d: -f1); "
        "tail_sec=$(sed -n \"${n},\\$p\" \"$f\"); "
        "others=$(printf %s \"$tail_sec\" | grep '^- id:' | "
        "grep -vc '^- id: \\(llm-deepseek\\|agent-default-model\\)$' || true); "
        "if [ \"$others\" = \"0\" ]; then sed -i \"${n},\\$d\" \"$f\"; changed=yes; "
        "echo '@@STRIP@@ patch=p0_removed'; else echo '@@STRIP@@ PATCH_P0_LEGACY'; fi;"),
        "  else echo '@@STRIP@@ patch=absent'; fi",
        "fi",
        "if [ -f \"$g\" ] && grep -q '^  " + REFS_KEY_NAME + ":' \"$g\"; then",
        (f"  umask 077; grep -v '^  {REFS_KEY_NAME}:' \"$g\" > \"$g.dshc\"; "
        "mv \"$g.dshc\" \"$g\"; chmod 600 \"$g\"; changed=yes; "
        f"echo '@@STRIP@@ refs=removed';"),
        "else echo '@@STRIP@@ refs=absent'; fi",
        "if [ \"$changed\" = yes ]; then echo '@@STRIP@@ RESTART_NEEDED'; fi",
    ])


# ------------------------------------------------------------------ 适配器

class MemoryHub:
    """core `/v3/meta/*` 的最小客户端：建用户（幂等）、发/回收 key、verify、探活。

    实现 MemoryHubLike Protocol；`base_url` 是 core（默认 :8420），不是 Panel。
    """

    manual_required = False

    def __init__(self, *, core_url: str, admin_key: str,
                 service_id: str = "default",
                 client: httpx.AsyncClient | None = None,
                 timeout: float = 15.0,
                 dry_run: bool = False,
                 recorder: DryRunRecorder | None = None) -> None:
        if not core_url:
            raise IntegrationError("记忆中枢 core 地址未配置（[memory].core_url）",
                                   code="no_config")
        if not admin_key:
            raise IntegrationError("记忆中枢 admin key 为空"
                                   "（credentials kind=memory_admin_key）",
                                   code="no_config")
        self.base_url = core_url.rstrip("/")
        self._admin_key = admin_key
        self.service_id = service_id or "default"
        self.dry_run = dry_run
        self.recorder = recorder
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url,
                                                   timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------- 传输原语

    async def _meta(self, path: str, payload: dict[str, Any], *,
                    key: str | None = None, write: bool = False,
                    action: str = "") -> dict[str, Any]:
        """POST /v3/meta<path>，返回信封里的 data；失败抛 IntegrationError。

        错误 message 前缀（`duplicate_entry` 这类）是稳定机器码，留在 .code 上
        供调用方分支；原文进 .detail（admin key 不在报文里，detail 可安全展示）。
        """
        url = f"/v3/meta{path}"
        if write and self.dry_run:
            if self.recorder is not None:
                self.recorder.record("memory", action or path, method="POST",
                                     target=self.base_url + url, payload=payload,
                                     note="dry-run: 未发送")
            return {}
        headers = {"x-tdai-service-id": self.service_id}
        if self._admin_key:
            headers["x-tdai-user-key"] = key if key is not None else self._admin_key
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise IntegrationError(
                f"记忆中枢 core 不可达（{self.base_url}{url}，{exc.__class__.__name__}）："
                "确认 tdai-memory-core 容器在跑、core_url 配的是控制台够得着的地址；"
                "恢复后重跑本步即可（幂等）", code="core_unreachable") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise IntegrationError(
                f"记忆中枢 {url} 返回非 JSON（HTTP {resp.status_code}）",
                code="bad_json") from exc
        code = data.get("code")
        message = str(data.get("message") or "")
        if resp.status_code != 200 or code != 0:
            err = message.split(":", 1)[0].strip().lower() if message else ""
            raise IntegrationError(
                f"记忆中枢 {url} 失败：HTTP {resp.status_code}"
                f"{(' ' + message) if message else ''}",
                code=err or str(code or resp.status_code), detail=message)
        return data.get("data") or {}

    # ---------------------------------------------------------- 用户生命周期

    async def ensure_instance_user(self, slug: str, *, team_id: str = "",
                                   agent_name: str = "") -> dict[str, Any]:
        """建（或认出）这台实例的业务用户，返回 {user_id, user_key, created, team}。

        幂等全押在确定性 user_id 上（core 同名不去重）：
        - 新建：`user/create` 直接带 default_user_key，一步到位；
        - 已存在（409 duplicate_entry）：老 key 的明文控制台可能根本没有
          （P0 时代手工建的），此时经 `user-key/create` 补发一把新 key——
          老 key 不回收，两条腿都能用，调用方拿新 key 往容器写。
        team_id 有值就拉进 Team；`member_already_exists` 视为成功。
        """
        uid = user_id_for(slug)
        created = True
        user_key = ""
        # 先查后建（2026-10-29 现网实证）：core 对重名回 HTTP 500 internal_error
        # 而不是 409 duplicate——靠解析错误形状会把「已存在」当故障炸出来。
        # user/get 存在回用户体、不存在回 404 user_not_found，两态干净，判据用它。
        exists = False
        try:
            got = await self._meta("/user/get", {"user_id": uid})
            exists = str(got.get("user_id") or "") == uid
        except IntegrationError as exc:
            if exc.code != "user_not_found":
                raise
        if exists:
            created = False
        else:
            try:
                data = await self._meta("/user/create",
                                        {"username": uid, "user_id": uid},
                                        write=True, action="user/create")
                user_key = str(data.get("default_user_key") or "")
                if self.dry_run:
                    user_key = ""
            except IntegrationError as exc:
                # 竞态兜底：查后被人抢建，core 只会回 500——认已存在前先确认人真在
                if "duplicate" not in exc.code and "duplicate" not in exc.detail.lower() \
                        and "internal_error" not in exc.code:
                    raise
                created = False
                got = await self._meta("/user/get", {"user_id": uid})
                if str(got.get("user_id") or "") != uid:
                    raise IntegrationError(
                        f"记忆中枢里 {uid} 建前查重后仍读不到（竞态或存储异常），"
                        "重跑本步即可", code="user_gone") from exc
        if not user_key:
            k = await self._meta("/user-key/create",
                                 {"user_id": uid, "name": "dsh-control"},
                                 write=True, action="user-key/create")
            user_key = str(k.get("key_value") or "")
            if self.dry_run:
                return {"user_id": uid, "user_key": "", "created": created,
                        "team": "dry-run", "dry_run": True}
        if not user_key:
            raise IntegrationError(
                "记忆中枢没回 key（user/create 与 user-key/create 都没给 key_value）："
                "检查 core 的 key_limit 限制后重跑本步（幂等）", code="no_key")
        team = "skipped"
        if team_id:
            try:
                await self._meta("/team-member/add",
                                 {"team_id": team_id, "user_id": uid,
                                  "role": "member"},
                                 write=True, action="team-member/add")
                team = "added"
            except IntegrationError as exc:
                if "member_already_exists" in exc.code or \
                        "member_already_exists" in exc.detail:
                    team = "already"
                else:
                    # 拉群失败不毁接线：detail 里说破，页面上看得见
                    team = f"failed: {exc.detail or exc.code}"
        # Agent 实体对齐（2026-10-29）：P0 时代每台实例在 Panel 里有个 owner 是
        # 业务用户的 Agent（资产按 Agent 隔离的载体），控制台新开通漏建了——
        # dsh-07 接线成功却在 Agents 管理里缺席。建用户顺手续 Agent，幂等。
        agent = "skipped"
        if team_id and not self.dry_run:
            try:
                lst = await self._meta("/agent/list", {"team_id": team_id,
                                                       "page": 1, "page_size": 50})
                mine = [it for it in (lst.get("items") or [])
                        if str(it.get("owner_user_id") or "") == uid]
                if mine:
                    agent = "exists"
                else:
                    await self._meta("/agent/create", {
                        "team_id": team_id, "owner_user_id": uid,
                        "name": agent_name or f"{uid}（dsh-control）",
                        "description": "dsh-control 自动接线",
                        "visibility": "team"},
                        write=True, action="agent/create")
                    agent = "created"
            except IntegrationError as exc:
                # 与拉群同口径：Agent 没建成不毁接线，事实里说破
                agent = f"failed: {exc.detail or exc.code}"
        return {"user_id": uid, "user_key": user_key, "created": created,
                "team": team, "agent": agent}

    async def verify_key(self, user_key: str) -> dict[str, Any]:
        """auth/verify：判据是 data.valid，非法 key 也回 200/code=0（不能按 401 分支）。"""
        if not user_key:
            return {"valid": False, "user_id": ""}
        data = await self._meta("/auth/verify", {"user_key": user_key})
        user = data.get("user") or {}
        return {"valid": bool(data.get("valid")),
                "user_id": str(user.get("user_id") or "")}

    async def list_user_keys(self, user_id: str) -> list[dict[str, Any]]:
        data = await self._meta("/user-key/list",
                                {"user_id": user_id, "limit": 100, "offset": 0})
        return list(data.get("items") or [])

    async def revoke_user_keys(self, user_id: str) -> dict[str, Any]:
        """停用＝回收：吊销该用户全部 active key（U-7 口径；记忆数据一律不动）。

        逐把 revoke，单把失败不中断其余；`last_key_cannot_revoke`（core 保护：
        不允许用户一把 key 都不剩）如实带回来，由编排层决定说辞——绝不静默成功。
        """
        keys = await self.list_user_keys(user_id)
        revoked: list[str] = []
        failed: list[dict[str, str]] = []
        for item in keys:
            if str(item.get("status") or "") != "active":
                continue
            kid = str(item.get("key_id") or "")
            if not kid:
                continue
            try:
                await self._meta("/user-key/revoke", {"key_id": kid},
                                 write=True, action="user-key/revoke")
                revoked.append(kid)
            except IntegrationError as exc:
                failed.append({"key_id": kid, "why": exc.detail or exc.code})
        return {"user_id": user_id, "revoked": revoked, "failed": failed,
                "active_left": len(keys) - len(revoked) - len(failed)
                if not failed else None}

    async def delete_user(self, user_id: str) -> dict[str, Any]:
        """回收用户（/v3/meta/user/delete，admin 批量接口的单人用法）。

        core 侧级联行为以文档为准；调用方负责先 revoke 再删并处理台账行。
        失败原样抛 IntegrationError，由编排层决定说辞。
        """
        return await self._meta("/user/delete", {"user_ids": [user_id]},
                                write=True, action="user/delete")

    # ---------------------------------------------------------- 探活

    async def core_health(self, *, timeout: float = 5.0) -> bool:
        """GET /health（非 v3、无鉴权，core 自带）。任何异常都是 False，不抛。"""
        try:
            resp = await self._client.get("/health", timeout=timeout)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


async def probe_tcp(url: str, *, timeout: float = 4.0) -> bool:
    """对 Proxy 出口前缀做 TCP 探活（host:port 取自 URL）。够得着=True。"""
    match = re.match(r"^[a-z][a-z0-9+.-]*://([^/]+)", (url or "").strip())
    if not match:
        return False
    hostport = match.group(1)
    host, _, port_s = hostport.rpartition(":")
    if not host or not port_s.isdigit():
        return False
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port_s)), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001, S110 —— 收尾关不上不算探活失败
            pass
        return True
    except (OSError, TimeoutError):
        return False


class NoopMemoryHub:
    """记忆层的 noop（与 integrations/noop.py 同德性：manual_required=True）。

    编排层按 `manual_required` 把步骤标 skipped 并把 `reason` 打进详情与警告事件；
    真被调用到写动作时抛 IntegrationError，不静默假成功。
    """

    manual_required = True

    def __init__(self, reason: str = "") -> None:
        self.reason = reason or "记忆层未启用"

    async def aclose(self) -> None:
        return None

    async def ensure_instance_user(self, slug: str, *, team_id: str = "",
                                   agent_name: str = "") -> dict[str, Any]:
        raise IntegrationError(f"{self.reason}（要人工：在 Hub 里手工建 "
                               f"{user_id_for(slug)} 并把 key 登记进凭据）",
                               code="memory_noop")

    async def verify_key(self, user_key: str) -> dict[str, Any]:
        return {"valid": False, "user_id": "", "reason": self.reason}

    async def revoke_user_keys(self, user_id: str) -> dict[str, Any]:
        raise IntegrationError(f"{self.reason}（要人工：在 Hub 里回收 {user_id} 的 key）",
                               code="memory_noop")

    async def delete_user(self, user_id: str) -> dict[str, Any]:
        raise IntegrationError(f"{self.reason}（要人工：在 Hub 里删除 {user_id}）",
                               code="memory_noop")

    async def core_health(self, *, timeout: float = 5.0) -> bool:
        return False


# ---------------------------------------------------------------- 装配

# ---------------------------------------------------------------- admin 身份

# admin key 的持有人缓存（TTL 300s）。Panel 的「我的 team」列表是拿会话里的
# user_id 去 core 查的（2026-10-29 现网实证）：嵌入应答若回占位名
# dshctl-console，选择器就永远「团队（0）」——必须回 admin key 的真实主体。
_ADMIN_ID_TTL = 300.0
_admin_id_cache: dict[str, tuple[float, str]] = {}


async def admin_identity(hub: Any) -> str:
    """admin key 对应的真实 user_id（auth/verify 用 admin key 照自己）。

    查不到回空串：调用方决定回落形状，这里不炸——身份问不出来不该把
    免登会话挡死，最差也就是 Panel 里团队列表暂时空着。
    """
    key = getattr(hub, "_admin_key", "") or ""
    if not key:
        return ""
    now = time.monotonic()
    hit = _admin_id_cache.get(key)
    if hit and now - hit[0] < _ADMIN_ID_TTL:
        return hit[1]
    try:
        v = await hub.verify_key(key)
    except (IntegrationError, httpx.HTTPError):   # 栈挂/信封错都算问不出来
        return ""
    uid = str(v.get("user_id") or "") if v.get("valid") else ""
    if uid:
        _admin_id_cache[key] = (now, uid)
    return uid


async def build_memory_hub(s: Any, settings: Any,
                           *, recorder: DryRunRecorder | None = None) -> tuple[Any, str]:
    """按站点配置与凭据装配适配器。返回 (hub|None, 缺料原因)。

    缺料三态（开通第 68 步据此标 skipped）：开关关 / 地址缺 / admin key 没登记。
    这里只判装配，栈通不通交给 core_health/probe_tcp——「配了但栈挂」也是缺料，
    由调用方合并处理（vendor §4 第 3 条：记忆没铺成不挡开通）。
    """
    from ..db import repo

    if not getattr(settings, "memory_enabled", False):
        return None, ("站点没启用记忆中枢（site.toml 无 [memory] 段或 enabled=false）："
                      "本次跳过记忆接线，实例其他功能不受影响")
    core_url = str(getattr(settings, "memory_core_url", "") or "").strip()
    if not core_url:
        return None, ("memory_enabled=true 但 [memory].core_url 为空："
                      "本次跳过记忆接线。补 site.toml 的 [memory].core_url "
                      "（记忆栈 core 的地址）后重跑开通即可")
    if not resolve_proxy_base(settings):
        return None, ("memory_enabled=true 但 [memory].proxy_url 为空："
                      "本次跳过记忆接线。补 [memory].proxy_url"
                      "（Proxy 出口前缀，不带 /v1）后重跑开通即可")
    cred = await repo.get_credential_by_name(s, _ADMIN_CRED_NAME)
    if cred is None or cred.kind != "memory_admin_key":
        cred = await repo.get_credential_by_kind(s, "memory_admin_key")
    admin_key = str(((cred.secret or {}) if cred else {}).get("user_key") or "").strip()
    if not admin_key:
        return None, ("credentials 里没有 kind=memory_admin_key 的 admin key"
                      f"（建议 name={_ADMIN_CRED_NAME}）：本次跳过记忆接线。"
                      "在 site.toml 加一条 [[credentials]] "
                      "kind=\"memory_admin_key\" 后经 cli seed 灌入即可")
    hub = MemoryHub(core_url=core_url, admin_key=admin_key,
                    dry_run=bool(getattr(settings, "dry_run", False)),
                    recorder=recorder)
    return hub, ""
