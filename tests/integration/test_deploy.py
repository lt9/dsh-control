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
    # 详情只给 mask：完整 token 与含 token 的 url 都不许出现在 detail 里
    assert detail["access"]["has_token"]
    assert detail["access"]["token_masked"]
    assert "token" not in detail["access"] and "url" not in detail["access"]
    assert detail["config"]["items"], "配置弹窗必须有数据"
    assert detail["dsh_version"] == "0.9.9"

    journal = "\n".join(env.journal + env.providers.executor.commands)
    assert "dns.create dsh-01 203.0.113.10" in journal
    # 派生命名（§6.1.1）：隧道 dsh_NN，upstream DSH_NN，server_name 完整域名
    assert "frp.ensure dsh_01 remote=4893 local=192.0.2.10" in journal
    # NG 回源必须用站点声明的 frps_upstream_host（198.51.100.9），不是 ingress_ip
    assert "ng.ensure dsh-01.example.test DSH_01 198.51.100.9:4893" in journal
    # kit 的接口：new-instance.sh <名字> <宿主机端口>，端口是第 2 个位置参数
    assert "new-instance.sh dsh-01 4893" in journal
    assert "deploy.sh dsh-01" in journal
    assert "verify.sh 'dsh-01'" in journal

    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert [st["key"] for st in job["steps"]] == STEP_KEYS_DEPLOY
    assert all(st["state"] == "succeeded" for st in job["steps"])
    # plan 步骤要把「NG 用哪个地址回源」打印给运维看（在步骤日志流里）
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs",
                                 params={"after": 0})).json()["items"]
    assert any("198.51.100.9" in it["line"] for it in logs)


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
                               json={"owner_label": "家人用", "note": "先开着"},
                               headers=XRW)
    assert r.status_code == 200
    d = r.json()
    assert d["owner_label"] == "家人用" and d["note"] == "先开着"
    # slug / 域名不可改
    assert d["slug"] == "dsh-01"
    r = await env.client.patch(f"/api/instances/{iid}", json={"note": "x"})
    assert r.status_code == 403  # PATCH 是写操作：不带 XRW 直接拒
    r = await env.client.patch(f"/api/instances/{iid}", json={"slug": "hacked"},
                               headers=XRW)
    assert r.status_code == 200  # 未声明字段被 schema 忽略
    assert (await env.client.get(f"/api/instances/{iid}")).json()["slug"] == "dsh-01"


async def test_missing_frps_upstream_host_fails_in_plan(env):
    """⑧修正：回源地址不许猜、不许回落 ingress_ip——缺失时 plan 步骤直接失败。"""
    env.settings.frps_upstream_host = ""
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed"
    assert "DSHC_FRPS_UPSTREAM_HOST" in (job["error"] or "")
    detail = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert detail["state"] == "error"
    # 失败发生在 plan：没有任何外部对象被创建
    assert not any(ln.startswith(("dns.create", "frp.ensure", "ng.ensure"))
                   for ln in env.journal)


async def test_failed_deploy_can_be_retried_and_resumes(env):
    """开通失败必须有路可走：修好成因后重跑是**续建**，不重建已经对的东西。"""
    env.settings.frps_upstream_host = ""
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    iid = body["instance_id"]
    await wait_job(env.app, body["job_id"])
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "error"
    # 失败态必须给"重跑开通"入口，否则实例永久卡死在 error
    assert d["actions"] == {"restart": True, "release": True, "deploy": True}

    env.settings.frps_upstream_host = "198.51.100.9"
    r2 = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    assert r2.status_code == 200, r2.text
    await wait_job(env.app, r2.json()["job_id"])
    d2 = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d2["state"] == "running", d2["last_error"]
    assert d2["actions"]["deploy"] is False, "跑通了就不该再给重跑入口"
    # 续建幂等：外部对象各只建一次
    assert sum(1 for ln in env.journal if ln.startswith("dns.create")) == 1


async def test_deploy_retry_refused_from_running(env):
    """running 不等于"开通失败"，重跑开通必须拒——那是 restart 的语义。"""
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    r2 = await env.client.post(f"/api/instances/{body['instance_id']}/deploy", headers=XRW)
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "cannot_deploy_from_running"


async def test_replace_refused_when_drift_would_wash_a_live_site(env):
    """库里脏+盘净（db_dirty）＝ replace 会把用户别的服务洗坏：第 50 步必须
    **不做 replace** 就中止，并把已建对象回滚干净（红线：不拿他的服务换我一次开通）。"""
    env.providers.ng.drift = [{"server_name": "someone-else.example.test",
                               "header_host": "$host:$proxy_port", "probe_status": 401,
                               "dsh_proven": True, "verdict": "db_dirty"}]
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed"
    assert "洗坏" in (job["error"] or "") and "replace" in (job["error"] or "")
    by_key = {s["key"]: s for s in job["steps"]}
    assert by_key["ng_create"]["state"] == "failed"
    assert env.providers.ng.reloads == 0, "体检没过却仍然 replace 了"
    # 扫的是登记在册的实例域名（含本次这台），不是全面板
    assert body["dns_name"] in env.providers.ng.drift_known
    # 回滚不留垃圾：站点、upstream 都不在面板里
    assert await env.providers.ng.find_site(body["dns_name"]) is None
    assert env.providers.ng.upstreams == {}


async def test_dirty_both_warns_in_log_but_deploy_continues(env):
    """库盘同脏（站点此刻公网被准入墙拦着）与本次无关：提醒一句，照建不误。"""
    env.providers.ng.drift = [{"server_name": "office.example.test",
                               "header_host": "$host:$proxy_port", "probe_status": 403,
                               "dsh_proven": True, "verdict": "dirty_both"}]
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert env.providers.ng.reloads == 1
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs?after=0")).json()
    lines = logs["items"] if isinstance(logs, dict) and "items" in logs else logs
    text = " ".join(x["line"] for x in lines)
    assert "体检提醒" in text and "office.example.test" in text


async def test_retry_after_reload_failure_does_not_duplicate_config_snapshot(env):
    """现网形状的第一次失败：DNS/隧道/站点都建好了，第 50 步 reload 与面板并发写撞车。
    此时配置快照已提交，续建从第 10 步重跑必须**覆盖**同版本快照，不能再 INSERT——
    2026-10-21 dsh-01 实测崩在 duplicate key instance_configs_instance_id_version_key。"""
    from sqlalchemy import select

    from dsh_control.db.models import InstanceConfig

    env.providers.ng.fail_reload = True
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    body = r.json()
    iid = body["instance_id"]
    await wait_job(env.app, body["job_id"])
    assert (await env.client.get(f"/api/instances/{iid}")).json()["state"] == "error"
    async with env.db.session() as s:
        first = (await s.scalars(select(InstanceConfig)
                                 .where(InstanceConfig.instance_id == iid))).all()
        assert len(first) == 1, "第一次开通就该留下一份配置快照"

    env.providers.ng.fail_reload = False
    r2 = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    assert r2.status_code == 200, r2.text
    await wait_job(env.app, r2.json()["job_id"])
    d2 = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d2["state"] == "running", d2["last_error"]
    async with env.db.session() as s:
        rows = (await s.scalars(select(InstanceConfig)
                                .where(InstanceConfig.instance_id == iid))).all()
        assert len(rows) == 1, f"续建把配置快照插重了：{len(rows)} 行"


async def test_host_prepare_creates_missing_data_root(env):
    """数据根目录不存在时由控制台 mkdir -p：不该让人先手动建目录才能开通。
    现网 host-109 的 /home/twan/dsh-data 就是这么把第一次开通卡住的。"""
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    cmds = env.providers.executor.commands
    assert any("mkdir -p '/hdd1/dsh-data'" in c for c in cmds), \
        "预检没创建数据根目录，第 60 步挂载会失败"
    assert any("test -w '/hdd1/dsh-data'" in c for c in cmds), "没校验目录可写"
