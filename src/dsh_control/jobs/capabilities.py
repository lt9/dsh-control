"""给实例铺「能力」：生图技能 + 智能体团队。

放这里而不是散在 deploy/restart 里，是因为这两件事既要在**开通**时做一次，也要在
**重启**时重做一遍——智能体团队走的是 DSH 的 home 层补丁（``$DSH_HOME/cordis.patch.yml``），
它是卷里的文件、entrypoint 不碰，但技能目录可能被人手抖删过，重铺一次是幂等的。

三条设计约束：

1. **不重建镜像**。官方把「智能体团队」做成默认关闭的实验组合包，开它只有两种办法：
   改镜像里的 profile 清单（要重新构建镜像、把没人审过的东西烙进去），或者写 home 层
   补丁（官方留给"给某个人开例外"的那一层，在数据卷里，重启不丢）。这里选后者。
2. **补丁正文不抄在我们仓库里**。运行时从镜像里读官方那份
   ``dsh-experimental-agent-team-profile/cordis.patch.yml``，DSH 升级了配置自动跟着变；
   我们只追加一条自己关心的成员数覆盖。
3. **密钥不进命令行**。技能正文可以走 base64 内联（里面没有秘密），token 一律 stdin。
"""

from __future__ import annotations

import base64
import shlex
from pathlib import Path

from ..db import repo
from .runner import JobCtx, StepFailed

SKILL_NAME = "comfy-image"
SKILL_FILES = ("SKILL.md", "comfy_generate.py")
_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME

# home 层补丁里我们的记号：见到它就认为已经铺过，不重复追加
TEAM_MARKER = "# dsh-control: 智能体团队（实验组合包）由控制台铺设"

_TEAM_BUNDLE = "@deepseek-ai/dsh-experimental-agent-team-profile"

_IMAGE_SKIP = ("站点没配生图服务（image_api_url 为空或没登记 image_key 密钥）："
               "本次跳过生图技能铺设，实例其他功能不受影响")


async def image_material(ctx: JobCtx) -> dict[str, str]:
    """生图服务地址在 settings，token 在 credentials(kind=image_key)。"""
    url = (ctx.settings.image_api_url or "").strip()
    cred = await repo.get_credential_by_kind(ctx.s, "image_key")
    token = str(((cred.secret or {}) if cred else {}).get("token") or "").strip()
    return {"url": url, "backup_url": (ctx.settings.image_backup_url or "").strip(),
            "token": token}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def skill_file(name: str) -> str:
    path = _SKILL_DIR / name
    if not path.is_file():  # pragma: no cover - 打包漏了文件属于安装问题
        raise StepFailed(f"控制台镜像里缺技能文件 {name}（打包漏了，不是实例的问题）")
    return path.read_text(encoding="utf-8")


def _push_skill_script(dsh_home: str) -> str:
    """把技能正文写进 $DSH_HOME/skills/<name>/（token 由 stdin 单独给）。"""
    target = f"{dsh_home}/skills/{SKILL_NAME}"
    lines = [
        "set -e",
        f"d={shlex.quote(target)}",
        'umask 022',
        'mkdir -p "$d"',
    ]
    for name in SKILL_FILES:
        lines.append(f"printf %s {shlex.quote(_b64(skill_file(name)))} | base64 -d > \"$d/{name}\"")
    lines += [
        # token 从 stdin 来：命令行会在目标机 ps 里裸奔，还会进任务日志
        "read -r image_url",
        "read -r backup_url",
        "read -r image_token",
        'umask 077',
        "{",
        ('  echo "# 生图服务地址与密钥，由 dsh-control 铺设；'
         '改请改站点配置后点重启"'),
        "  printf 'COMFY_URL=%s\\n' \"$image_url\"",
        '  [ -n "$backup_url" ] && printf \'COMFY_BACKUP_URL=%s\\n\' "$backup_url"',
        '  printf \'COMFY_TOKEN=%s\\n\' "$image_token"',
        '  echo "COMFY_TIMEOUT=180"',
        '} > "$d/config.env"',
        'umask 022',
        'chmod 600 "$d/config.env"',
        'ls -1 "$d"',
    ]
    return "\n".join(lines)


def _team_patch_script(dsh_home: str, max_members: int) -> str:
    """home 层补丁：从镜像里读官方补丁正文，追加到 $DSH_HOME/cordis.patch.yml。

    官方补丁会插入 agent-team / tool-agent-team / ui-agent-team，并把四条子代理
    工具 disable 掉（团队协作取代了直接委派）。我们在后面补一条同 id 的条目，把
    成员数压到家庭机器扛得住的量——补丁按顺序应用，后面的 config 整段替换前面的。

    官方文件的位置不写死：顺着 dsh 可执行文件解析出包根再找（现网实测
    /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js → 包根上一级就是
    node_modules，183 ms 找到）。找不到就跳过并说明，不猜路径。
    """
    home = f"{dsh_home}/cordis.patch.yml"
    quoted = shlex.quote(TEAM_MARKER)
    return "\n".join([
        "set -e",
        f"f={shlex.quote(home)}",
        (f"if [ -f \"$f\" ] && grep -qF {quoted} \"$f\"; then "
         "echo '智能体团队：home 层已铺过，跳过'; exit 0; fi"),
        'bin="$(readlink -f "$(command -v dsh)")"',
        'pkg="$(CDPATH= cd -- "$(dirname "$bin")/.." && pwd)"',
        (f"src=\"$(find \"$pkg\" -path '*{_TEAM_BUNDLE}/cordis.patch.yml' "
         '2>/dev/null | head -1)\"'),
        ("[ -n \"$src\" ] || { echo '智能体团队：镜像里找不到官方组合包，"
         "跳过（不影响其他功能）'; exit 0; }"),
        (f"{{ echo; echo {quoted}; cat \"$src\"; "
         "echo; "
         "echo '- id: agent-team'; "
         "echo '  name: \"@deepseek-ai/dsh-experimental-agent-team\"'; "
         "echo '  config:'; "
         f"echo '    maxMembers: {int(max_members)}'; "
         "echo '    maxTasks: 256'; "
         "echo '    maxMessageBytes: 65536'; "
         "echo '    disposalTimeoutMs: 5000'; "
         '} >> "$f"'),
        # 回显的是配置里的成员上限，不是 grep 命中次数（那种数字看着像值，最骗人）
        ('echo "智能体团队：已写入 home 层 $f，成员上限 '
         '$(grep maxMembers \"$f\" | tail -1 | tr -d \" \" | cut -d: -f2)"'),
        'wc -c "$f"',
    ])


async def _data_home(ctx: JobCtx, container: str) -> str:
    """DSH 的家目录在容器里固定是 /data（镜像 ENV DSH_HOME=/data），取不到也不猜。"""
    r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c "
                      + shlex.quote('printf %s "${DSH_HOME:-/data}"'), timeout=60)
    home = (r.stdout or "").strip().rstrip("/") or "/data"
    return home


async def apply_capabilities(ctx: JobCtx, container: str, *,
                             dsh_home: str | None = None) -> dict:
    """铺能力并回读校验。返回一组可直接写进 step detail / 日志的事实。"""
    container_q = shlex.quote(container)
    home = dsh_home or await _data_home(ctx, container)
    out: dict[str, str] = {}

    # ── 1. 智能体团队（home 层补丁，不碰镜像）──
    r = await ctx.ssh(f"docker exec {container_q} sh -c {shlex.quote(_team_patch_script(home, ctx.settings.team_max_members))}",
                      timeout=120)
    if not r.ok:
        raise StepFailed(f"铺设智能体团队失败: {r.tail()}")
    out["team"] = (r.stdout or "").strip().splitlines()[0][:200]

    # ── 2. 生图技能（缺材料就跳过，不挡住开通/重启）──
    material = await image_material(ctx)
    if not material["url"] or not material["token"]:
        out["image"] = _IMAGE_SKIP
        await ctx.log("warn", _IMAGE_SKIP)
    else:
        script = _push_skill_script(home)
        stdin = f"{material['url']}\n{material['backup_url']}\n{material['token']}\n"
        r = await ctx.ssh(f"docker exec -i {container_q} sh -c {shlex.quote(script)}",
                          timeout=120, stdin=stdin, quiet=True)
        if not r.ok:
            raise StepFailed(f"铺设生图技能失败: {r.tail()}")
        names = " ".join((r.stdout or "").split())
        out["image"] = f"生图技能已铺到 {home}/skills/{SKILL_NAME}/（{names}）"

    # ── 3. 回读：技能目录与 home 层各确认一眼 ──
    check = "\n".join([
        f"echo \"技能: $(ls -1 {shlex.quote(home)}/skills 2>/dev/null | tr '\\n' ' ')\"",
        f"echo \"home 层: $([ -f {shlex.quote(home)}/cordis.patch.yml ] && echo 有 || echo 无)\"",
    ])
    r = await ctx.ssh(f"docker exec {container_q} sh -c {shlex.quote(check)}", timeout=60)
    for line in (r.stdout or "").splitlines():
        if line.strip():
            out.setdefault("check", "")
            out["check"] += line.strip() + "；"
            await ctx.log("info", f"[能力] {line.strip()}"[:200])
    return out


async def capabilities_ready(ctx: JobCtx, container: str, *,
                             dsh_home: str | None = None) -> bool:
    """技能与 home 层是否都已就位（重启步骤用来决定要不要重铺）。"""
    home = dsh_home or await _data_home(ctx, container)
    script = ("[ -f "
              + shlex.quote(f"{home}/cordis.patch.yml") + " ] && [ -d "
              + shlex.quote(f"{home}/skills/{SKILL_NAME}") + " ] && echo yes || echo no")
    r = await ctx.ssh(f"docker exec {shlex.quote(container)} sh -c {shlex.quote(script)}",
                      timeout=60)
    return "yes" in (r.stdout or "")


async def read_dsh_config(runtime, container: str, *, dsh_version: str = "") -> dict:
    """把容器里实际生效的 DSH 配置读成给详情页用的分组事实。

    两次 SSH 往返（dump-config 约 0.6 秒、能力清单约 0.2 秒）。不放进详情接口本身：
    台账页现在一次 ssh 都不做、秒开，把它塞进首屏等于让 NAS 的负载决定页面快慢。
    """
    from datetime import UTC, datetime

    from ..domain import dsh_config

    dump = await runtime.dump_config(container)
    if not dump.strip():
        return {}
    facts = dsh_config.container_facts(await runtime.container_facts(container))
    return dsh_config.build_facts(
        dump,
        bundles=facts["bundles"],
        skills=facts["skills"],
        dsh_version=dsh_version,
        captured_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        home_patch_present=facts["home_patch"],
    )
