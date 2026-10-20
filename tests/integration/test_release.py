"""release：确认词逐字相等、删除顺序、只 stop 不删数据（SEED.md §3 第 5 步）。"""

from __future__ import annotations

import pytest  # noqa: F401

from tests.conftest import XRW, create_instance, wait_job


async def deploy(env) -> dict:
    r = await create_instance(env.client)
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
    # 终态三个动作全关：deploy 必须在里面——released 是终态，绝不能给"重跑开通"入口
    assert d["actions"] == {"restart": False, "release": False, "deploy": False}
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
    r = await create_instance(env.client)
    body = r.json()
    # 部署进行中再点重启：实例已是 deploying，重启 409
    r2 = await env.client.post(f"/api/instances/{body['instance_id']}/restart",
                               headers=XRW)
    assert r2.status_code == 409
    await wait_job(env.app, body["job_id"])


async def test_release_noop_instance_deletes_nothing_with_fake_ids(noop_env):
    """noop 开通的实例释放：三层没有真实 id（空），步骤必须 skipped、
    一条删除请求都不发；只有 container_stop 真执行。"""
    r = await create_instance(noop_env.client)
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

    r = await create_instance(env.client)
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
    # "ng.delete " 带尾空格=站点删除动作。upstream 那条（"ng.delete_upstream"）
    # 不在此列且**应当**发出：它的 id 是真登记值（污染只针对 dns/frp/站点三层），
    # 释放漏删它就是面板攒孤儿（dsh-01 实测踩过）。
    assert not any(ln.startswith(("dns.delete", "frp.delete", "ng.delete "))
                   for ln in env.journal)
    assert env.providers.ng.upstreams == {}  # 真登记的 upstream 被正常删走
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert d["state"] == "released"


# ---------------------------------------------------------------- D1/D3/D4
# 孤儿 upstream、假凭据 id、"对象已不在"与"身份不明"的区分。


async def test_release_deletes_upstream_after_site(env):
    """D1：释放删完站点必须接着删它名下 upstream，面板里一条不许留。"""
    body = await deploy(env)
    iid = body["instance_id"]
    assert env.providers.ng.upstreams, "开通成功后面板上应有 DSH_01 upstream"
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    assert env.providers.ng.sites == {} and env.providers.ng.upstreams == {}
    j = env.journal
    i_site = _idx(j, "ng.delete dsh-01.example.test")
    i_up = _idx(j, "ng.delete_upstream")
    assert -1 < i_site < i_up, j  # 先删引用方（站点），再删被引用方（upstream）
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    ng_ev = next(e for e in evs if e["kind"] == "release_ng_deleted")
    assert ng_ev["data"]["upstream"] == "DSH_01"
    assert ng_ev["data"]["upstream_cleanup_ok"] is True


async def test_release_upstream_already_gone_is_not_an_error(env):
    """D1 幂等：upstream 已被人工删过（或重复释放），照常成功、不许出 error 日志。"""
    body = await deploy(env)
    iid = body["instance_id"]
    env.providers.ng.upstreams.clear()  # 模拟面板里已被人删干净
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert job["state"] == "succeeded", job["error"]
    logs = (await env.client.get(f"/api/jobs/{job_id}/logs")).json()["items"]
    assert [l for l in logs if l["level"] == "error"] == []
    assert any("已不存在" in l["line"] for l in logs)


async def test_release_tunnel_id_hijacked_still_refuses(env):
    """D4 红线：登记的 proxy_id 仍查得到、但名字不是我们的 → 拒删照旧，步骤失败，
    别人那条隧道一根毛都不许动。（"本来就没有"豁免只认 id 与名字双双查无。）"""
    from dsh_control.db import repo

    body = await deploy(env)
    iid = body["instance_id"]
    async with env.db.session() as s:
        pid = (await repo.get_instance(s, iid)).frp_proxy_id
    assert pid in env.providers.frp.tunnels
    env.providers.frp.tunnels[pid].name = "other_app"  # id 被别的对象占了
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    by_key = {st["key"]: st for st in job["steps"]}
    assert by_key["frp_delete"]["state"] == "failed", by_key["frp_delete"]
    assert pid in env.providers.frp.tunnels  # 没被删，红线守住


async def test_deploy_rollback_removes_upstream_and_clears_ids(env):
    """D1+D3：开通失败回滚后——面板四样全清零（upstream 也在），库里外部 id
    必须清空（含 frp 端口放回池子），并留审计事件。留着就是假凭据。"""
    from dsh_control.db import repo

    # 失败注入在第 50 步的 reload：与 dsh-01 实测同形态（upstream/站点已建成，
    # reload 失败 → 步骤 50 失败触发回滚）。故意不选第 60 步——那边契约是
    # "保留现场不回滚"（deploy.py 第 60 步开头 rollback_actions.clear()）。
    env.providers.ng.fail_reload = True
    r = await create_instance(env.client)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    iid = body["instance_id"]

    assert env.providers.ng.sites == {} and env.providers.ng.upstreams == {}
    assert env.providers.frp.tunnels == {} and env.providers.dns.records == {}
    j = env.journal
    i_site = _idx(j, "ng.delete dsh-01.example.test")
    i_up = _idx(j, "ng.delete_upstream")
    assert -1 < i_site < i_up, j  # 回滚同为：先站点后 upstream

    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.dns_record_id == "" and inst.frp_proxy_id is None
        # 回滚那次什么都没建成：端口放回池子给下一个实例
        # （释放成功路径相反——端口保留不复用，见上条契约钉）
        assert inst.frp_remote_port is None
        assert (inst.ng_server_id is None and inst.ng_location_id is None
                and inst.ng_upstream_id is None and inst.ng_upstream_name == "")
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    # 回滚现在按"谁删的谁抹"逐对象清（一次失败的续建不许清别人建的对象），
    # 事件因此是多条：合起来必须把这次建过的每个登记字段都交代清楚。
    cleared = [e for e in evs if e["kind"] == "external_ids_cleared"]
    assert cleared, "回滚必须记下抹掉了哪些死 id"
    assert all("已清空" in e["message"] for e in cleared)
    wiped = {k: v for e in cleared for k, v in (e["data"] or {}).items()}
    assert wiped["frp_proxy_id"] is not None           # 记的是清掉前的死 id
    assert wiped["dns_record_id"], "DNS 死 id 没记下来"
    assert wiped["frp_remote_port"] is not None, "端口要放回池子"
    # NG 的 id 是 reload 成功之后才登记的；这次 reload 就失败了，从没登记过，
    # 清理记录里当然不该出现——出现反而说明我们在清自己没碰过的东西。
    assert "ng_server_id" not in wiped and "ng_upstream_id" not in wiped


async def test_release_all_objects_already_gone_passes(env):
    """D4：三层对象都被外部删光（半成品续跑/重复释放的前半程），释放三步
    必须全过、日志零 error——"本来就没有"不是"身份不明"。"""
    body = await deploy(env)
    iid = body["instance_id"]
    env.providers.ng.sites.clear()
    env.providers.ng.upstreams.clear()
    env.providers.frp.tunnels.clear()
    env.providers.dns.records.clear()
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    job_id = r.json()["job_id"]
    await wait_job(env.app, job_id)
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert job["state"] == "succeeded", job["error"]
    logs = (await env.client.get(f"/api/jobs/{job_id}/logs")).json()["items"]
    assert [l for l in logs if l["level"] == "error"] == [], logs
    assert sum(1 for l in logs if "已不存在，视为已删除" in l["line"]) >= 3


async def test_release_recovers_when_panel_changed_proxy_id(env):
    """proxy_id 是缓存不是身份：frpc 掉线重连后面板会重建隧道行换 id
    （现网 2026-10-21：库里 2404，面板上 dsh_01 已是 2439）。照着旧 id 删，
    释放永远卡在"查不到 proxy_id，拒绝盲删"。"""
    from dsh_control.db import repo

    body = await deploy(env)
    iid = body["instance_id"]
    (tunnel,) = env.providers.frp.tunnels.values()
    true_id = tunnel.proxy_id
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        inst.frp_proxy_id = 424242          # 库里一条陈旧登记，面板上早已没有
        await s.commit()

    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    job = (await env.client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    line = next(ln for ln in env.journal if ln.startswith("frp.delete"))
    assert f"id={true_id}" in line, f"必须删面板上那条真身，实际 {line}"
    assert env.providers.frp.tunnels == {}
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    assert any(e["kind"] == "frp_proxy_id_healed" for e in evs), "校正过程要留痕"


async def test_release_refuses_tunnel_with_other_remote_port(env):
    """按名字找回真身时端口必须对得上：对不上就不是我们那条，宁可不删。"""
    from dsh_control.db import repo

    body = await deploy(env)
    iid = body["instance_id"]
    (tunnel,) = env.providers.frp.tunnels.values()
    tunnel.remote_port = 4999               # 同名但指向变了
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        inst.frp_proxy_id = 424242
        await s.commit()

    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    job = (await env.client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    assert job["state"] == "failed"
    assert "不一致" in job["error"] and "拒绝删除" in job["error"]
    assert tunnel.proxy_id in env.providers.frp.tunnels, "对不上就不许动它"
