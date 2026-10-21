"""静态入口与缓存头。

现网教训（2026-10-21）：服务端换了新前端，但 index.html 没有内容哈希、也没发
Cache-Control，浏览器按 Last-Modified 启发式缓存，用过控制台的人继续跑旧包——
旧包是"点一下就部署"的版本，凭空开出一台公网实例。入口 HTML 必须每次回源核对。
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from dsh_control.db.session import create_all, make_db
from dsh_control.main import create_app
from tests.conftest import make_settings


@pytest_asyncio.fixture
async def site(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>台账</title>", encoding="utf-8")
    (dist / "assets" / "index-DEADBEEF.js").write_text("console.log(1)", encoding="utf-8")
    db = make_db("sqlite+aiosqlite:///:memory:")
    await create_all(db)
    app = create_app(db=db, settings=make_settings(), start_watcher=False, static_dir=dist)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        yield client
    await db.engine.dispose()


async def test_index_html_is_not_cacheable(site):
    for path in ("/", "/instances/2", "/no-such-route"):
        r = await site.get(path)
        assert r.status_code == 200, path
        # no-cache 不是"不缓存"，是"每次必须回源核对"——新版前端要能立刻被拿到
        assert r.headers.get("cache-control") == "no-cache", (path, r.headers)


async def test_hashed_assets_still_served(site):
    r = await site.get("/assets/index-DEADBEEF.js")
    assert r.status_code == 200
    assert "console.log" in r.text
