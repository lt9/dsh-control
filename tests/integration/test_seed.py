"""seed：站点描述文件 → credentials/hosts（init 零站点数据的另一半）。"""

from __future__ import annotations

from sqlalchemy import select

from dsh_control.cli import _resolve_secret, seed_into
from dsh_control.db.models import Credential, Host


def test_resolve_secret_from_env_and_file(tmp_path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("PEM-DATA\n", encoding="utf-8")
    monkeypatch.setenv("MY_AK", "AKVALUE")
    monkeypatch.setenv("MY_SK", "SKVALUE")
    monkeypatch.setenv("MY_KEY_FILE", str(key_file))
    secret = _resolve_secret({
        "name": "x", "kind": "ssh_key",
        "env": {"access_key_id": "MY_AK", "access_key_secret": "MY_SK"},
        "env_file": {"private_key_pem": "MY_KEY_FILE"},
        "base_url": "http://panel.local",
    })
    assert secret["access_key_id"] == "AKVALUE"
    assert secret["private_key_pem"] == "PEM-DATA\n"
    assert secret["base_url"] == "http://panel.local"
    # 缺失的变量：留空 + 警告，不炸
    secret2 = _resolve_secret({"name": "y", "kind": "ssh_key",
                               "env": {"private_key_pem": "NO_SUCH_VAR"}})
    assert secret2["private_key_pem"] == ""


async def test_seed_into_populates_site_creds_hosts(env):
    site = {
        "site": {"public_base": "https://c.example.test",
                 "ingress_ip": "203.0.113.99", "dns_zone": "example.test",
                 "domain_prefix": "dsh-", "frp_port_start": 4900,
                 "frps_upstream_host": "198.51.100.77"},
        "providers": {"dns": "noop", "frp": "noop", "ng": "noop"},
        "credentials": [
            {"kind": "ssh_key", "name": "console-ed25519",
             "env": {"private_key_pem": "SEED_TEST_PEM"}},
        ],
        "hosts": [
            {"name": "nas-x", "ssh_host": "198.51.100.8", "ssh_port": 2333,
             "ssh_user": "u1", "kit_path": "/srv/kit", "data_root": "/mnt/hdd/dsh",
             "frpc_client_id": "u1.c.nasx@1", "credential_name": "console-ed25519"},
            {"name": "vps-y", "ssh_host": "203.0.113.40", "ssh_user": "u2",
             "kit_path": "/srv/kit", "data_root": "/srv/dsh",
             "upstream_mode": "direct", "mem_limit": "1g", "cpu_limit": "2",
             "direct_backend": "198.51.100.77", "llm_base_url": "http://198.51.100.77:7777/v1",
             "credential_name": "console-ed25519"},
        ],
    }
    import os

    os.environ["SEED_TEST_PEM"] = "INLINE-PEM"
    try:
        n_host, n_cred = await seed_into(env.db, site)
    finally:
        del os.environ["SEED_TEST_PEM"]
    assert (n_host, n_cred) == (2, 1)

    async with env.db.session() as s:
        site_row = next((c for c in (await s.scalars(
            select(Credential).where(Credential.kind == "site"))).all()
            if c.name == "site"), None)
        # env fixture 已灌过 site？没有——fixture 只灌 operator_key；seed_into 会 upsert
        assert site_row.secret["dns_zone"] == "example.test"
        assert site_row.secret["providers"]["dns"] == "noop"
        # ⑧修正：回源地址随 site.toml 入库，并会被 apply_site_overrides 灌进 settings
        assert site_row.secret["frps_upstream_host"] == "198.51.100.77"

        key = await s.scalar(select(Credential).where(Credential.name == "console-ed25519"))
        assert key.secret["private_key_pem"] == "INLINE-PEM"

        host = (await s.scalars(select(Host).where(Host.name == "nas-x"))).one()
        assert host.ssh_port == 2333
        assert host.ssh_credential_id == key.id
        # 没写 upstream_mode 的主机 = frp 默认，行为零变化；没写限额 = None
        assert host.upstream_mode == "frp"
        assert host.mem_limit is None and host.cpu_limit is None
        # 没登记回源/模型覆盖 = None：普通机走 §6.1.4 原口径，零变化
        assert host.direct_backend is None and host.llm_base_url is None
        vps = (await s.scalars(select(Host).where(Host.name == "vps-y"))).one()
        assert vps.upstream_mode == "direct"
        assert vps.mem_limit == "1g" and vps.cpu_limit == "2"
        assert vps.direct_backend == "198.51.100.77"
        assert vps.llm_base_url == "http://198.51.100.77:7777/v1"

    from dsh_control.settings import Settings, apply_site_overrides
    st = apply_site_overrides(Settings(_env_file=None), site["site"])
    assert st.frps_upstream_host == "198.51.100.77"

    # 幂等：再灌一次不重复
    import os as _os

    _os.environ["SEED_TEST_PEM"] = "INLINE-PEM"
    try:
        await seed_into(env.db, site)
    finally:
        del _os.environ["SEED_TEST_PEM"]
    async with env.db.session() as s:
        assert len((await s.scalars(select(Host))).all()) == 4  # fixture 两台 + nas-x + vps-y


async def test_seed_rejects_unknown_upstream_mode(env):
    import pytest

    site = {"hosts": [{"name": "weird", "ssh_host": "203.0.113.41",
                       "upstream_mode": "direct-tunnel"}]}
    with pytest.raises(SystemExit, match="upstream_mode"):
        await seed_into(env.db, site)


async def test_migrations_contain_no_inserts():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "migrations"
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8").lower()
        assert "op.bulk_insert" not in text
        assert "insert into" not in text
        banned = (("pip" "boy"), ("8.134." "168.14"), "172.19.")  # 拼接：守卫本身不留痕
        assert not any(tok in text for tok in banned), f.name
