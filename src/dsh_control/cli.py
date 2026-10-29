"""命令行：init / seed / migrate / expose / images / serve。

- `init`：只做两件事——跑迁移建表 + 生成运维密钥（打印一次，库里存明文可读回）。
  **零站点数据**（docs/SITE-CONFIG.md：数据全走 seed）。
- `seed --site deploy/site.toml`：读站点描述文件灌 hosts 与 credentials；
  凭据值从环境变量读（env = {字段 = 环境变量名}），env_file 的值是私钥文件路径；
  site/providers 段镜像进 credentials(kind=site)。任何密钥值都不打印。
- `serve`：uvicorn 起 API + 静态页（契约 §7 单进程）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import tomllib
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import repo
from .db.models import HOST_UPSTREAM_MODES, Credential, Host
from .db.session import Db, make_db
from .jobs.runner import redact_text
from .settings import get_settings

# ---------------------------------------------------------------- 迁移

def _alembic_config():
    from alembic.config import Config

    # 查找顺序：显式指定 > 当前工作目录 > 包自身往上。
    # 只看包自身是不够的：镜像里包被装进 site-packages，`__file__` 的任何父目录
    # 都不含 /app/migrations，容器里就会报"找不到 migrations/"。
    candidates: list[Path] = []
    env = os.environ.get("DSHC_MIGRATIONS_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / "migrations")
    candidates.extend(parent / "migrations" for parent in Path(__file__).resolve().parents)

    for cand in candidates:
        if cand.is_dir():
            cfg = Config()
            cfg.set_main_option("script_location", str(cand))
            cfg.set_main_option("prepend_sys_path", str(cand.parent))
            return cfg
    raise SystemExit(
        "找不到 migrations/ 目录（应与 pyproject.toml 同级，或用 DSHC_MIGRATIONS_DIR 指定）")


def cmd_migrate(database_url: str) -> None:
    from alembic import command

    os.environ["DSHC_DATABASE_URL"] = database_url
    command.upgrade(_alembic_config(), "head")


# ---------------------------------------------------------------- init

def _generate_operator_key() -> str:
    try:
        import bech102

        return bech102.encode(secrets.token_bytes(16))
    except Exception:  # noqa: BLE001 —— bech102 缺失时退回可读前缀
        return "dshc-" + secrets.token_urlsafe(24)


def cmd_init() -> None:
    settings = get_settings()
    url = settings.require("database_url")
    cmd_migrate(url)
    db = make_db(url)

    async def _run() -> str | None:
        try:
            async with db.session() as s:
                existing = await repo.get_credential_by_kind(s, "operator_key")
                if existing is not None:
                    return None
                key = _generate_operator_key()
                await repo.upsert_credential(
                    s, kind="operator_key", name="default", secret={"key": key},
                    note="运维密钥：明文存储（用户要求可读回），只在 init stdout 打印一次",
                )
                return key
        finally:
            await db.engine.dispose()

    key = asyncio.run(_run())
    if key is None:
        print("已存在 operator_key（kind=operator_key, name=default），未重复生成；可读回。")
    else:
        print("=" * 62)
        print("运维密钥（只打印这一次，之后可从 credentials 读回）：")
        print(key)
        print("=" * 62)


# ---------------------------------------------------------------- seed

def _resolve_secret(entry: dict[str, Any]) -> dict[str, str]:
    """secret 值只从环境变量取；env_file 的值是文件路径，读其内容。绝不打印值。"""
    secret: dict[str, str] = {}
    for field, var in (entry.get("env") or {}).items():
        value = os.environ.get(str(var), "")
        if not value:
            print(f"警告: 凭据 {entry.get('name')} 的字段 {field} 环境变量 {var} 为空",
                  file=sys.stderr)
        secret[field] = value
    for field, var in (entry.get("env_file") or {}).items():
        path = os.environ.get(str(var), "")
        if not path:
            print(f"警告: 凭据 {entry.get('name')} 的字段 {field} 环境变量 {var} 为空",
                  file=sys.stderr)
            continue
        p = Path(path)
        if not p.is_file():
            print(f"警告: 凭据 {entry.get('name')} 的文件不存在: {redact_text(path)}",
                  file=sys.stderr)
            continue
        secret[field] = p.read_text(encoding="utf-8")
    for field, value in (entry.get("const") or {}).items():
        secret[field] = str(value)
    if entry.get("base_url"):
        secret["base_url"] = str(entry["base_url"])
    return secret


async def seed_into(db: Db, site: dict[str, Any]) -> tuple[int, int]:
    """站点数据入库的唯一入口（init 零站点数据，全部走这里；测试可直接调）。"""
    async with db.session() as s:
        site_secret: dict[str, Any] = dict(site.get("site") or {})
        if site.get("providers"):
            site_secret["providers"] = dict(site["providers"])
        if site_secret:
            await repo.upsert_credential(
                s, kind="site", name="site", secret=site_secret,
                note="站点描述文件 [site]+[providers] 段的落库镜像",
            )
        n_cred = 0
        for entry in site.get("credentials") or []:
            await repo.upsert_credential(
                s, kind=str(entry["kind"]), name=str(entry["name"]),
                secret=_resolve_secret(entry), note=str(entry.get("note", "")),
            )
            n_cred += 1
        n_host = 0
        for h in site.get("hosts") or []:
            cred_id = None
            if h.get("credential_name"):
                cred = await repo.get_credential_by_name(s, str(h["credential_name"]))
                cred_id = cred.id if cred else None
            host = await s.scalar(select(Host).where(Host.name == str(h["name"])))
            if host is None:
                host = Host(name=str(h["name"]))
                s.add(host)
            host.ssh_host = str(h.get("ssh_host", ""))
            host.ssh_port = int(h.get("ssh_port", 22))
            host.ssh_user = str(h.get("ssh_user", ""))
            host.ssh_credential_id = cred_id
            host.docker_context = str(h.get("docker_context", "default"))
            host.kit_path = str(h.get("kit_path", ""))
            host.data_root = str(h.get("data_root", ""))
            host.frpc_client_id = str(h.get("frpc_client_id", ""))
            # 直连主机与资源配额（§2/§6.1.4）：direct 不建隧道、NG 直接回源 ssh_host；
            # mem/cpu 限额开通时写进实例 .env，共享机必填的护栏在这里登记
            mode = str(h.get("upstream_mode", "frp")).strip() or "frp"
            if mode not in HOST_UPSTREAM_MODES:
                raise SystemExit(
                    f"hosts.{h.get('name')}: upstream_mode={mode!r} 非法，"
                    f"只认 {'|'.join(HOST_UPSTREAM_MODES)}")
            host.upstream_mode = mode
            host.mem_limit = str(h.get("mem_limit") or "").strip() or None
            host.cpu_limit = str(h.get("cpu_limit") or "").strip() or None
            # 云轻量服务器（SWAS 等）EIP 不在网卡上：回源/绑定要用登记地址（§6.1.4）
            host.direct_backend = str(h.get("direct_backend") or "").strip() or None
            # 模型入口按主机覆盖：云上机器拨不到局域网网关（§2）
            host.llm_base_url = str(h.get("llm_base_url") or "").strip() or None
            # 制品下载基址按主机覆盖：入口机拨自家公网域是 hairpin 黑洞（§6.1.5）
            host.image_base_url = str(h.get("image_base_url") or "").strip() or None
            host.enabled = bool(h.get("enabled", True))
            host.note = str(h.get("note", ""))
            n_host += 1
        await s.commit()
        return n_host, n_cred


def _seed_sync(site: dict[str, Any]) -> tuple[int, int]:
    db = make_db(get_settings().require("database_url"))

    async def _run() -> tuple[int, int]:
        try:
            return await seed_into(db, site)
        finally:
            await db.engine.dispose()

    return asyncio.run(_run())


def cmd_seed(site_path: str) -> None:
    path = Path(site_path)
    if not path.is_file():
        raise SystemExit(f"站点描述文件不存在: {path}")
    site = tomllib.loads(path.read_text(encoding="utf-8"))
    n_host, n_cred = _seed_sync(site)
    print(f"seed 完成：hosts={n_host}, credentials={n_cred}（密钥值一律不打印）")


# ---------------------------------------------------------------- providers 装配

def _make_db(settings):
    return make_db(settings.require("database_url"))


async def _aload_providers(db, settings):
    """cli 侧与 main.lifespan 同款装配：库里的 credentials 全量交给 registry。

    只能在命令自己那一个事件循环里 await。之前这里是 `_providers_from_db()`：
    内部先 `asyncio.run` 一次，把 asyncpg 连接绑到第一个循环上然后循环就关了，
    命令随后又 `asyncio.run` 用同一个 engine，于是 `Future attached to a
    different loop` + `Event loop is closed`——一跑 expose 就炸。
    """
    from .integrations import registry

    async with db.session() as s:
        creds = (await s.scalars(select(Credential))).all()
    return registry.build_providers(
        settings, [{"kind": c.kind, "name": c.name, "secret": c.secret or {}}
                   for c in creds])


# ---------------------------------------------------------------- expose

def cmd_expose(dry_run: bool = False) -> None:
    """给主控台自己的公网入口开三处外部对象（幂等，只创建不删除）。"""
    from .console import run_expose

    settings = get_settings()
    if dry_run:
        settings.dry_run = True
    db = _make_db(settings)

    async def _go():
        async def log(level: str, msg: str) -> None:
            print(f"[{level}] {msg}")
        providers = None
        try:
            providers = await _aload_providers(db, settings)
            return await run_expose(db, providers, settings, dry_run=dry_run, log=log)
        finally:
            if providers is not None:
                close = getattr(providers, "aclose", None)
                if close is not None:
                    await close()
            await db.engine.dispose()

    try:
        asyncio.run(_go())
    except RuntimeError as exc:
        raise SystemExit(f"expose 中止：{exc}") from exc


# ---------------------------------------------------------------- images

def cmd_images(args: argparse.Namespace) -> None:
    """镜像排障/搬运（契约 §6.1.2：镜像是准备好的，不是现场构建的）。"""
    from .images import image_ls, ship_image

    settings = get_settings()
    db = _make_db(settings)

    async def _go():
        providers = None
        try:
            providers = await _aload_providers(db, settings)
            if args.images_cmd == "ls":
                return await image_ls(db, providers, args.host)
            return await ship_image(db, providers, tag=args.tag,
                                    src_ref=args.src, dst_ref=args.dst)
        finally:
            if providers is not None:
                close = getattr(providers, "aclose", None)
                if close is not None:
                    await close()
            await db.engine.dispose()

    try:
        out = asyncio.run(_go())
    except RuntimeError as exc:
        raise SystemExit(f"images {args.images_cmd} 失败：{exc}") from exc
    if args.images_cmd == "ls":
        if not out:
            print("该机没有 dsh 相关镜像（cli images ship 可以从别处搬）")
        for row in out:
            print(f"{row['tag']:48s} {row['size']}")
    else:
        mb = out["bytes"] / 1e6
        print(f"搬运完成：{out['tag']}  {out['src']} -> {out['dst']}  "
              f"{out['bytes']} 字节（{mb:.1f} MB），耗时 {out['seconds']}s")
        if out.get("loaded"):
            print(out["loaded"])


# ---------------------------------------------------------------- 资产（kit / 字体库）

def cmd_kit(args: argparse.Namespace) -> None:
    """kit 脚本入库与查看（docs/SITE-CONFIG.md「资产分发」）。"""
    from . import assets

    db = _make_db(get_settings())

    async def _go():
        try:
            if args.kit_cmd == "publish":
                return await assets.publish_kit(db, args.dir, version=args.version,
                                                note=args.note)
            async with db.session() as s:
                return await repo.list_kit_releases(s)
        finally:
            await db.engine.dispose()

    try:
        out = asyncio.run(_go())
        if args.kit_cmd == "publish":
            print(f"kit 入库完成：{out['version']}（{out['files']} 个文件 / "
                  f"{out['size_bytes'] // 1024} KiB）；"
                  "下次开通第 20 步自动对账下发，存量机重跑 deploy 即换装")
        else:
            if not out:
                print("（kit 未入库：cli kit publish --dir <dsh-family 仓库>）")
            for r in out:
                print(f"{'*' if r.current else ' '} {r.version:28s} "
                      f"{r.file_count:4d} 文件  {r.size_bytes // 1024:6d} KiB  "
                      f"{r.created_at:%Y-%m-%d %H:%M}  {r.note}")
    except RuntimeError as exc:
        raise SystemExit(f"kit {args.kit_cmd} 失败：{exc}") from exc


def cmd_fontlib(args: argparse.Namespace) -> None:
    """字体库进制品库与查看（和镜像制品同目录同约定）。"""
    from . import assets

    settings = get_settings()
    db = _make_db(settings)

    async def _go():
        try:
            if args.fontlib_cmd == "publish":
                return await assets.publish_fontlib(
                    db, settings.require("image_store"), version=args.version,
                    note=args.note, local_dir=args.dir, host_ref=args.host,
                    lib=args.lib)
            async with db.session() as s:
                return await repo.list_fontlib_releases(s)
        finally:
            await db.engine.dispose()

    try:
        out = asyncio.run(_go())
    except RuntimeError as exc:
        raise SystemExit(f"fontlib {args.fontlib_cmd} 失败：{exc}") from exc
    if args.fontlib_cmd == "publish":
        print(f"字体库入库完成：{out['version']}（{out['files']} 个文件 / "
              f"{out['size_bytes'] / 1e6:.0f} MB / sha {out['sha256']}… → "
              f"{out['filename']}）；开通第 67 步优先从制品库下发")
    else:
        if not out:
            print("（字体库未入库：cli fontlib publish --host <机器> --lib <路径>）")
        for r in out:
            print(f"{'*' if r.current else ' '} {r.version:28s} "
                  f"{r.file_count:4d} 文件  {r.size_bytes / 1e6:6.0f} MB  "
                  f"sha {r.sha256[:12]}  ← {r.origin}  {r.note}")


# ---------------------------------------------------------------- capability（ADR-0001 决策三 a）

def _cap_target(s, db, slug: str):
    """按 slug 拿 (instance, host, ssh_key_pem)——runner 装配同款的取法。"""
    from sqlalchemy import select as _select

    from .db.models import Credential, Instance

    async def _go():
        inst = await s.scalar(_select(Instance).where(Instance.slug == slug))
        if inst is None:
            raise SystemExit(f"实例 {slug!r} 不存在")
        host = await repo.get_host(s, inst.host_id)
        if host is None:
            raise SystemExit(f"实例 {slug} 的主机（host_id={inst.host_id}）没登记")
        key_pem = ""
        if host.ssh_credential_id:
            cred = await s.get(Credential, host.ssh_credential_id)
            key_pem = (cred.secret or {}).get("private_key_pem", "") if cred else ""
        return inst, host, key_pem
    return _go()


def _split_id_version(text: str) -> tuple[str, str]:
    """`speckit@1.0.0` → ("speckit", "1.0.0")；没带 @ 版本为空（跟随 current）。"""
    if "@" in text:
        cid, _, ver = text.partition("@")
        return cid.strip(), ver.strip()
    return text.strip(), ""


def cmd_capability(args: argparse.Namespace) -> None:
    """能力管理：目录 ls/publish 读库读包；status 现采现报；动作只入队工单。

    动作子命令与页面点的是**同一条路**：kind=capability 工单 → 同一个
    run_capability 执行体——CLI 里不另写一套执行（决策三）。参数不在命令行上：
    引擎按 manifest 的 params schema 从站点配置与 credentials 现取，命令行没有
    `--token` 这类旗标，密钥永远现取现塞 stdin。`--dry-run` 一个字节都不写、
    不入队，探针照跑（只读），逐条打印本来要做的动作。"""
    from .db.models import Capability
    from .integrations import capability_engine as engine

    settings = get_settings()
    db = _make_db(settings)

    async def _ls():
        async with db.session() as s:
            rows = (await s.scalars(
                select(Capability).order_by(Capability.capability_id,
                                            Capability.id.desc()))).all()
            return rows

    async def _publish():
        packs = await asyncio.to_thread(engine.packs_from_dir, args.dir)
        if not packs:
            raise SystemExit(f"{args.dir} 里没找到能力包"
                             "（要 capabilities/<id>/manifest.toml，或直接给一棵包）")
        async with db.session() as s:
            rows = await engine.upsert_catalog(s, packs, kit_version=args.kit_version,
                                               note=args.note)
            return rows

    async def _status():
        from .jobs.runner import HostEndpoint, get_executor, make_runtime
        providers = await _aload_providers(db, settings)
        try:
            async with db.session() as s:
                inst, host, key_pem = await _cap_target(s, db, args.slug)
                from .db.models import InstanceCapability
                ledgers = (await s.scalars(
                    select(InstanceCapability).where(
                        InstanceCapability.instance_id == inst.id))).all()
                out = []
                ssh = engine.make_ssh(get_executor(providers),
                                      HostEndpoint(host.ssh_host, host.ssh_port,
                                                   host.ssh_user, key_pem))
                runtime = make_runtime(providers,
                                       HostEndpoint(host.ssh_host, host.ssh_port,
                                                    host.ssh_user, key_pem))
                container = inst.container_name or inst.slug
                home = await engine.read_dsh_home(ssh, container)
                dsh_ver, source = await engine.read_dsh_version(runtime, container)
                for led in ledgers:
                    cap = await s.scalar(
                        select(Capability).where(
                            Capability.capability_id == led.capability_id,
                            Capability.version == led.desired_version
                            if led.desired_version else Capability.current.is_(True)))
                    if cap is None:
                        out.append({"capability_id": led.capability_id,
                                    "reason": "目录里没有货（先 publish）",
                                    "installed": led.installed, "kind": "?"})
                        continue
                    obs = await engine.run_probe(ssh, container, dict(cap.manifest or {}),
                                                 home=home, values=dict(led.params or {}))
                    obs["dsh_version"] = dsh_ver
                    obs["dsh_version_source"] = source
                    obs["desired_state"] = led.desired_state
                    obs["desired_version"] = led.desired_version
                    out.append(obs)
                return inst, dsh_ver, source, out
        finally:
            close = getattr(providers, "aclose", None)
            if close is not None:
                await close()

    async def _action(dry: bool = False):
        from .db.models import InstanceCapability
        from .jobs.capabilities import run_capability
        from .jobs.runner import JobRunner
        action = args.cap_cmd
        cap_id, version = _split_id_version(args.capability or "")
        if not cap_id and action != "apply":
            raise SystemExit(f"{action} 要指定能力 id（<slug> <id>[@ver]）")
        if dry:
            if not cap_id:
                raise SystemExit("--dry-run 要指定能力 id")
            return await _dry_run(cap_id, version, action)
        providers = await _aload_providers(db, settings)
        runner = JobRunner(db, providers, settings)
        runner.register("capability", run_capability)
        # apply 不带 id = 整实例对账重铺（决策三 `apply <slug>`）：每个
        # desired=present 的台账行入一单；带 id 就只重铺那一个。
        async with db.session() as s:
            inst, _host, _ = await _cap_target(s, db, args.slug)
            targets: list[tuple[str, str]] = []
            if action == "apply" and not cap_id:
                leds = (await s.scalars(
                    select(InstanceCapability).where(
                        InstanceCapability.instance_id == inst.id,
                        InstanceCapability.desired_state == "present"))).all()
                if not leds:
                    print(f"实例 {inst.slug} 台账里没有 desired=present 的能力，无事可做")
                    return
                targets = [(l.capability_id, l.desired_version) for l in leds]
            else:
                targets = [(cap_id, version)]
            jobs = []
            for cid, ver in targets:
                params = {"action": action, "capability": cid, "version": ver,
                          "params": {}}
                job = await repo.create_job(s, inst.id, "capability", params)
                await repo.add_event(s, "capability_queued",
                                     message=f"CLI 入队：{cid} {action}"
                                             f"{'@' + ver if ver else ''}"
                                             f"（实例 {inst.slug}）",
                                     instance_id=inst.id, job_id=job.id, data=params)
                jobs.append(job.id)
                print(f"已入队：job {job.id}（{inst.slug} {cid} {action}）")
        try:
            for job_id in jobs:
                task = runner.spawn(job_id)
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=args.wait)
                except TimeoutError:
                    print(f"job {job_id} 还在跑：页面 job 详情可看实时日志")
                    continue
                async with db.session() as s:
                    j = await repo.get_job(s, job_id)
                    steps = await repo.list_steps(s, job_id)
                    for st in steps:
                        print(f"  [{st.state}] {st.seq}:{st.key} {st.detail[:120]}")
                    if j.state == "succeeded":
                        print(f"job {job_id} 成功")
                    else:
                        raise SystemExit(f"job {job_id} {j.state}: {(j.error or '')[:300]}")
        finally:
            await runner.cancel_all()

    async def _dry_run(cap_id: str, version: str, action: str):
        """报告本来要做什么，一个字节都不写：不写文件、不入队；探针照跑。"""
        from .jobs.runner import HostEndpoint, get_executor
        providers = await _aload_providers(db, settings)
        try:
            async with db.session() as s:
                inst, host, key_pem = await _cap_target(s, db, args.slug)
                cap = await s.scalar(
                    select(Capability).where(
                        Capability.capability_id == cap_id,
                        Capability.version == version if version
                        else Capability.current.is_(True)))
                if cap is None:
                    raise SystemExit(f"目录里没有 {cap_id}@{version or 'current'}")
                ep = HostEndpoint(host.ssh_host, host.ssh_port, host.ssh_user, key_pem)
                ssh = engine.make_ssh(get_executor(providers), ep)
                container = inst.container_name or inst.slug
                home = await engine.read_dsh_home(ssh, container)
                man = dict(cap.manifest or {})
                resolved = engine.resolve_params(man, {}, {})
                probe = await engine.run_probe(ssh, container, man, home=home)
                res = await engine.run_action(ssh, container, man, home, action, resolved,
                                              kit_dir=host.kit_path or "",
                                              version=cap.version, dry_run=True)
                print(f"dry-run：{cap_id}@{cap.version} {action}（不写任何字节）")
                state = "already-in-desired-state" if "幂等" in (res.detail or "") \
                    else "would-change"
                print(f"  判定: {state}")
                print(f"  现状: installed={probe['installed']} "
                      f"enabled={probe['enabled']} version="
                      f"{probe['observed_version'] or '未知'} health={probe['health']}")
                for line in res.planned:
                    print(f"  - {line}")
        finally:
            close = getattr(providers, "aclose", None)
            if close is not None:
                await close()

    async def _wrapped(coro):
        try:
            return await coro
        finally:
            # engine 的连接绑在本命令唯一的事件循环上：dispose 必须同循环内做，
            # 另起 asyncio.run 会撞上 Event loop is closed（cmd_expose 的旧坑）。
            await db.engine.dispose()

    if args.cap_cmd == "ls":
        rows = asyncio.run(_wrapped(_ls()))
        if not rows:
            print("（能力目录为空：cli capability publish --dir <kit 仓库>）")
        for r in rows:
            print(f"{'*' if r.current else ' '} {r.capability_id:22s} {r.version:12s} "
                  f"{r.kind:8s} {r.state:9s} compatible>={r.compatible_dsh:16s} "
                  f"kit={r.kit_version or '-':24s} {r.title}")
    elif args.cap_cmd == "publish":
        rows = asyncio.run(_wrapped(_publish()))
        for r in rows:
            print(f"入目录：{r.capability_id}@{r.version} kind={r.kind} "
                  f"sha={r.manifest_sha256[:12]} compatible_dsh={r.compatible_dsh}")
        print(f"capability publish 完成：{len(rows)} 包入目录"
              "（页面/CLI 的 install/apply 即刻可用）")
    elif args.cap_cmd == "status":
        inst, dsh_ver, source, out = asyncio.run(_wrapped(_status()))
        if args.json:
            import json
            print(json.dumps({"slug": inst.slug, "dsh_version": dsh_ver,
                              "dsh_version_source": source, "items": out},
                             ensure_ascii=False, indent=2))
        else:
            print(f"实例 {inst.slug}：DSH {dsh_ver or '未读到'}"
                  f"（现读来源：{source}）")
            if not out:
                print("  （台账为空：还没有期望态；capability install 或站点默认）")
            for o in out:
                mark = "×" if not o.get("installed") else ("⏻" if o.get("enabled") else "⏸")
                print(f"  {mark} {o['capability_id']:22s} "
                      f"version={o.get('observed_version') or '未知':10s} "
                      f"health={o.get('health', '?'):8s} "
                      f"desired={o.get('desired_state', '?')} "
                      f"{o.get('reason', '')[:60]}")
    else:
        asyncio.run(_wrapped(_action(dry=args.dry_run)))


# ---------------------------------------------------------------- serve

def cmd_serve() -> None:
    import uvicorn

    from .main import create_app

    settings = get_settings()
    uvicorn.run(create_app(), host=settings.listen_host, port=settings.listen_port,
                log_level="info")


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="dsh-control", description="DSH 实例运维主控台")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="建表 + 生成运维密钥（零站点数据）")
    p_seed = sub.add_parser("seed", help="读站点描述文件灌 hosts/credentials")
    p_seed.add_argument("--site", required=True, help="deploy/site.toml 路径")
    sub.add_parser("migrate", help="只跑 alembic upgrade head")
    p_exp = sub.add_parser("expose",
                           help="给主控台公网入口开 DNS/frp/NG（幂等，只创建不删除）")
    p_exp.add_argument("--dry-run", action="store_true",
                       help="打印将要发的每个写请求（走 recorder），一个都不发")
    p_img = sub.add_parser("images", help="镜像在位查看与两机之间流式搬运")
    subi = p_img.add_subparsers(dest="images_cmd", required=True)
    p_ls = subi.add_parser("ls", help="列某主机的 dsh 相关镜像标签与大小")
    p_ls.add_argument("--host", required=True, help="hosts.name 或 ssh_host")
    p_sh = subi.add_parser("ship", help="docker save | docker load 内存流式搬运")
    p_sh.add_argument("--tag", required=True, help="镜像标签（准备好的版本）")
    p_sh.add_argument("--from", dest="src", required=True, help="源主机")
    p_sh.add_argument("--to", dest="dst", required=True, help="目标主机")
    p_kit = sub.add_parser("kit", help="kit 脚本入库（开通第 20 步按版本自动下发）")
    kit_sub = p_kit.add_subparsers(dest="kit_cmd", required=True)
    p_kp = kit_sub.add_parser("publish", help="读 dsh-family 仓库，整包写进 kit_releases")
    p_kp.add_argument("--dir", required=True, help="kit 仓库根（含 scripts/ 与 compose.yaml）")
    p_kp.add_argument("--version", default="", help="默认 kit-<时间戳>")
    p_kp.add_argument("--note", default="", help="发布说明（建议带 git sha）")
    kit_sub.add_parser("ls", help="列已入库版本")
    p_fl = sub.add_parser("fontlib", help="字体库进制品库（开通第 67 步优先下发）")
    fl_sub = p_fl.add_subparsers(dest="fontlib_cmd", required=True)
    p_fp = fl_sub.add_parser("publish", help="本机目录或从来源主机流式拉取打包")
    p_fp.add_argument("--dir", default="", help="本机字体库目录")
    p_fp.add_argument("--host", default="", help="来源主机（hosts.name 或 ssh_host）")
    p_fp.add_argument("--lib", default="", help="来源主机上的字体库目录（配 --host）")
    p_fp.add_argument("--version", default="", help="默认 fontlib-<时间戳>")
    p_fp.add_argument("--note", default="", help="发布说明")
    fl_sub.add_parser("ls", help="列已入库版本")
    p_cap = sub.add_parser("capability",
                           help="能力目录与工单（ADR-0001：publish 发货、动作只入队）")
    cap_sub = p_cap.add_subparsers(dest="cap_cmd", required=True)
    cap_sub.add_parser("ls", help="列能力目录（capabilities 表）")
    p_cp = cap_sub.add_parser("publish",
                              help="读 kit 仓库 capabilities/ 内 manifest 进目录（与 kit publish 同款）")
    p_cp.add_argument("--dir", required=True, help="kit 仓库根（认 capabilities/ 子树）或一棵包目录")
    p_cp.add_argument("--kit-version", default="", help="这批货随哪个 kit 版本发货（默认空）")
    p_cp.add_argument("--note", default="", help="发布说明")
    p_cs = cap_sub.add_parser("status", help="现采现报某实例观测态（排障用，只读）")
    p_cs.add_argument("slug", help="实例 slug，如 dsh-02")
    p_cs.add_argument("--json", action="store_true", help="机器可读输出（含 dsh_version_source）")
    for _act, _help in (("install", "装一个能力（入队工单）"),
                        ("apply", "对账重铺：不带 id = 整实例 desired=present 逐个入队"),
                        ("enable", "启用（可逆）"),
                        ("disable", "禁用（可逆；重启型待自然重启生效）"),
                        ("uninstall", "卸载（uninstall=reverse）")):
        _p = cap_sub.add_parser(_act, help=_help)
        _p.add_argument("slug", help="实例 slug")
        _p.add_argument("capability", nargs="?", default="",
                        help="能力 id，可带 @version（apply 不带 id = 整实例）")
        _p.add_argument("--dry-run", action="store_true",
                        help="报告本来要做什么，一个字节都不写、不入队；探针照跑")
        _p.add_argument("--wait", type=float, default=600,
                        help="等工单跑完的秒数（超时只报 job id，不杀工单）")
    sub.add_parser("serve", help="启动 API + 静态页")
    args = parser.parse_args(argv)

    if args.cmd == "init":
        cmd_init()
    elif args.cmd == "seed":
        cmd_seed(args.site)
    elif args.cmd == "migrate":
        cmd_migrate(get_settings().require("database_url"))
    elif args.cmd == "expose":
        cmd_expose(dry_run=args.dry_run)
    elif args.cmd == "images":
        cmd_images(args)
    elif args.cmd == "kit":
        cmd_kit(args)
    elif args.cmd == "fontlib":
        cmd_fontlib(args)
    elif args.cmd == "capability":
        cmd_capability(args)
    elif args.cmd == "serve":
        cmd_serve()


if __name__ == "__main__":
    main()
