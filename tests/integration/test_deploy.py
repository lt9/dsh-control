"""deploy 全流程：服务端选主机与 slug、派生命名、noop 三态、日志增量、SSE。"""

from __future__ import annotations

from tests.conftest import XRW, create_instance, wait_job

STEP_KEYS_DEPLOY = ["plan", "host_prepare", "dns_create", "frp_create", "ng_create",
                    "container_deploy", "config_backfill", "capabilities", "token_capture",
                    "probe_public", "verify"]


async def create(env, body=None):
    slug = (await env.client.get("/api/instances/next-slug")).json()["slug"]
    body = {"confirm": slug, **({} if body is None else body)}
    r = await env.client.post("/api/instances", json=body,
                              headers=XRW)
    assert r.status_code == 200, r.text
    return r.json()


async def test_create_requires_echoed_slug(env):
    """没回显域名就 POST：422，而且库里一个实例都不许有。

    前端的"打字确认"被真双击穿透过一次（2026-10-21 现网凭空开出 dsh-03，
    DNS/隧道/反代/容器全建了）。确认必须落到服务端，这条钉住它。"""
    r = await env.client.post("/api/instances", json={}, headers=XRW)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "confirm_mismatch"
    assert (await env.client.get("/api/instances")).json()["items"] == []


async def test_create_rejects_stale_slug(env):
    """确认卡上的名字和真要建的不一致：拒绝，而不是"建成别的"。"""
    r = await env.client.post("/api/instances", json={"confirm": "dsh-09"}, headers=XRW)
    assert r.status_code == 422
    assert (await env.client.get("/api/instances")).json()["items"] == []
    # 空白宽容，但不允许错字
    r = await env.client.post("/api/instances", json={"confirm": " dsh-01 "}, headers=XRW)
    assert r.status_code == 200


async def test_next_slug_tracks_existing_instances(env):
    """取号接口是确认卡唯一的名字来源，必须跟着库里已有的 slug 走。"""
    assert (await env.client.get("/api/instances/next-slug")).json()["slug"] == "dsh-01"
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    assert (await env.client.get("/api/instances/next-slug")).json()["slug"] == "dsh-02"


async def test_container_port_never_laundered_from_host_port(env):
    """docker 映射读不到时，宁可显示 0 + 标明来源，也不许把 .env 的 HOST_PORT
    洗成"容器内监听端口"——那是第 27 个缺陷的本体：详情页曾把 4895 标在容器上。"""
    from dsh_control.integrations.base import ExecResult

    env_txt = "HOST_PORT=4895\nDATA_DIR=/mnt/hdd2/dsh-family/dsh-family/dsh-01/data\n"
    env.providers.executor.handlers.insert(
        0, ("cat /srv/dsh-family/instances/dsh-01/.env", ExecResult(0, env_txt, "")))
    env.providers.executor.handlers.insert(
        0, ("HostConfig.PortBindings", ExecResult(1, "", "permission denied")))
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    kv = {i["key"]: i["value"] for i in d["config"]["items"]}
    assert str(kv["CONTAINER_PORT"]) != "4895", kv
    assert "没读到 docker 端口映射" in str(kv["CONFIG_SOURCE"]), kv.get("CONFIG_SOURCE")


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
    assert "verify.sh dsh-01" in journal
    # 安全门是控制台自己算的：读容器生效配置，不看 kit 脚本的脸色
    assert "dsh web --dump-config" in journal

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
    assert it["dsh_version"] == "0.9.9" and it["config_version"] == "v2"
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
    r = await create_instance(env.client)
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
    r = await create_instance(env.client)
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
    r = await create_instance(env.client)
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
    r = await create_instance(env.client)
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
    现网有一台主机的数据根目录被人放在了家目录下，第一次开通就是这么卡住的。"""
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    cmds = env.providers.executor.commands
    assert any("mkdir -p '/hdd1/dsh-data'" in c for c in cmds), \
        "预检没创建数据根目录，第 60 步挂载会失败"
    assert any("test -w '/hdd1/dsh-data'" in c for c in cmds), "没校验目录可写"


async def test_retry_adopts_dns_record_pointing_at_our_ingress(env):
    """现网形状：上次建到第 60 步失败，DNS/隧道/站点留在外面（按契约不回滚），
    而库里的登记 id 被当时刻板式的"通吃回滚"抹掉过。续建要按「记录值 == 本站入口」
    认出自己的解析并复用，不能判成别人占用把实例永久锁死在 error。"""
    from dsh_control.db import repo

    body = await create(env)
    await wait_job(env.app, body["job_id"])
    iid = body["instance_id"]
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.dns_record_id, "第一次开通应把 RecordId 登记进库"
        inst.state = "error"        # 模拟第 60 步失败后的现场
        inst.dns_record_id = ""     # 模拟旧回滚把登记抹了（缺陷本身）
        await s.commit()

    r = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    assert r.status_code == 200, r.text
    await wait_job(env.app, r.json()["job_id"])
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "running", d["last_error"]
    assert sum(1 for ln in env.journal if ln.startswith("dns.create")) == 1, "复用了还重复建"
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.dns_record_id, "复用之后要把 RecordId 挂回库里"
    events = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    assert any("复用" in e["message"] for e in events), "复用这条判断必须在事件里看得见"


async def test_failed_retry_keeps_previous_attempts_registrations(env):
    """一次失败的**续建**只许抹它自己建的东西：上一次尝试留下的 DNS/隧道/站点
    还活着，登记 id 一旦被抹就成了孤儿——页面看不见、释放删不掉。"""
    from dsh_control.db import repo

    body = await create(env)
    await wait_job(env.app, body["job_id"])
    iid = body["instance_id"]
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        keep = (inst.dns_record_id, inst.frp_proxy_id, inst.frp_remote_port,
                inst.ng_server_id, inst.ng_upstream_id)
        inst.state = "error"
        await s.commit()

    env.settings.frps_upstream_host = ""        # 让这次续建在第 10 步就失败
    r = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    assert r.status_code == 200, r.text
    await wait_job(env.app, r.json()["job_id"])
    assert (await env.client.get(f"/api/instances/{iid}")).json()["state"] == "error"
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert (inst.dns_record_id, inst.frp_proxy_id, inst.frp_remote_port,
                inst.ng_server_id, inst.ng_upstream_id) == keep, "失败的续建抹掉了上次的登记"
    assert env.providers.dns.records and env.providers.frp.tunnels and env.providers.ng.sites
    logs = (await env.client.get(f"/api/jobs/{r.json()['job_id']}/logs?after=0")).json()["items"]
    assert any("外部登记 id 保持不动" in it["line"] for it in logs), "要说明为什么没清"


async def _leftover_tunnel(env, port: int = 4894):
    """把「上一次尝试建好的隧道」摆回面板：名字是我们的，端口被它占着。"""
    from sqlalchemy import select

    from dsh_control.db.models import Host
    from dsh_control.integrations.base import Tunnel

    async with env.db.session() as s:
        host = (await s.scalars(select(Host).order_by(Host.id))).first()
    t = Tunnel(proxy_id=777, name="dsh_01", client_id=host.frpc_client_id,
               server_id="default", local_ip=host.ssh_host, local_port=port,
               remote_port=port)
    env.providers.frp.tunnels[777] = t
    env.providers.frp.taken.add(port)
    return t


async def test_retry_reuses_our_existing_tunnel_and_its_port(env):
    """续建认得自己的隧道：沿用它的端口，不重复建，也不被「拒绝改动既有隧道」挡死。"""
    from dsh_control.db import repo

    await _leftover_tunnel(env)
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    assert not [ln for ln in env.journal if ln.startswith("frp.ensure")], "复用了还新建"
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    net = d["network"]
    assert net["frp_proxy_id"] == 777 and net["frp_remote_port"] == 4894
    evs = (await env.client.get(f"/api/instances/{body['instance_id']}/events")).json()["items"]
    assert any(e["kind"] == "frp_reused" for e in evs)
    async with env.db.session() as s:
        inst = await repo.get_instance(s, body["instance_id"])
        assert inst.frp_proxy_id == 777


async def test_rollback_of_retry_leaves_reused_tunnel_alone(env):
    """复用来的隧道不是这次建的：后续步骤失败触发回滚时，它既不能被删，
    登记 id 也不能被抹（抹了就成了没人认领的孤儿隧道）。"""
    from dsh_control.db import repo

    await _leftover_tunnel(env)
    env.providers.ng.fail_reload = True          # 第 50 步炸，触发回滚
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    iid = body["instance_id"]
    assert 777 in env.providers.frp.tunnels, "回滚删掉了复用的隧道"
    assert env.providers.ng.sites == {} and env.providers.dns.records == {}
    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.frp_proxy_id == 777, "复用的登记被抹掉了，隧道从此没人认领"
        assert inst.frp_remote_port == 4894
        assert inst.dns_record_id == "", "这次真的建过的 DNS 才该被抹"


async def test_llm_key_travels_by_stdin_never_in_command(env):
    """模型密钥不许出现在命令行：命令行会进任务日志，也在目标机的 ps 里裸奔，
    而打码正则只认 token/secret/password 这类名字，认不出 LLM_API_KEY。"""
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    ex = env.providers.executor
    patch_cmds = [c for c in ex.commands if "LLM_API_KEY" in c]
    assert patch_cmds, "第 60 步要往实例 .env 写模型配置"
    assert not any("sk-TEST-LLM-KEY" in c for c in ex.commands), "密钥漏进命令行"
    assert not any("sk-TEST-SEARCH" in c for c in ex.commands)
    assert any(s and "sk-TEST-LLM-KEY" in s for s in ex.stdins), "密钥该走 stdin 递过去"
    assert any("put LLM_BASE_URL https://llm.example.test:8000/v1" in c for c in ex.commands)
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs?after=0")).json()["items"]
    assert not any("sk-TEST-LLM-KEY" in it["line"] for it in logs), "日志里不许出现密钥"
    assert any("LLM_API_KEY 已写入" in it["line"] for it in logs)


async def test_missing_llm_credential_fails_at_plan_touching_nothing(env):
    """缺模型密钥要在第 10 步就停：别等 DNS/隧道/站点都建好了才发现容器起不来。"""
    from dsh_control.db import repo

    async with env.db.session() as s:
        cred = await repo.get_credential_by_kind(s, "llm_key")
        await s.delete(cred)
        await s.commit()

    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed"
    assert "llm_key" in (job["error"] or "")
    assert job["steps"][0]["seq"] == 10 and job["steps"][0]["state"] == "failed"
    assert not any(ln.startswith(("dns.create", "frp.ensure", "ng.")) for ln in env.journal), \
        "第 10 步失败前不许碰任何外部系统"


async def test_safety_gate_blocks_when_cloud_path_still_open(env):
    """安全门不过就不许算开通完成——花钱和外传这两条由控制台自己判定，
    不看 kit 校验脚本的脸色（那份在 pipefail 下出过假警报）。"""
    from dsh_control.integrations.base import ExecResult

    unsafe = ("config:\n  providers:\n    local:\n"
              "      baseURL: https://llm.example.test:8000/v1\n"
              "- id: llm-deepseek\n  name: '@x/key'\n  disabled: false\n")
    env.providers.executor.handlers.insert(0, ("dump-config", ExecResult(0, unsafe, "")))

    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed"
    assert "安全门" in (job["error"] or "") and "llm-deepseek" in (job["error"] or "")
    steps = {s["seq"]: s["state"] for s in job["steps"]}
    assert steps[60] == "succeeded" and steps[90] == "failed"
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert d["state"] == "error"
    # 第 60 步之后按契约保留现场：不回滚，DNS/隧道/站点都还在，人可以上去查
    assert env.providers.dns.records and env.providers.frp.tunnels and env.providers.ng.sites
    step90 = next(s for s in job["steps"] if s["seq"] == 90)
    assert "安全门不通过" in (step90["detail"] or ""), "失败原因要落在步骤详情里，页面上看得见"
    evs = (await env.client.get(f"/api/instances/{body['instance_id']}/events")).json()["items"]
    assert any(e["kind"] == "job_failed" and "安全门" in e["message"] for e in evs)


async def test_kit_verify_warnings_are_reported_but_not_fatal(env):
    """kit verify 的提示（比如数据盘在同一块分区）给人看，不拦开通。"""
    from dsh_control.integrations.base import ExecResult

    env.providers.executor.handlers.insert(
        0, ("./scripts/verify.sh", ExecResult(1, "  ✗ 数据目录与系统在同一个盘上\n", "")))
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    evs = (await env.client.get(f"/api/instances/{body['instance_id']}/events")).json()["items"]
    warn = next(e for e in evs if e["kind"] == "verify_warnings")
    assert "数据目录" in warn["message"]


async def test_created_row_already_carries_kit_container_name(env):
    """创建那一刻容器名就得是 dsh-<slug>。留个错值"等第 10 步纠正"，中间任何
    一次重启或抓 token 都指向一个不存在的容器。"""
    from dsh_control.db import repo

    r = await create_instance(env.client)
    assert r.status_code == 200
    async with env.db.session() as s:
        inst = await repo.get_instance(s, r.json()["instance_id"])
        assert inst.container_name == f"dsh-{inst.slug}"
        assert inst.container_name != inst.slug


async def test_safety_gate_does_not_flood_job_log(env):
    """自检读的是整份配置（上千行），不许把它灌进任务日志——页面要"点对点看进度"，
    日志被 dump 淹了，部署过程就没人看得见。"""
    from dsh_control.integrations.base import ExecResult

    big = ("config:\n  providers:\n    local:\n"
           "      baseURL: https://llm.example.test:8000/v1\n"
           + "".join(f"# 填充行 {i}\n" for i in range(1200))
           + "".join(f"- id: {p}\n  disabled: true\n"
                     for p in ("llm-deepseek", "llm-deepseek-account", "deepseek-account",
                               "session-log-deepseek", "plugin-package-inventory-deepseek",
                               "authorization"))
           + "- id: web-deepseek-api\n")
    env.providers.executor.handlers.insert(0, ("dump-config", ExecResult(0, big, "")))
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    assert (await env.client.get(f"/api/jobs/{body['job_id']}")).json()["state"] == "succeeded"
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs?after=0")).json()["items"]
    assert not any("填充行" in it["line"] for it in logs), "配置正文不许进日志"
    assert any("安全门" in it["line"] for it in logs), "但判定结论必须留一行"


async def test_config_snapshot_reports_effective_values(env):
    """详情页的端口与数据目录必须是机器上的事实。第 10 步只能猜：端口只能填 0，
    目录按 data_root 硬拼——现网因此写着 CONTAINER_PORT=0 和一个不存在的路径，
    而她真正的数据在 /mnt/hdd2/dsh-family/dsh-family/dsh-01/data。"""
    from dsh_control.integrations.base import ExecResult

    env_txt = ("HOST_PORT=4895\nPUBLIC_URL=https://dsh-01.example.test\n"
               "DATA_DIR=/mnt/hdd2/dsh-family/dsh-family/dsh-01/data\n"
               "DSH_IMAGE=dsh-family:0.2.1-rc.3\n"
               "LLM_API_KEY=sk-SECRET-不许进快照\n")
    env.providers.executor.handlers.insert(
        0, ("cat /srv/dsh-family/instances/dsh-01/.env", ExecResult(0, env_txt, "")))
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    kv = {i["key"]: i["value"] for i in d["config"]["items"]}
    # 容器内监听端口取自 docker 映射的左半边；4895 是宿主机发布端口
    assert str(kv["CONTAINER_PORT"]) == "8080", kv
    assert str(kv["HOST_PORT"]) == "4895", kv
    assert kv["DOCUMENTS_DIRECTORY"] == "/mnt/hdd2/dsh-family/dsh-family/dsh-01/data"
    assert str(kv["FRP_REMOTE_PORT"]) != "0"
    assert str(kv["CONFIG_SOURCE"]).startswith("生效值"), kv.get("CONFIG_SOURCE")
    blob = str(kv)
    assert "sk-SECRET" not in blob, "读 .env 只取白名单键，密钥不许进快照"
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs?after=0")).json()["items"]
    assert not any("sk-SECRET" in it["line"] for it in logs), "也不许进任务日志"


async def test_config_snapshot_labels_estimates_when_env_unreadable(env):
    """读不到 .env 不该让开通失败（容器已经起来了），但快照必须自报"这是估算"，
    否则详情页把开工前的推测当事实摆着，比空着更坏。"""
    from dsh_control.integrations.base import ExecResult

    env.providers.executor.handlers.insert(
        0, ("cat /srv/dsh-family/instances/dsh-01/.env", ExecResult(1, "", "cat: 不存在")))
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    assert (await env.client.get(f"/api/jobs/{body['job_id']}")).json()["state"] == "succeeded"
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    kv = {i["key"]: i["value"] for i in d["config"]["items"]}
    # docker 端口映射还是读到了，所以是"部分生效值"，但绝不自称完整生效值
    assert str(kv["CONFIG_SOURCE"]) == "部分生效值（没读到实例 .env）", kv.get("CONFIG_SOURCE")
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs?after=0")).json()["items"]
    assert any("估算" in it["line"] for it in logs)


async def test_config_snapshot_says_estimate_when_nothing_readable(env):
    """.env 和 docker 映射都读不到：快照必须自报"这是估算"，不许冒充生效值。"""
    from dsh_control.integrations.base import ExecResult

    env.providers.executor.handlers.insert(
        0, ("cat /srv/dsh-family/instances/dsh-01/.env", ExecResult(1, "", "cat: 不存在")))
    env.providers.executor.handlers.insert(
        0, ("HostConfig.PortBindings", ExecResult(1, "", "permission denied")))
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    kv = {i["key"]: i["value"] for i in d["config"]["items"]}
    assert str(kv["CONFIG_SOURCE"]).startswith("估算"), kv.get("CONFIG_SOURCE")


class _QueueProbe:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def check(self, base_url, *, expect="dsh"):
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


async def test_probe_retries_before_calling_it_degraded(env, monkeypatch):
    """容器刚起来那几秒首页可能还回 401：单次探活会把"还没准备好"读成"坏了"
    （现网 dsh-02 就是 401/unexpected，事后从公网实测一切正常）。"""
    from dsh_control import jobs
    from dsh_control.integrations.base import ProbeResult

    monkeypatch.setattr(jobs.deploy, "_PROBE_GAP", 0)
    bad = ProbeResult(False, 401, "unexpected", hint="首页 401")
    env.providers.probe = _QueueProbe([bad, bad, ProbeResult(True, 401, "ok")])
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    st = next(s for s in job["steps"] if s["key"] == "probe_public")
    assert "共探 3 次" in st["detail"], st["detail"]
    evs = (await env.client.get(f"/api/instances/{body['instance_id']}/events")).json()["items"]
    assert not any(e["kind"] == "probe_degraded" for e in evs), "重试过了就不算降级"


async def test_probe_still_reports_degraded_after_retries(env, monkeypatch):
    """真坏了也不许把开通判死（容器已起、外部对象已建，第 60 步之后不回滚），
    但必须把"公网这条路没验证通过"喊出来，不能悄悄放行。"""
    from dsh_control import jobs
    from dsh_control.integrations.base import ProbeResult

    monkeypatch.setattr(jobs.deploy, "_PROBE_GAP", 0)
    env.providers.probe = _QueueProbe([ProbeResult(False, 502, "unexpected", hint="上游不通")])
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "succeeded"
    st = next(s for s in job["steps"] if s["key"] == "probe_public")
    assert f"共探 {jobs.deploy._PROBE_TRIES} 次" in st["detail"]
    evs = (await env.client.get(f"/api/instances/{body['instance_id']}/events")).json()["items"]
    degraded = next(e for e in evs if e["kind"] == "probe_degraded")
    assert "上游不通" in degraded["message"] or "上游不通" in str(degraded["data"])


async def test_job_payload_carries_instance_linkage(env):
    """工单 API 必须自带实例关联：手敲 URL 打开工单页时没有 ?instance=，
    没有这三个字段，前端就不知道那台已经回收，还会把死掉的一键地址当成能用。"""
    body = await create(env)
    await wait_job(env.app, body["job_id"])
    j = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert j["instance_id"] == body["instance_id"], j
    assert j["instance_slug"] == "dsh-01", j
    assert j["instance_state"] == "running", j
    r = await env.client.post(f"/api/instances/{body['instance_id']}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    j2 = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert j2["instance_state"] == "released", j2
