"""CLI 入口必须**一个命令只用一个事件循环**（真机踩过的坑：装配 providers 的
asyncio.run 与命令体的 asyncio.run 是两个循环，DB 连接池绑死在第一个上，
第二次用就 "Future attached to a different loop" + "Event loop is closed"）。

这里直接调 cmd_expose / cmd_images 入口，并数 asyncio.run 的调用次数——
谁以后再往 cmd_* 里塞第二次 asyncio.run，这条测试立刻红。
"""

from __future__ import annotations

import asyncio

import pytest

from dsh_control import cli
from dsh_control.db.models import Credential, Host
from dsh_control.db.session import create_all, make_db
from dsh_control.settings import get_settings


def _prep_db(url: str) -> None:
    """建表 + 一台 enabled 主机（setup 用独立循环，与命令循环无关）。"""
    async def _go() -> None:
        db = make_db(url)
        await create_all(db)
        async with db.session() as s:
            s.add(Host(name="nas-1", ssh_host="192.0.2.10", ssh_port=2333,
                       ssh_user="ops", data_root="/hdd1/dsh-data",
                       kit_path="/srv/dsh-family", frpc_client_id="ops.c.nas1@1",
                       enabled=True))
            s.add(Credential(kind="operator_key", name="default",
                             secret={"key": "test-op-key"}, note=""))
            await s.commit()
        await db.engine.dispose()

    asyncio.run(_go())


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    # chdir 到 tmp：get_settings 会读仓库根的 .env（开发机上的真站点配置），
    # 测试必须与它无关——否则 provider 选择、域名全被真值污染。
    monkeypatch.chdir(tmp_path)
    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    _prep_db(url)
    for k, v in {
        "DSHC_DATABASE_URL": url,
        "DSHC_PUBLIC_BASE": "https://console.example.invalid",
        "DSHC_DNS_ZONE": "example.invalid",
        "DSHC_INGRESS_IP": "192.0.2.1",
        "DSHC_FRPS_UPSTREAM_HOST": "198.51.100.1",
        "DSHC_CONSOLE_HOST": "nas-1",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


@pytest.fixture
def loop_counter(monkeypatch):
    """数 asyncio.run 调用次数；pytest-asyncio 的测试循环不走 asyncio.run，
    所以计到的每一次都是被测代码自己开的循环。"""
    calls: list[int] = []
    real_run = asyncio.run

    def spy(coro, **kw):
        calls.append(1)
        return real_run(coro, **kw)

    monkeypatch.setattr(asyncio, "run", spy)
    return calls


def test_cmd_expose_uses_single_event_loop(cli_env, loop_counter, capsys):
    cli.cmd_expose()
    assert len(loop_counter) == 1, "cmd_expose 开了不止一个事件循环"
    out = capsys.readouterr().out
    # 全链路走通（noop 层人工指令 + 探活失败提示 + 安全组提醒都在 stdout）
    assert "console.example.invalid" in out
    assert "安全组" in out
    assert "人工" in out          # noop 层指令确实打出来了


def test_cmd_expose_dry_run_single_loop(cli_env, loop_counter, capsys):
    cli.cmd_expose(dry_run=True)
    assert len(loop_counter) == 1
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_cmd_images_ls_single_loop(cli_env, loop_counter):
    # nas-1 没有 ssh_key 凭据：应在同一循环内失败为 SystemExit，不许出现循环错误
    args = cli.argparse.Namespace(images_cmd="ls", host="nas-1")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_images(args)
    assert len(loop_counter) == 1
    msg = str(exc.value)
    assert "SSH 私钥" in msg
    assert "loop" not in msg.lower()  # 报的必须是业务错，不是循环错
