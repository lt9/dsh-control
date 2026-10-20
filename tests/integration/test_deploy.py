"""deploy 全流程：服务端选主机与 slug、派生命名、noop 三态、日志增量、SSE。"""

from __future__ import annotations

from tests.conftest import XRW, wait_job

STEP_KEYS_DEPLOY = ["plan", "host_prepare", "dns_create", "frp_create", "ng_create",
                    "container_deploy", "token_capture", "probe_public", "verify"]


async def create(env, body=None):
    r = await env.client.post("/api/instances", json=body if body is not None else {},
                              headers=XRW)
    assert r.status_code == 200, r.text
    return r.json()


async def test_deploy_full_flow_with_derived_names(env):
    body = await create(env)
    assert body["dns_name"] == "dsh-01.example.test"
    await wait_job(env.app, body["job_id"])

    detail = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert detail["slug"] == "dsh-01"
    assert detail["state"] == "running", detail["last_error"]
    # prefer_hdd：机械盘 nas-1 剩余 10G 也应赢固态 100G
    assert detail["host"]["name"] == "nas-1"
    assert detail["network"]["frp_remote_port"] == 4893
    assert detail["access"]["has_token"] and detail["access"]["token"]
    assert detail["config"]["items"], "配置弹窗必须有数据"
    assert detail["dsh_version"] == "0.9.9"

    journal = "\n".join(env.journal + env.providers.executor.commands)
    assert "dns.create dsh-01 203.0.113.10" in journal
    # 派生命名（§6.1.1）：隧道 dsh_NN，upstream DSH_NN，server_name 完整域名
    assert "frp.ensure dsh_01 remote=4893 local=192.0.2.10" in journal
    assert "ng.ensure dsh-01.example.test DSH_01 203.0.113.10:4893" in journal
    assert "new-instance.sh 'dsh-01'" in journal
    assert "deploy.sh 'dsh-01'" in journal
    assert "verify.sh 'dsh-01'" in journal

    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert [st["key"] for st in job["steps"]] == STEP_KEYS_DEPLOY
    assert all(st["state"] == "succeeded" for st in job["steps"])


async def test_explicit_host_id_overrides_pick(env):
    body = await create(env, {"host_id": env.host_ssd})
    await wait_job(env.app, body["job_id"])
    detail = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert detail["host"]["name"] == "host-2"


async def test_slug_increments_and_port_never_reused(env):
    b1 = await create(env)
    await wait_job(env.app, b1["job_id"])
    b2 = await create(env)
    await wait_job(env.app, b2["job_id"])
    d2 = (await env.client.get(f"/api/instances/{b2['instance_id']}")).json()
    assert d2["slug"] == "dsh-02"
    assert d2["network"]["frp_remote_port"] == 4894


async def test_dns_occupied_fails_job_cleanly(env):
    from dsh_control.integrations.base import DnsRecord

    env.providers.dns.existing["dsh-01"] = DnsRecord("rec-x", "dsh-01", "A", "1.2.3.4")
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed"
    assert "DNS" in (job["error"] or "")
    detail = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert detail["state"] == "error"
    # 没建过就不许删：dns.delete 不该出现在日志里
    assert not any("dns.delete" in line for line in env.journal)


async def test_noop_layers_mark_skipped_and_deploy_still_succeeds(noop_env):
    body = await create(noop_env)
    await wait_job(noop_env.app, body["job_id"])
    job = (await noop_env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    by_key = {st["key"]: st for st in job["steps"]}
    for key in ("dns_create", "frp_create", "ng_create"):
        assert by_key[key]["state"] == "skipped", key
        assert ("人工" in by_key[key]["detail"] or "noop" in by_key[key]["detail"])
    detail = (await noop_env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert detail["state"] == "running"
    # 硬约束：noop 的合成占位 id（"manual" / 0）绝不允许进实例登记列——
    # 否则释放会拿假 id 调真删除，把「只删登记过的 id」防线污染成「假 id 也算登记」。
    assert detail["network"]["dns_record_id"] in ("", None)
    assert detail["network"]["frp_proxy_id"] in ("", None)
    from dsh_control.db.models import Instance

    async with noop_env.db.session() as s:
        inst = await s.get(Instance, body["instance_id"])
        assert inst.ng_server_id is None and inst.ng_location_id is None
        assert inst.ng_upstream_name in ("", None)
    # journal 断言用 startswith：noop 分支也会调用 noop 写方法生成人工清单，
    # 记录行带 "noop." 前缀（真实建对象才是不带前缀的 dns.create 等）。
    assert not any(ln.startswith(("dns.create", "frp.ensure", "ng.ensure"))
                   for ln in noop_env.journal)


async def test_logs_incremental_after_cursor(env):
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    r = await env.client.get(f"/api/jobs/{body['job_id']}/logs", params={"after": 0})
    page1 = r.json()
    assert page1["items"] and page1["next"] > 0
    r = await env.client.get(f"/api/jobs/{body['job_id']}/logs",
                             params={"after": page1["next"]})
    page2 = r.json()
    assert page2["items"] == []
    # 半程游标：从中间重放不丢不重
    mid = page1["items"][len(page1["items"]) // 2]["id"]
    r = await env.client.get(f"/api/jobs/{body['job_id']}/logs",
                             params={"after": mid})
    rest = r.json()["items"]
    assert [it["id"] for it in rest] == [it["id"] for it in page1["items"]
                                         if it["id"] > mid]


async def test_sse_stream_ends_with_done(env):
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    r = await env.client.get(f"/api/jobs/{body['job_id']}/stream")
    text = r.text
    assert "event: step" in text
    assert "event: log" in text
    assert "event: done" in text
    assert '"state": "succeeded"' in text or '"state":"succeeded"' in text


async def test_list_lean_and_searchable(env):
    b1 = await create(env)
    await wait_job(env.app, b1["job_id"])
    r = await env.client.get("/api/instances")
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    for lean_gone in ("public_url", "last_error", "token", "image_tag", "created_at"):
        assert lean_gone not in it
    assert it["slug"] == "dsh-01" and it["state"] == "running"
    assert it["dsh_version"] == "0.9.9" and it["config_version"] == "v1"
    # 域名搜索
    r = await env.client.get("/api/instances", params={"q": "dsh-01"})
    assert len(r.json()["items"]) == 1
    r = await env.client.get("/api/instances", params={"q": "不存在"})
    assert r.json()["items"] == []


async def test_patch_notes_only(env):
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    iid = body["instance_id"]
    r = await env.client.patch(f"/api/instances/{iid}",
                               json={"owner_label": "老婆用", "note": "先开着"},
                               headers=XRW)
    assert r.status_code == 200
    d = r.json()
    assert d["owner_label"] == "老婆用" and d["note"] == "先开着"
    # slug / 域名不可改
    assert d["slug"] == "dsh-01"
    r = await env.client.patch(f"/api/instances/{iid}", json={"note": "x"})
    assert r.status_code == 403  # PATCH 是写操作：不带 XRW 直接拒
    r = await env.client.patch(f"/api/instances/{iid}", json={"slug": "hacked"},
                               headers=XRW)
    assert r.status_code == 200  # 未声明字段被 schema 忽略
    assert (await env.client.get(f"/api/instances/{iid}")).json()["slug"] == "dsh-01"
