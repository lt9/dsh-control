"""release：确认词逐字相等、删除顺序、只 stop 不删数据（SEED.md §3 第 5 步）。"""

from __future__ import annotations

import pytest  # noqa: F401

from tests.conftest import XRW, wait_job


async def deploy(env) -> dict:
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    return body


def _idx(journal, needle):
    for i, line in enumerate(journal):
        if needle in line:
            return i
    return -1


async def test_release_confirm_mismatch_422(env):
    body = await deploy(env)
    iid = body["instance_id"]
    for wrong in ("dsh-1", "DSH-01", "dsh-01 ", "confirm", ""):
        r = await env.client.post(f"/api/instances/{iid}/release",
                                  json={"confirm": wrong}, headers=XRW)
        assert r.status_code == 422, wrong
        assert r.json()["detail"]["error"] == "confirm_mismatch"
    # 422 不许产生任何副作用
    assert _idx(env.journal, "ng.delete") == -1


async def test_release_order_stop_only_no_rm(env):
    body = await deploy(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)

    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert [st["key"] for st in job["steps"]] == [
        "probe_guard", "ng_delete", "frp_delete", "dns_delete",
        "container_stop", "mark_released"]

    j = env.journal
    i_ng, i_frp, i_dns, i_stop = (_idx(j, "ng.delete"), _idx(j, "frp.delete"),
                                  _idx(j, "dns.delete"), _idx(j, "runtime.stop"))
    assert -1 < i_ng < i_frp < i_dns < i_stop, j  # nginx→frp→DNS→stop 硬性顺序
    assert "dsh_01" in j[i_frp]  # expect_name 核对过

    # 数据保留硬边界：全程不出现 rm / prune / 删卷 / 删目录
    blob = "\n".join(j).lower()
    for banned in ("docker rm", " volume", "prune", "rmdir", " rm -"):
        assert banned not in blob

    # SEED.md §3 第 5 步：DNS/frp/NG 三处都查不到；容器只是 stopped
    assert await env.providers.dns.find("dsh-01") is None
    assert await env.providers.frp.list() == []
    assert await env.providers.ng.find_site("dsh-01.example.test") is None
    assert env.runtime.exists is True and env.runtime.state == "exited"

    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "released" and d["desired_state"] == "released"
    assert d["released_at"] is not None
    assert d["actions"] == {"restart": False, "release": False}
    # 端口保留不复用
    assert d["network"]["frp_remote_port"] == 4893


async def test_release_events_record_each_deleted_object(env):
    body = await deploy(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    by_kind = {e["kind"]: e for e in evs}
    assert by_kind["release_ng_deleted"]["data"]["server_name"] == "dsh-01.example.test"
    assert by_kind["release_frp_deleted"]["data"]["name"] == "dsh_01"
    assert by_kind["release_dns_deleted"]["data"]["record_id"]
    assert "container" in by_kind["release_container_stopped"]["data"]
    assert by_kind["released"]["data"]["slug"] == "dsh-01"


async def test_release_twice_409_and_restart_after_release_409(env):
    body = await deploy(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    assert r.status_code == 409
    r = await env.client.post(f"/api/instances/{iid}/restart", headers=XRW)
    assert r.status_code == 409


async def test_release_from_pending_ok_and_concurrent_job_blocked(env):
    # pending 状态也在可释放集合（部署失败留下的半成品要能拆）
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    # 部署进行中再点重启：实例已是 deploying，重启 409
    r2 = await env.client.post(f"/api/instances/{body['instance_id']}/restart",
                               headers=XRW)
    assert r2.status_code == 409
    await wait_job(env.app, body["job_id"])


async def test_release_noop_instance_deletes_nothing_with_fake_ids(noop_env):
    """noop 开通的实例释放：三层没有真实 id（空），步骤必须 skipped、
    一条删除请求都不发；只有 container_stop 真执行。"""
    r = await noop_env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(noop_env.app, body["job_id"])
    r = await noop_env.client.post(f"/api/instances/{body['instance_id']}/release",
                                   json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(noop_env.app, r.json()["job_id"])

    by_key = {st["key"]: st for st in (
        (await noop_env.client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    )["steps"]}
    for key in ("ng_delete", "frp_delete", "dns_delete"):
        assert by_key[key]["state"] == "skipped", key
        assert "人工" in by_key[key]["detail"]
    assert by_key["container_stop"]["state"] == "succeeded"
    assert not any(ln.startswith(("ng.delete", "frp.delete", "dns.delete"))
                   for ln in noop_env.journal)
    assert noop_env.runtime.state == "exited" and noop_env.runtime.exists


async def test_release_refuses_synthetic_ids(env):
    """库里若被污染成 noop 占位值（"manual" / 0），释放也绝不发删除请求。"""
    from dsh_control.db import repo

    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    async with env.db.session() as s:
        inst = await repo.get_instance(s, body["instance_id"])
        inst.dns_record_id = "manual"   # 人为污染成 registry 占位值
        inst.frp_proxy_id = 0
        inst.ng_server_id = 0
        await s.commit()
    r = await env.client.post(f"/api/instances/{body['instance_id']}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    assert not any(ln.startswith(("dns.delete", "frp.delete", "ng.delete"))
                   for ln in env.journal)
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert d["state"] == "released"
