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
    elif args.cmd == "serve":
        cmd_serve()


if __name__ == "__main__":
    main()
