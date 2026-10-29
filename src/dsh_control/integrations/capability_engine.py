"""能力引擎（ADR-0001 决策二/三）：manifest 解释、命令拼装、探针回读、兼容闸门。

边界（决策一）：控制台是唯一动手的人和唯一记账的人，能力包只是「货」。
本模块**不认识任何具体能力**——它只解释 manifest 声明的动作，经 SSH `docker exec`
打进目标容器执行并回读。新能力一律以 manifest 表达，为某个能力新写一段 shell
直接驳回。

三条执行口径：

1. **包文件的搬运走 kit 换装通道**（决策五）：第 20 步已把 `capabilities/` 整棵
   铺到被管机 `kit/` 下，apply 时用 `docker exec -i … < 主机文件` 从**主机侧**
   流进容器——不把正文 base64 塞进命令行（speckit 一包 200KB，argv 单参数
   128KB 上限当场爆）。密钥仍走 stdin，不进 argv（硬规则第 3 条）。
2. **幂等**：patch 类见到 marker 就跳过，与 `jobs/capabilities.py` 现有
   `TEAM_MARKER` 语义逐字兼容（P3 双跑对账的依据）；升级按「marker 在但版本戳
   不符」重铺（摘段再追加），改 marker 文本必须升 version。
3. **观测只认探针**：status/healthcheck 是 manifest 声明的一行命令，退出码 0
   即「装着」；探针命令在目标机不存在（exit 127）记 `health = "unknown"` 并
   说明，不静默判健康。

skill 类的 `files` 落位规则（v1 固定，货与引擎两头都按这个走）：
- 不含 `/` 的条目 → `$DSH_HOME/skills/<id>/<条目>`（comfy-image 先例）；
- 含 `/` 的条目 → `$DSH_HOME/skills/<条目>`（speckit 十个技能各自成目录：DSH 的
  技能发现只扫一层 `<root>/<dir>/SKILL.md`，2026-10-29 读 dsh-skill-filesystem
  源码确认，嵌套目录不会被发现）。

版本戳统一落 `$DSH_HOME/.dshctl-capabilities/<id>/version`（一行版本号，形态照
`kit/.dshctl-kit-version`）；禁用状态记号是同目录的 `disabled` 文件——disable
必须可逆，uninstall 才把整目录连戳一起删。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .base import ExecResult

# ---------------------------------------------------------------- 发布阈值（U-3 定案）

MAX_PACK_FILE_BYTES = 2 << 20        # 单文件 > 2MB → 制品库
MAX_PACK_TOTAL_BYTES = 8 << 20       # 整包 > 8MB → 制品库
_ARTIFACT_HINT = ("走制品库文件账本（fontlib 同款：cli fontlib publish / §6.1.5），"
                  "manifest 里只留引用；随 kit 的 JSONB 整包不收")

CAPABILITY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION_SAFE_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
# patch/plugin 的 marker 必须以此开头：按 marker 摘段（awk）靠它判断「下一段开始了」
MARKER_PREFIX = "# dsh-control:"

# ---------------------------------------------------------------- manifest 结构（决策二）

KINDS_IMPLEMENTED = ("skill", "patch", "plugin", "service")
KIND_ASSET = "asset"                       # U-5：只留解析位，不实现执行
KINDS_ALL = KINDS_IMPLEMENTED + (KIND_ASSET,)

_COMMON_REQUIRED = ("id", "version", "kind", "title", "params", "compatible_dsh",
                    "marker", "status", "healthcheck", "uninstall")
_COMMON_TYPES: dict[str, tuple[type, ...]] = {
    "id": (str,), "version": (str,), "kind": (str,), "title": (str,),
    "params": (dict,), "compatible_dsh": (str,), "marker": (str,),
    "status": (str,), "healthcheck": (str,), "uninstall": (str,),
    "restart_required": (bool,), "default_on": (bool,),
}
# default_on 是本实现在契约外补的一个可选布尔（speckit「默认开」的载体，
# 站点没配开关时按它落 desired=present）——回报 lead，评审认可后补进 ADR 字段表。
_KIND_REQUIRED: dict[str, tuple[str, ...]] = {
    "skill": ("files",),
    "patch": ("source", "append"),
    "plugin": ("packages", "enable_entries", "laid_by"),
    "service": ("compose", "external_api", "wiring"),
    # asset（U-5）：文件投递语义——target 目录 + files；P2 只校验形状
    "asset": ("files", "target"),
}
_KIND_TYPES: dict[str, dict[str, tuple[type, ...]]] = {
    "skill": {"files": (list,), "config_template": (str,), "secret_target": (dict,)},
    "patch": {"source": (dict,), "append": (str,)},
    "plugin": {"packages": (list,), "enable_entries": (list,), "laid_by": (str,)},
    "service": {"compose": (str,), "external_api": (str,), "wiring": (dict,)},
    "asset": {"files": (list,), "target": (str,)},
}
_PARAM_TYPES = ("string", "integer", "boolean")
_ON_MISSING = ("skipped", "failed")
_INTERP_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
# 密钥会经 eval 展开（见 _render_config_env），这些字符会改变语法形状，拒收
_SECRET_UNSAFE_RE = re.compile(r"[\"'`$\\\n\r]")


class ManifestError(RuntimeError):
    """发布校验不过：未知字段、超阈值、结构不对——kit publish / capability publish 当场拒。"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("；".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- semver（U-2 定案的比对口径）

_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$")
_CMP_RE = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(\S+)$")


def parse_version(text: str) -> tuple[int, int, int, tuple] | None:
    """semver 2.0 解析；预发布号按规范拆段（数字段按数值比、字母段按 ASCII 比）。

    读不出就是 None——闸门对 None 一律拦（宁可不升，不静默升）。"""
    m = _SEMVER_RE.match((text or "").strip().lstrip("v"))
    if not m:
        return None
    pre: tuple = ()
    raw = m.group("pre")
    if raw is not None:
        parts = []
        for piece in raw.split("."):
            parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
        pre = tuple(parts)
    return int(m.group("major")), int(m.group("minor")), int(m.group("patch")), pre


def compare_versions(a: str, b: str) -> int:
    """semver 2.0 §11 排序：主/次/修订逐段比；同核心时预发布 < 正式。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        raise ValueError(f"版本号不合法（semver）: {a!r} / {b!r}")
    core_a, core_b = pa[:3], pb[:3]
    if core_a != core_b:
        return -1 if core_a < core_b else 1
    if pa[3] == pb[3]:
        return 0
    if not pa[3]:
        return 1          # 正式版 > 预发布版
    if not pb[3]:
        return -1
    for x, y in zip(pa[3], pb[3]):
        if x != y:
            return -1 if x < y else 1
    return -1 if len(pa[3]) < len(pb[3]) else 1


def satisfies(version: str, spec: str) -> bool:
    """semver 区间比对（ADR 示例只用 `>=x`；逗号/空格分隔为合取；支持 ^ ~）。

    ^0.x 按 npm 语义：^0.2.0-rc.1 = >=0.2.0-rc.1,<0.3.0；~ 锁 major.minor。
    spec 为空 → 不设限（发布校验必填，这里只兜历史空值）。"""
    spec = (spec or "").strip()
    if not spec:
        return True
    pv = parse_version(version)
    if pv is None:
        raise ValueError(f"当前版本号不合法（semver）: {version!r}")
    for piece in re.split(r"[,\s]+", spec):
        if not piece:
            continue
        m = _CMP_RE.match(piece)
        if not m:
            raise ValueError(f"兼容区间无法解析: {piece!r}")
        op, bound = m.group(1) or "=", m.group(2)
        bv = parse_version(bound)
        if bv is None:
            raise ValueError(f"兼容区间里的版本号不合法: {piece!r}")
        c = compare_versions(version, bound)
        ok = {" >=": c >= 0, ">=": c >= 0, ">": c > 0, "<=": c <= 0,
              "<": c < 0, "=": c == 0}.get(op, c >= 0)
        if not ok:
            return False
        if op in ("^", "~"):
            # 上界：^ 主版本不变（0.x 时次版本不变）；~ 主+次都不变
            if op == "^" and bv[0] > 0:
                upper = (bv[0] + 1, 0, 0)
            elif op == "^":
                upper = (0, bv[1] + 1, 0)
            else:
                upper = (bv[0], bv[1] + 1, 0)
            if pv[:3] >= upper:
                return False
    return True


# ---------------------------------------------------------------- manifest 解析与发布校验

def parse_manifest_text(text: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError([f"TOML 解析失败: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ManifestError(["manifest 顶层必须是表"])
    return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v)


def validate_manifest(man: Mapping[str, Any], *,
                      files: Mapping[str, bytes] | None = None) -> list[str]:
    """返回错误列表（空 = 通过）。宁可不装，不要半装：未知字段、没声明的插值、
    超阈值全部在这里拦死，不等到铺到一半才发现。"""
    errs: list[str] = []
    kind = man.get("kind")
    if not isinstance(kind, str) or kind not in KINDS_ALL:
        errs.append(f"kind 必须是 {'|'.join(KINDS_ALL)}，实为 {kind!r}")

    allowed = set(_COMMON_TYPES) | set(_KIND_TYPES.get(kind if isinstance(kind, str) else "", {}))
    for key in man:
        if key not in allowed:
            errs.append(f"未知字段 {key!r}（v1 不认的字段发布时就拒，不发明契约外的键）")
    for key in _COMMON_REQUIRED:
        if key not in man:
            errs.append(f"缺必填字段 {key!r}")
    for key, types in _COMMON_TYPES.items():
        if key in man and not isinstance(man[key], types):
            errs.append(f"字段 {key!r} 类型应为 {types}，实为 {type(man[key]).__name__}")

    cid = man.get("id", "")
    if isinstance(cid, str) and cid and not CAPABILITY_ID_RE.fullmatch(cid):
        errs.append(f"id 命名只认 [a-z0-9-]（不以 - 开头结尾、≤63 字符）: {cid!r}")
    ver = man.get("version", "")
    if isinstance(ver, str) and ver and not _VERSION_SAFE_RE.fullmatch(ver):
        errs.append(f"version 只认 [A-Za-z0-9._+-] ≤64 字符: {ver!r}")
    cdsh = man.get("compatible_dsh", "")
    if isinstance(cdsh, str) and cdsh:
        try:
            satisfies("0.0.0", cdsh)          # 语法自检：区间本身要能解析
        except ValueError as exc:
            errs.append(f"compatible_dsh 区间非法: {exc}")
    if man.get("uninstall") not in (None, "reverse"):
        errs.append(f"uninstall v1 只允许 \"reverse\"（卸载=安装的反向），实为 {man.get('uninstall')!r}")
    marker = man.get("marker", "")
    if (isinstance(marker, str) and marker and kind in ("patch", "plugin")
            and not marker.startswith(MARKER_PREFIX)):
        errs.append(f"{kind} 的 marker 必须以 {MARKER_PREFIX!r} 开头（按 marker 摘段靠它分段）: {marker!r}")

    params = man.get("params")
    declared: set[str] = set()
    if isinstance(params, dict):
        om = params.get("on_missing", "failed")
        if om not in _ON_MISSING:
            errs.append(f"params.on_missing 只认 skipped|failed，实为 {om!r}")
        for name, schema in params.items():
            if name == "on_missing":
                continue
            if not isinstance(schema, dict):
                errs.append(f"params.{name} 必须是表（type/required/secret）")
                continue
            ptype = schema.get("type")
            if ptype not in _PARAM_TYPES:
                errs.append(f"params.{name}.type 只认 {'|'.join(_PARAM_TYPES)}，实为 {ptype!r}")
            for flag in ("required", "secret"):
                if flag in schema and not isinstance(schema[flag], bool):
                    errs.append(f"params.{name}.{flag} 必须是布尔")
            declared.add(str(name))
    elif params is not None:
        errs.append("params 必须是表")

    if isinstance(kind, str) and kind in _KIND_REQUIRED:
        for key in _KIND_REQUIRED[kind]:
            if key not in man:
                errs.append(f"kind={kind} 缺专属字段 {key!r}")
        for key, types in _KIND_TYPES[kind].items():
            if key in man and not isinstance(man[key], types):
                errs.append(f"kind={kind} 的 {key!r} 类型应为 {types}，"
                            f"实为 {type(man[key]).__name__}")
        if kind == "plugin" and man.get("laid_by") not in (None, "entrypoint", "console"):
            errs.append("plugin.laid_by 只认 entrypoint|console（U-4 定案 v1 只有 entrypoint 动手）")
        if kind == "skill":
            for rel in man.get("files") or []:
                if not isinstance(rel, str) or not rel or rel.startswith("/") \
                        or ".." in rel.split("/"):
                    errs.append(f"skill.files 条目必须是不带 .. 的相对路径: {rel!r}")
            if isinstance(man.get("secret_target"), dict) \
                    and not man["secret_target"].get("file"):
                errs.append("secret_target 缺 file 键")

    # 插值只允许引用 params 里声明过的参数；${DSH_HOME} 这类容器内环境变量原样透传
    for text in _iter_strings({k: v for k, v in man.items() if k != "params"}):
        for name in _INTERP_RE.findall(text):
            if name not in declared:
                errs.append("插值 {{" + name + "}} 未在 params 里声明（按未知字段拒）")

    if files is not None:
        errs.extend(check_pack_sizes(files))
        if kind == "skill":
            for rel in man.get("files") or []:
                if isinstance(rel, str) and rel not in files:
                    errs.append(f"skill.files 声明了 {rel!r}，包里没有这个文件")
            tmpl = man.get("config_template")
            if isinstance(tmpl, str) and tmpl and tmpl not in files:
                errs.append(f"config_template 指向 {tmpl!r}，包里没有这个文件")
        if kind == "service" and isinstance(man.get("compose"), str) \
                and man["compose"] and man["compose"] not in files:
            errs.append(f"compose 片段 {man['compose']!r} 包里没有")
    return errs


def check_pack_sizes(files: Mapping[str, bytes]) -> list[str]:
    """U-3 定案的阈值：单文件 > 2MB 或整包 > 8MB 直接拒收，提示走制品库。"""
    errs: list[str] = []
    total = 0
    for rel, data in files.items():
        size = len(data)
        total += size
        if size > MAX_PACK_FILE_BYTES:
            errs.append(f"{rel}: 单文件 {size} 字节 > {MAX_PACK_FILE_BYTES} 字节——{_ARTIFACT_HINT}")
    if total > MAX_PACK_TOTAL_BYTES:
        errs.append(f"整包 {total} 字节 > {MAX_PACK_TOTAL_BYTES} 字节——{_ARTIFACT_HINT}")
    return errs


_JUNK_SUFFIX = ("__pycache__", ".pyc", ".swp", "~")


def _is_junk(name: str) -> bool:
    return any(name.endswith(s) for s in _JUNK_SUFFIX)


def discover_packs(files: Mapping[str, bytes]) -> dict[str, dict[str, bytes]]:
    """从一批「路径 → 正文」里挑出能力包：`capabilities/<id>/manifest.toml` 所在
    目录就是一包，键返回 `capabilities/<id>` 前缀。kit 发布与目录发布共用这条缝。"""
    packs: dict[str, dict[str, bytes]] = {}
    for rel in files:
        parts = rel.split("/")
        if len(parts) == 2 and parts[0] == "capabilities" and parts[1] == "manifest.toml":
            packs[f"{parts[0]}/{parts[1].rsplit('.', 1)[0]}"] = {}
        if len(parts) >= 2 and parts[0] == "capabilities":
            packs.setdefault(f"capabilities/{parts[1]}", {})
    for rel, data in files.items():
        if _is_junk(rel):
            continue
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "capabilities":
            packs[f"capabilities/{parts[1]}"][rel[len(f"capabilities/{parts[1]}") + 1:]] = data
    return {k: v for k, v in packs.items() if "manifest.toml" in v}


def load_pack_dir(path: str) -> dict[str, bytes]:
    """读一棵包目录（含 manifest.toml）→ {相对路径: 正文}。"""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not _is_junk(d)]
        for name in filenames:
            if _is_junk(name):
                continue
            ap = os.path.join(dirpath, name)
            with open(ap, "rb") as fh:
                out[os.path.relpath(ap, path).replace("\\", "/")] = fh.read()
    return out


def packs_from_files(files: Mapping[str, bytes]) -> list[ParsedPack]:
    """一批文件（kit 全集或单包）→ 校验通过的 ParsedPack 列表；任何一包不合都拒。"""
    out: list[ParsedPack] = []
    for prefix, sub in sorted(discover_packs(files).items()):
        raw_manifest = sub.get("manifest.toml")
        if raw_manifest is None:
            continue
        man = parse_manifest_text(raw_manifest.decode("utf-8"))
        errs = validate_manifest(man, files=sub)
        if errs:
            raise ManifestError([f"{prefix}: {e}" for e in errs])
        out.append(ParsedPack(
            capability_id=str(man["id"]), version=str(man["version"]),
            kind=str(man["kind"]), title=str(man.get("title") or man["id"]),
            manifest=man, manifest_sha256=sha256_hex(raw_manifest), files=sub))
    return out


def packs_from_dir(root: str) -> list[ParsedPack]:
    """`--dir` 既可以给 kit 仓库根（认 `capabilities/` 子树），也可以直接给一棵包。"""
    kit_sub = os.path.join(root, "capabilities")
    if os.path.isdir(kit_sub):
        files = {f"capabilities/{rel}": data
                 for rel, data in load_pack_dir(kit_sub).items()}
        return packs_from_files(files)
    return packs_from_files({f"capabilities/x/{rel}": data
                             for rel, data in load_pack_dir(root).items()})


@dataclass(slots=True)
class ParsedPack:
    capability_id: str
    version: str
    kind: str
    title: str
    manifest: dict[str, Any]
    manifest_sha256: str
    files: dict[str, bytes] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------- 目录写入（kit publish 同刷）

async def upsert_catalog(s, packs: list[ParsedPack], *, kit_version: str = "",
                         note: str = "", commit: bool = True) -> list[Any]:
    """同 id+version 重发 = manifest 正文就地覆盖并升 current；同一 id 的其它行
    demote（换行法同 kit_releases.add）。commit=False 留给调用方拼同一事务。"""
    from sqlalchemy import select, update

    from ..db.models import Capability
    rows = []
    for pack in packs:
        row = await s.scalar(select(Capability).where(
            Capability.capability_id == pack.capability_id,
            Capability.version == pack.version))
        params_schema = pack.manifest.get("params") or {}
        if row is None:
            row = Capability(capability_id=pack.capability_id, version=pack.version)
            s.add(row)
        row.kind = pack.kind
        row.title = pack.title
        row.manifest = pack.manifest
        row.manifest_sha256 = pack.manifest_sha256
        if kit_version:
            row.kit_version = kit_version
        row.compatible_dsh = str(pack.manifest.get("compatible_dsh") or "")
        row.on_missing = str(params_schema.get("on_missing") or "failed")
        if note:
            row.note = note
        row.state = "published"
        row.current = True
        await s.flush()
        await s.execute(update(Capability)
                        .where(Capability.capability_id == pack.capability_id,
                               Capability.id != row.id)
                        .values(current=False))
        rows.append(row)
    if commit:
        await s.commit()
    return rows


# ---------------------------------------------------------------- 站点默认（speckit「默认开」）

def site_desires(manifest: Mapping[str, Any], site_secret: Mapping[str, Any] | None) -> bool:
    """站点期望：site.toml 的开关镜像（credentials kind=site 的 secret）优先——
    认 `capabilities = {<id> = true|false}` 表或 `<id>_enabled` 键；都没有时回
    manifest 的 default_on（speckit 包声明 true，即「站点默认 desired=present」）。"""
    cid = str(manifest.get("id") or "")
    sec = site_secret or {}
    caps = sec.get("capabilities")
    if isinstance(caps, dict) and cid in caps:
        return bool(caps[cid])
    key = f"{cid}_enabled"
    if key in sec:
        return bool(sec[key])
    return bool(manifest.get("default_on", False))


async def provision_default_ledger(s, instance_id: int,
                                   site_secret: Mapping[str, Any] | None) -> list[str]:
    """给新实例铺「站点默认该开」的台账行（desired=present，版本跟随 current）。
    开通第 66 步挂点调它；已有行不动——实例级可关就靠不覆盖。"""
    from sqlalchemy import select

    from ..db.models import Capability, InstanceCapability
    created: list[str] = []
    rows = (await s.scalars(select(Capability).where(
        Capability.current.is_(True), Capability.state == "published"))).all()
    for cap in rows:
        if not site_desires(cap.manifest or {}, site_secret):
            continue
        exists = await s.scalar(select(InstanceCapability).where(
            InstanceCapability.instance_id == instance_id,
            InstanceCapability.capability_id == cap.capability_id))
        if exists is not None:
            continue
        s.add(InstanceCapability(instance_id=instance_id,
                                 capability_id=cap.capability_id,
                                 desired_state="present", desired_version=""))
        created.append(cap.capability_id)
    if created:
        await s.commit()
    return created


# ---------------------------------------------------------------- 执行通道

SshFn = Callable[..., Awaitable[ExecResult]]


def make_ssh(executor, ep) -> SshFn:
    """给 CLI 用：把 Executor 包成引擎要的 ssh callable（形状同 JobCtx.ssh）。"""
    async def _ssh(command: str, *, timeout: float = 300, stdin: str | None = None,
                   quiet: bool = True) -> ExecResult:
        return await executor.run(host=ep.host, port=ep.port, user=ep.user,
                                  private_key_pem=ep.private_key_pem, command=command,
                                  timeout=timeout, stdin=stdin)
    return _ssh


async def read_dsh_home(ssh: SshFn, container: str) -> str:
    """$DSH_HOME 取生效值（照 jobs/capabilities._data_home 口径），取不到不猜。"""
    r = await ssh(f"docker exec {shlex.quote(container)} sh -c "
                  + shlex.quote('printf %s "${DSH_HOME:-/data}"'), timeout=60)
    return (r.stdout or "").strip().rstrip("/") or "/data"


async def _docker_sh(ssh: SshFn, container: str, script: str, *,
                     stdin: str | None = None, timeout: float = 180) -> ExecResult:
    """一条命令打进容器里跑（stdin 只用来递密钥）。"""
    q = shlex.quote(container)
    flag = "-i " if stdin is not None else ""
    return await ssh(f"docker exec {flag}{q} sh -c {shlex.quote(script)}",
                     timeout=timeout, stdin=stdin, quiet=True)


async def _host_sh(ssh: SshFn, script: str, *, stdin: str | None = None,
                   timeout: float = 240) -> ExecResult:
    """一条脚本在**被管主机**上跑（从主机往容器里灌文件用这条）。"""
    return await ssh(f"sh -c {shlex.quote(script)}", timeout=timeout, stdin=stdin,
                     quiet=True)


async def read_dsh_version(runtime, container: str) -> tuple[str, str]:
    """现读 dsh 版本（U-2 定案）：复用 read_dsh_config 同一条 runtime 通道，
    `docker exec <c> dsh --version`。返回值必须带 source；台账 instances.dsh_version
    只做展示，闸门不信它（换镜像后可能滞后一轮）。"""
    try:
        raw = await runtime.version(container)
    except Exception:  # noqa: BLE001 —— 读不到是闸门要报告的事实，不是异常出口
        return "", "docker-exec dsh --version: 读取失败"
    line = (raw or "").strip().splitlines()[0].strip() if raw else ""
    m = re.search(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", line)
    if not m:
        return "", "docker-exec dsh --version: 输出不含版本号"
    return m.group(0), "docker-exec dsh --version"


# ---------------------------------------------------------------- 落位与参数

def stamp_dir(home: str, capability_id: str) -> str:
    return f"{home}/.dshctl-capabilities/{capability_id}"


def skill_targets(man: Mapping[str, Any], home: str) -> list[tuple[str, str]]:
    """[(包内相对路径, 容器内目标路径)]。落位规则见模块 docstring。"""
    cid = str(man["id"])
    out: list[tuple[str, str]] = []
    for rel in man.get("files") or []:
        rel = str(rel)
        target = f"{home}/skills/{cid}/{rel}" if "/" not in rel else f"{home}/skills/{rel}"
        out.append((rel, target))
    return out


def _skill_top_dirs(man: Mapping[str, Any], home: str) -> list[str]:
    """卸载/禁用要动的 skills/ 下第一层目录，去重保序（禁用即把它整体移走）。"""
    cid = str(man["id"])
    dirs: list[str] = []
    for rel, _target in skill_targets(man, home):
        first = rel.split("/")[0] if "/" in rel else cid
        d = f"{home}/skills/{first}"
        if d not in dirs:
            dirs.append(d)
    return dirs


def _seg_strip(marker: str) -> str:
    """按 marker 摘除 home 层里我们那段：从 marker 行起跳过，直到下一条
    `# dsh-control` 开头的行（别人的段/别的能力段）或 EOF。终止式不带冒号是
    有意的：memory_hub 的 `# dsh-control-memory` 与 P0 旧记号都没冒号，带冒号
    会把排在后面的记忆段一起摘掉（P3 收编评审发现，2026-10-29）。awk 实现，
    POSIX 够用。"""
    return ("awk -v m=" + shlex.quote(marker) +
            " 'index($0, m)==1 {s=1; next} /^# dsh-control/ {s=0} !s' "
            '"$f" > "$f.dshc.tmp" && mv "$f.dshc.tmp" "$f"')


def render(text: str, values: Mapping[str, Any]) -> str:
    """`{{param}}` 插值（只渲染已给值的；没给的保留原文——发布校验已保证键都声明过）。"""
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in values:
            return str(values[name])
        return m.group(0)
    return _INTERP_RE.sub(sub, text or "")


@dataclass(slots=True)
class ResolvedParams:
    values: dict[str, Any] = field(default_factory=dict)      # 非敏感
    secrets: dict[str, str] = field(default_factory=dict)     # 声明序即 stdin 行序
    missing: list[str] = field(default_factory=list)          # 必填但没值


def resolve_params(man: Mapping[str, Any], given: Mapping[str, Any],
                   secrets: Mapping[str, str]) -> ResolvedParams:
    """按 manifest 的 params schema 取值、校验、分流（secret 走 stdin）。
    参数不合 schema 抛 ManifestError——按决策三那是 failed，不能静默凑。"""
    out = ResolvedParams()
    for name, schema in (man.get("params") or {}).items():
        if name == "on_missing" or not isinstance(schema, dict):
            continue
        src = secrets if schema.get("secret") else given
        raw = src.get(name, None)
        if raw is None:
            if schema.get("required"):
                out.missing.append(str(name))
            continue
        ptype = schema.get("type")
        try:
            if ptype == "integer":
                val: Any = int(str(raw))
            elif ptype == "boolean":
                val = str(raw).strip().lower() in ("1", "true", "yes", "on")
            else:
                val = str(raw)
        except ValueError as exc:
            raise ManifestError([f"参数 {name}={raw!r} 不合 schema（{ptype}）: {exc}"]) from exc
        if schema.get("secret") and _SECRET_UNSAFE_RE.search(str(raw)):
            raise ManifestError([(
                f"参数 {name} 的值含引号/反引号/$/反斜杠/换行，"
                "容器内渲染用 eval 展开会破坏语法，拒收")])
        (out.secrets if schema.get("secret") else out.values)[str(name)] = val
    return out


def _secret_order(man: Mapping[str, Any]) -> list[str]:
    return [str(k) for k, v in (man.get("params") or {}).items()
            if k != "on_missing" and isinstance(v, dict) and v.get("secret")]


def _stdin_of(resolved: ResolvedParams, order: list[str]) -> str:
    return "".join(f"{resolved.secrets.get(name, '')}\n" for name in order)


# ---------------------------------------------------------------- 动作（plan / apply / …）

@dataclass(slots=True)
class CapResult:
    state: str            # succeeded | skipped | failed（决策三：不造退出码）
    detail: str = ""
    reason: str = ""
    planned: list[str] = field(default_factory=list)


def _pack_dir_on_host(kit_dir: str, capability_id: str) -> str:
    return f"{kit_dir.rstrip('/')}/capabilities/{capability_id}"


async def run_action(ssh: SshFn, container: str, man: Mapping[str, Any],
                     home: str, action: str, resolved: ResolvedParams, *,
                     kit_dir: str = "", version: str = "",
                     dry_run: bool = False) -> CapResult:
    """装/卸/启用/禁用/重铺的执行体（kind 分发）。返回三态 + detail；
    幂等短路算 succeeded（决策三第一行），缺料按 on_missing 定 skipped/failed。"""
    kind = str(man.get("kind") or "")
    if action not in ("install", "apply", "enable", "disable", "uninstall"):
        return CapResult("failed", reason=f"未知动作 {action!r}")
    if kind == KIND_ASSET:
        return CapResult("failed", reason="asset 类（U-5）P2 只留解析位，铺法 P3 实装")
    if action == "apply" and kind == "skill":
        # apply 只补文件不动启用状态；被 disable 的能力由工单层挡（respect_disabled）
        pass
    on_missing = str((man.get("params") or {}).get("on_missing") or "failed")

    plan = _plan_lines(man, action, home, kit_dir, version)
    if dry_run:
        return CapResult("succeeded", detail="dry-run：一个字节都不写", planned=plan)

    if resolved.missing and action not in ("uninstall", "disable"):
        msg = ("缺参数（manifest 声明的必填项没取到）：" + "、".join(resolved.missing)
               + f"；on_missing={on_missing}")
        return CapResult("skipped" if on_missing == "skipped" else "failed",
                         reason=msg, detail=msg)

    if kind == "skill":
        return await _action_skill(ssh, container, man, home, action, resolved,
                                   kit_dir=kit_dir, version=version, on_missing=on_missing)
    if kind == "patch":
        return await _action_patch(ssh, container, man, home, action, resolved,
                                   version=version, on_missing=on_missing)
    if kind == "plugin":
        return await _action_plugin(ssh, container, man, home, action, resolved,
                                    version=version)
    return await _action_service(ssh, container, man, home, action, resolved,
                                 kit_dir=kit_dir, version=version, on_missing=on_missing)


def _plan_lines(man: Mapping[str, Any], action: str, home: str, kit_dir: str,
                version: str) -> list[str]:
    cid = str(man["id"])
    kind = str(man.get("kind"))
    lines = [f"动作 {action}：{cid}@{version or man.get('version')}（kind={kind}）"]
    if kind == "skill":
        for rel, target in skill_targets(man, home):
            lines.append(f"would-copy {kit_dir}/capabilities/{cid}/{rel} → {target}"
                         "（依据 manifest.files）")
        lines.append(f"would-stamp {stamp_dir(home, cid)}/version = {version}")
    else:
        lines.append(f"would-edit {home}/cordis.patch.yml 中 marker 段：{man.get('marker')!r}")
    if action == "uninstall":
        lines.append("卸载 = 安装的反向（uninstall=\"reverse\"）")
    return lines


async def _write_stamp(ssh: SshFn, container: str, home: str, cid: str, version: str,
                       *, remove: bool = False, disabled: bool | None = None) -> None:
    d = shlex.quote(stamp_dir(home, cid))
    if remove:
        await _docker_sh(ssh, container, f"rm -rf {d}", timeout=60)
        return
    lines = ["set -e", f"mkdir -p {d}"]
    if version:
        lines.append(f"printf '%s\\n' {shlex.quote(version)} > {d}/version")
    if disabled is True:
        lines.append(f"printf '%s\\n' 1 > {d}/disabled")
    elif disabled is False:
        lines.append(f"rm -f {d}/disabled")
    await _docker_sh(ssh, container, "\n".join(lines), timeout=60)


# ---------------------------------------------------------------- skill 类

async def _pack_present(ssh: SshFn, kit_dir: str, cid: str) -> bool:
    """包文件在不在**被管机的 kit 目录**里（第 20 步换装的产物）。"""
    if not kit_dir:
        return False
    p = shlex.quote(f"{_pack_dir_on_host(kit_dir, cid)}/manifest.toml")
    r = await ssh(f"[ -f {p} ] && echo yes || echo no", timeout=60, quiet=True)
    return "yes" in (r.stdout or "")


async def _action_skill(ssh: SshFn, container: str, man: Mapping[str, Any], home: str,
                        action: str, resolved: ResolvedParams, *, kit_dir: str,
                        version: str, on_missing: str) -> CapResult:
    cid = str(man["id"])
    targets = skill_targets(man, home)
    tops = _skill_top_dirs(man, home)
    qc = shlex.quote(container)

    if action == "uninstall":
        script = ("set -e; "
                  + "; ".join(f"rm -rf {shlex.quote(d)}" for d in tops)
                  + f"; rm -rf {shlex.quote(stamp_dir(home, cid))}")
        r = await _docker_sh(ssh, container, script, timeout=120)
        if not r.ok:
            return CapResult("failed", reason=f"卸载失败: {r.tail()}")
        return CapResult("succeeded",
                         detail=f"已删 {len(tops)} 个技能目录与版本戳（uninstall=reverse）")
    if action == "disable":
        scratch = f"{stamp_dir(home, cid)}/disabled-dir"
        moves = " ; ".join(
            f"if [ -d {shlex.quote(d)} ]; then mv {shlex.quote(d)} "
            f"{shlex.quote(scratch)}/{shlex.quote(d.rsplit('/', 1)[-1])}; fi"
            for d in tops)
        script = f"set -e; mkdir -p {shlex.quote(scratch)}; {moves}"
        r = await _docker_sh(ssh, container, script, timeout=120)
        if not r.ok:
            return CapResult("failed", reason=f"禁用失败: {r.tail()}")
        await _write_stamp(ssh, container, home, cid, "", disabled=True)
        return CapResult("succeeded", detail="技能目录已移入 scratch（可逆）；"
                                             "重启型能力不追着重启，待自然重启生效（U-6）")
    if action == "enable":
        scratch = f"{stamp_dir(home, cid)}/disabled-dir"
        moves = " ; ".join(
            f"if [ -d {shlex.quote(scratch)}/{shlex.quote(d.rsplit('/', 1)[-1])} ]; then "
            f"mv {shlex.quote(scratch)}/{shlex.quote(d.rsplit('/', 1)[-1])} {shlex.quote(d)}; fi"
            for d in tops)
        r = await _docker_sh(ssh, container, "set -e; " + (moves or "true"), timeout=120)
        if not r.ok:
            return CapResult("failed", reason=f"启用失败: {r.tail()}")
        await _write_stamp(ssh, container, home, cid, "", disabled=False)
        return CapResult("succeeded", detail="技能目录已移回 skills/（可逆）")

    # install / apply：包必须在被管机 kit 目录里（决策五：第 20 步换装到货）
    if not await _pack_present(ssh, kit_dir, cid):
        msg = (f"包不在被管机 {_pack_dir_on_host(kit_dir, cid)}/（kit 未换装到货？）"
               f"；on_missing={on_missing}")
        return CapResult("skipped" if on_missing == "skipped" else "failed",
                         reason=msg, detail=msg)

    pack = _pack_dir_on_host(kit_dir, cid)
    lines = ["set -e"]
    dirs = sorted({t.rsplit("/", 1)[0] for _, t in targets})
    if dirs:
        lines.append(f"docker exec {qc} sh -c "
                     + shlex.quote("mkdir -p " + " ".join(shlex.quote(d) for d in dirs)))
    # 内层引号用「双引号包单引号」一层：三层套 `'\''` 转义人眼与回归都读不动。
    for rel, target in targets:
        lines.append(f'docker exec -i {qc} sh -c "cat > {shlex.quote(target)}" < '
                     + shlex.quote(f"{pack}/{rel}"))
    tmpl = str(man.get("config_template") or "")
    if tmpl:
        # 模板先原样进容器（正文不进 argv），密钥稍后经 stdin 在容器内展开
        tmp_target = f"{home}/.dshctl-cap-tmp"
        lines.append(f'docker exec -i {qc} sh -c "cat > {shlex.quote(tmp_target)}" < '
                     + shlex.quote(f"{pack}/{tmpl}"))
    lines.append("echo SKILL_APPLIED")
    r = await _host_sh(ssh, "\n".join(lines))
    if not r.ok:
        return CapResult("failed", reason=f"铺设失败: {r.tail()}")

    if tmpl:
        st = man.get("secret_target") or {}
        fname = str(st.get("file") or "config.env")
        mode = str(st.get("mode") or "0600")
        mode = mode[-3:] if len(mode) >= 3 else mode
        order = _secret_order(man)
        rr = await _render_config_env(ssh, container, home, cid, fname, tmpl, order, resolved)
        if not rr.ok:
            return CapResult("failed", reason=f"config 渲染失败: {rr.tail()}")
        await _docker_sh(ssh, container,
                         f"chmod {mode} {shlex.quote(f'{home}/skills/{cid}/{fname}')}",
                         timeout=60)

    await _write_stamp(ssh, container, home, cid, version or str(man.get("version") or ""))
    names = " ".join(rel for rel, _ in targets)
    return CapResult("succeeded",
                     detail=f"已铺 {len(targets)} 个文件到 {home}/skills/（{names}），版本戳已更新")


async def _render_config_env(ssh: SshFn, container: str, home: str, cid: str,
                             fname: str, tmpl: str, order: list[str],
                             resolved: ResolvedParams):
    """config.env 渲染：非敏感 `{{param}}` 在控制台侧渲好后经 sed 换成 shell 变量
    引用，密钥从 stdin 读进变量、容器内 eval 展开——**密钥全程不进 argv**。
    约束：模板每行是 `KEY=VALUE` 形态（comfy 先例形状）；密钥值不含引号/$
    （resolve_params 已把关）。"""
    tmp = shlex.quote(f"{home}/.dshctl-cap-tmp")
    target = shlex.quote(f"{home}/skills/{cid}/{fname}")
    reads = "; ".join(f"read -r __dshc_s{i + 1}" for i in range(len(order)))
    # 模板里的密钥占位先改成变量引用，再逐行 eval 展开（无外部命令、argv 无密钥）
    seds = " ".join(
        "-e " + shlex.quote("s|{{" + name + "}}|${__dshc_s" + str(i + 1) + "}|g")
        for i, name in enumerate(order))
    script = ("set -e; umask 077; " + (reads + "; " if reads else "")
              + (f"sed {seds} {tmp} | " if order else f"cat {tmp} | ")
              + "{ while IFS= read -r line; do eval \"printf '%s\\\\n' \\\"$line\\\"\"; done"
                f" ; }} > {target}; rm -f {tmp}")
    return await _docker_sh(ssh, container, script,
                            stdin=_stdin_of(resolved, order), timeout=120)


# ---------------------------------------------------------------- patch 类

async def _action_patch(ssh: SshFn, container: str, man: Mapping[str, Any], home: str,
                        action: str, resolved: ResolvedParams, *, version: str,
                        on_missing: str) -> CapResult:
    """home 层补丁（cordis.patch.yml）：marker 段幂等——与 jobs/capabilities 的
    TEAM_MARKER 先例逐字同判（见到 marker 就算铺过）。"""
    cid = str(man["id"])
    marker = str(man.get("marker") or "")
    patch_file = f"{home}/cordis.patch.yml"
    f, mq = shlex.quote(patch_file), shlex.quote(marker)

    if action in ("uninstall", "disable"):
        script = "\n".join([
            "set -e",
            f"f={f}",
            "[ -f \"$f\" ] || { echo NOSEG; exit 0; }",
            f"grep -qF {mq} \"$f\" || {{ echo NOSEG; exit 0; }}",
            _seg_strip(marker),
        ])
        r = await _docker_sh(ssh, container, script, timeout=120)
        if not r.ok:
            return CapResult("failed", reason=f"摘段失败: {r.tail()}")
        if action == "disable":
            await _write_stamp(ssh, container, home, cid, "", disabled=True)
            return CapResult("succeeded", detail="home 层 marker 段已摘除（可逆：enable 重铺）")
        await _write_stamp(ssh, container, home, cid, "", remove=True)
        return CapResult("succeeded", detail="home 层 marker 段与版本戳已摘除（reverse）")

    # 正文：bundle 来源在容器里顺着 dsh 可执行文件解析（agent-team 先例的路子，
    # 正文不抄进我们仓库）；append 段渲染好后 base64 内联（不含密钥）。
    src = man.get("source") or {}
    append = render(str(man.get("append") or ""), resolved.values)
    ver = version or str(man.get("version") or "")
    stamp = shlex.quote(f"{stamp_dir(home, cid)}/version")
    body_cmds: list[str] = []
    if isinstance(src, dict) and src.get("bundle"):
        bundle = re.sub(r"[^A-Za-z0-9@/._-]", "", str(src["bundle"]))
        sfile = re.sub(r"[^A-Za-z0-9._-]", "", str(src.get("file") or "cordis.patch.yml"))
        body_cmds += [
            'bin="$(readlink -f "$(command -v dsh)")"',
            'pkg="$(CDPATH= cd -- "$(dirname "$bin")/.." && pwd)"',
            f"B=$(find \"$pkg\" -path '*{bundle}/{sfile}' 2>/dev/null | head -1)",
            "export B",
            '[ -n "$B" ] || { echo SRC_MISSING; exit 0; }',
            'cat "$B"',
        ]
    if append:
        b64 = base64.b64encode(("\n" + append.rstrip("\n") + "\n").encode("utf-8")).decode("ascii")
        body_cmds.append(f"printf %s {shlex.quote(b64)} | base64 -d")
    if not body_cmds:
        msg = f"补丁正文是空的（source 与 append 都没给）；on_missing={on_missing}"
        return CapResult("skipped" if on_missing == "skipped" else "failed",
                         reason=msg, detail=msg)

    script = "\n".join([
        "set -e",
        f"f={f}",
        # bundle 先解析：找不到就整个动作原样退出（不摘旧段，不把现场弄坏）
        *(body_cmds[:5] if isinstance(src, dict) and src.get("bundle") else []),
        # 幂等：见到 marker 就算铺过；版本戳一致就短路（TEAM_MARKER 逐字兼容）
        f"if [ -f \"$f\" ] && grep -qF {mq} \"$f\"; then "
        f"v=$(cat {stamp} 2>/dev/null || true); "
        f"[ \"$v\" = {shlex.quote(ver)} ] && {{ echo ALREADY; exit 0; }}; "
        + _seg_strip(marker) + "; fi",
        'mkdir -p "$(dirname "$f")"',
        "{ echo; echo " + mq + "; " + "; ".join(body_cmds) + "; } >> \"$f\"",
        (f"mkdir -p {shlex.quote(stamp_dir(home, cid))}; "
         f"printf '%s\\n' {shlex.quote(ver)} > {stamp}"),
        "echo PATCH_APPLIED",
    ])
    r = await _docker_sh(ssh, container, script, timeout=180)
    if not r.ok:
        return CapResult("failed", reason=f"补丁铺设失败: {r.tail()}")
    out = r.stdout or ""
    if "SRC_MISSING" in out:
        msg = (f"镜像里找不到官方组合包（{src.get('bundle')}）——跳过并说明，不猜路径"
               f"；on_missing={on_missing}")
        return CapResult("skipped" if on_missing == "skipped" else "failed",
                         reason=msg, detail=msg)
    if "ALREADY" in out:
        return CapResult("succeeded", detail="home 层已铺过（marker 在、版本一致），跳过（幂等）")
    if action == "enable":
        await _write_stamp(ssh, container, home, cid, "", disabled=False)
    return CapResult("succeeded", detail=f"已把 marker 段写入 {patch_file}（重启后生效）")


# ---------------------------------------------------------------- plugin 类

async def _action_plugin(ssh: SshFn, container: str, man: Mapping[str, Any], home: str,
                         action: str, resolved: ResolvedParams, *, version: str) -> CapResult:
    """plugin 类（U-4 定案 laid_by=entrypoint）：**只量不动手**——包体由镜像
    entrypoint 启动时铺，控制台只管 home 层的启用条目。"""
    if man.get("laid_by") == "console":
        return CapResult("failed",
                         reason="plugin/laid_by=console 未实现（U-4 定案不给 plugin 类提供 console 铺法）")
    cid = str(man["id"])
    marker = str(man.get("marker") or "")
    patch_file = f"{home}/cordis.patch.yml"
    f, mq = shlex.quote(patch_file), shlex.quote(marker)

    if action == "uninstall":
        script = "\n".join(["set -e", f"f={f}",
                            f"[ -f \"$f\" ] && grep -qF {mq} \"$f\" || {{ echo NOSEG; exit 0; }}",
                            _seg_strip(marker)])
        r = await _docker_sh(ssh, container, script, timeout=120)
        if not r.ok:
            return CapResult("failed", reason=f"摘启用条目失败: {r.tail()}")
        await _write_stamp(ssh, container, home, cid, "", remove=True)
        return CapResult("succeeded",
                         detail="启用条目已摘；运行时副本归 entrypoint，控制台不动（U-4）")

    disable = action == "disable"
    seg_lines: list[str] = []
    for e in man.get("enable_entries") or []:
        if not isinstance(e, dict):
            continue
        seg_lines.append(f"- id: {e.get('id')}")
        if e.get("name"):
            seg_lines.append(f"  name: \"{render(str(e['name']), resolved.values)}\"")
        if disable:
            seg_lines.append("  disabled: true")
    seg = ("\n".join(seg_lines) + "\n").encode("utf-8")
    b64 = base64.b64encode(seg).decode("ascii")
    ver = version or str(man.get("version") or "")
    stamp = shlex.quote(f"{stamp_dir(home, cid)}/version")
    script = "\n".join([
        "set -e", f"f={f}",
        f"if [ -f \"$f\" ] && grep -qF {mq} \"$f\"; then " + _seg_strip(marker) + "; fi",
        'mkdir -p "$(dirname "$f")"',
        f"{{ echo {mq}; printf %s {shlex.quote(b64)} | base64 -d; }} >> \"$f\"",
        (f"mkdir -p {shlex.quote(stamp_dir(home, cid))}; "
         f"printf '%s\\n' {shlex.quote(ver)} > {stamp}"),
        f"printf '1\\n' > {stamp}/disabled" if disable else f"rm -f {stamp}/disabled",
    ])
    r = await _docker_sh(ssh, container, script, timeout=120)
    if not r.ok:
        return CapResult("failed", reason=f"启用条目写入失败: {r.tail()}")
    note = ("已重写为 disabled:true（禁用）" if disable
            else "启用条目已写入/刷新（包体归 entrypoint，只动条目）")
    return CapResult("succeeded", detail=f"{note}；重启后生效，不追着重启（U-6）")


# ---------------------------------------------------------------- service 类

async def _action_service(ssh: SshFn, container: str, man: Mapping[str, Any], home: str,
                          action: str, resolved: ResolvedParams, *, kit_dir: str,
                          version: str, on_missing: str) -> CapResult:
    """service 类（v1 薄层）：主机级 compose 片段起停 + 实例 home 层接线段。
    external_api（如 memory_hub 适配器）的建用户/停用由工单编排层先行把密钥
    注入 stdin，引擎只认 manifest 与参数，不认识具体适配器。"""
    cid = str(man["id"])
    frag = str(man.get("compose") or "")
    kit_pack = _pack_dir_on_host(kit_dir, cid) if kit_dir else ""
    marker = str(man.get("marker") or f"{MARKER_PREFIX} {cid} 由控制台铺设")
    wiring = man.get("wiring") if isinstance(man.get("wiring"), dict) else {}
    seg = wiring.get("segment")
    if not seg and isinstance(wiring.get("patch"), dict):
        seg = f"- {wiring['patch'].get('key')}: {wiring['patch'].get('value')}"
    seg = render(str(seg or ""), resolved.values)
    sub = {**man, "marker": marker, "source": {}, "append": seg}

    detail = ""
    if frag and kit_pack and action in ("install", "apply", "uninstall"):
        fq = shlex.quote(f"{kit_pack}/{frag}")
        verb = "down --remove-orphans" if action == "uninstall" else "up -d"
        r = await ssh(f"[ -f {fq} ] && docker compose -f {fq} {verb}", timeout=600, quiet=True)
        if not r.ok and "no such file" not in (r.stderr or "").lower():
            if action != "uninstall" and r.code in (1, 127):
                msg = f"compose 片段 {kit_pack}/{frag} 起停失败: {r.tail()}"
                return CapResult("skipped" if on_missing == "skipped" else "failed",
                                 reason=msg, detail=msg)
            return CapResult("failed", reason=f"compose {verb} 失败: {r.tail()}")
        detail = f"compose 片段已 {verb.split()[0]}；" if r.ok else "compose 片段不在位，跳过主机侧；"

    inner = await _action_patch(ssh, container, sub, home, action, resolved,
                                version=version, on_missing=on_missing)
    return CapResult(inner.state, detail=detail + inner.detail, reason=inner.reason)


# ---------------------------------------------------------------- 探针（决策三：观测）

async def run_probe(ssh: SshFn, container: str, man: Mapping[str, Any], *,
                    home: str, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """status / 版本戳 / disabled / healthcheck 一次 SSH 往返采回。分段照
    DOCKER_FACTS_SNIPPET 的做法：容器侧只跑 POSIX sh，解析在我们这边。"""
    status = render(man.get("status") or "false", values or {})
    health = render(man.get("healthcheck") or "", values or {})
    stamp = shlex.quote(f"{stamp_dir(home, str(man['id']))}/version")
    disabled = shlex.quote(f"{stamp_dir(home, str(man['id']))}/disabled")
    script = "\n".join([
        'echo "@@STATUS@@"',
        "{ " + status + " ; } >/dev/null 2>&1; echo $?",
        'echo "@@STAMP@@"',
        f"cat {stamp} 2>/dev/null || true",
        'echo "@@DISABLED@@"',
        f"[ -f {disabled} ] && echo yes || echo no",
        'echo "@@HEALTH@@"',
        ("_o=$( { " + health + " ; } 2>&1 ); echo $?"
         "; printf '%s\\n' \"$_o\" | head -c 400" if health
         else "echo -1; echo 'manifest 无健康探针'"),
    ])
    r = await _docker_sh(ssh, container, script, timeout=90)
    sec: dict[str, list[str]] = {}
    cur = ""
    for line in (r.stdout or "").splitlines():
        key = line.strip().strip("@").lower()
        if key in ("status", "stamp", "disabled", "health") and line.startswith("@@"):
            cur = key
            sec.setdefault(cur, [])
        elif cur:
            sec[cur].append(line)

    def _rc(name: str, default: int = -1) -> int:
        try:
            return int((sec.get(name) or [""])[0].strip())
        except ValueError:
            return default

    installed = _rc("status", 1) == 0
    observed_version = "\n".join(sec.get("stamp") or []).strip()
    disabled_flag = "yes" in " ".join(sec.get("disabled") or [])
    hrc = _rc("health", -1)
    hdetail = "\n".join(sec.get("health") or [])[1:].strip()
    if not health:
        health_state, hdetail = "unknown", "manifest 无健康探针"
    elif not installed:
        health_state, hdetail = "unknown", "未装，健康探针不判"
    elif hrc == 0:
        health_state = "ok"
    elif hrc == 127:
        health_state = "unknown"
        hdetail = "探针命令在目标容器里不存在（slim 镜像缺命令）：" + hdetail[:200]
    else:
        health_state = "failed"
    return {
        "capability_id": str(man["id"]),
        "kind": str(man.get("kind") or ""),
        "installed": installed,
        "enabled": installed and not disabled_flag,
        "observed_version": observed_version,
        # marker_present/installed 同源于 status 探针（决策三）
        "marker_present": installed,
        "health": health_state,
        "health_detail": hdetail[:400],
        "reason": ("" if observed_version or not installed else
                   "已装但版本戳读不到（版本未知，按黄标不按漂移）"),
    }


# ---------------------------------------------------------------- 兼容闸门（决策五）

@dataclass(slots=True)
class GateResult:
    allowed: bool
    dsh_version: str
    dsh_version_source: str
    blockers: list[dict[str, Any]] = field(default_factory=list)
    checked: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "dsh_version": self.dsh_version,
                "dsh_version_source": self.dsh_version_source,
                "blockers": self.blockers, "checked": self.checked}


async def gate_compatible(s, *, instance_id: int, ssh: SshFn, runtime, container: str,
                          target_dsh_version: str = "") -> GateResult:
    """切镜像前的闸门：台账里所有 desired=present 的能力逐个比 compatible_dsh。

    版本现读口径（U-2 定案）：`target_dsh_version` 给了就用它（升级工单知道
    目标镜像的版本），source 注明 caller-specified；没给就经 runtime 现读
    （与 read_dsh_config 同一条通道上的 `dsh --version`）。**读不到就拦**——
    绝不静默升上去（file-download 的按钮会「安静地不画」，这类失败不让容器
    变红，只能靠闸门）。给 deploy.py 用：在切镜像那一步之前 await 它，
    `not result.allowed` 就 StepFailed，detail 用 result.as_dict()。
    """
    from sqlalchemy import select

    from ..db.models import Capability, InstanceCapability
    ledger = (await s.scalars(select(InstanceCapability).where(
        InstanceCapability.instance_id == instance_id,
        InstanceCapability.desired_state == "present"))).all()
    if not ledger:
        return GateResult(True, target_dsh_version,
                          "caller-specified（目标镜像）" if target_dsh_version
                          else "无 desired=present 能力，未判")
    if target_dsh_version:
        version, source = target_dsh_version, "caller-specified（目标镜像）"
    else:
        version, source = await read_dsh_version(runtime, container)
    if not version or parse_version(version) is None:
        return GateResult(False, version, source, [{
            "capability_id": "(dsh-version)", "version": "",
            "compatible_dsh": "",
            "reason": f"现读不到 DSH 版本（source={source}），闸门不放行——宁可不升，不静默升"}],
            len(ledger))
    blockers: list[dict[str, Any]] = []
    for row in ledger:
        want = row.desired_version
        if want:
            cap = await s.scalar(select(Capability).where(
                Capability.capability_id == row.capability_id,
                Capability.version == want))
        else:
            cap = await s.scalar(select(Capability).where(
                Capability.capability_id == row.capability_id,
                Capability.current.is_(True)))
        if cap is None or cap.state != "published":
            blockers.append({"capability_id": row.capability_id, "version": want,
                             "compatible_dsh": "",
                             "reason": "目录里没有现行版/指定版，闸门不放行"})
            continue
        try:
            ok = satisfies(version, cap.compatible_dsh or "")
        except ValueError as exc:
            return GateResult(False, version, source,
                              [{"capability_id": cap.capability_id, "version": cap.version,
                                "compatible_dsh": cap.compatible_dsh,
                                "reason": f"兼容区间判不了: {exc}"}], len(ledger))
        if not ok:
            blockers.append({"capability_id": cap.capability_id, "version": cap.version,
                             "compatible_dsh": cap.compatible_dsh,
                             "reason": f"目标版本 {version} 不在区间内——先禁用它再升"})
    return GateResult(not blockers, version, source, blockers, len(ledger))
