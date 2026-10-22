"""直连主机（§6.1.4）：第 40 步 skipped、本机端口实探、NG 回源 ssh_host、
MEM/CPU 配额改写、direct 的释放与恢复。"""

from __future__ import annotations

from dsh_control.db import repo
from dsh_control.integrations.base import ExecResult, Tunnel
from tests.conftest import XRW, create_instance, wait_job


async def deploy_on_direct(env, **extra) -> dict:
    r = await create_instance(env.client, extra={"host_id": env.host_nas, **extra})
    body = r.json()
    await wait_job(env.app, body["job_id"])
    return body


async def steps(env, job_id: int) -> dict[str, dict]:
    job = (await env.client.get(f"/api/jobs/{job_id}")).json()
    assert job["state"] == "succeeded", job["error"]
    return {st["key"]: st for st in job["steps"]}


async def test_direct_deploy_skips_tunnel_and_upstreams_to_host(direct_env, env):
    body = await deploy_on_direct(env)
    iid = body["instance_id"]
    steps_ = await steps(env, body["job_id"])
    # 第 40 步整步 skipped，detail 说清为什么不建
    assert steps_["frp_create"]["state"] == "skipped"
    assert "direct 主机不建隧道（§6.1.4）" in steps_["frp_create"]["detail"]
    # 全程没碰 frp-panel：没有 ensure、面板账本空空
    assert not [ln for ln in env.journal if ln.startswith("frp.ensure")], env.journal
    assert env.providers.frp.tunnels == {}
    # NG upstream 后端 = 这台机的 ssh_host:公网端口（不是入口机 198.51.100.9）
    ensures = [ln for ln in env.journal if ln.startswith("ng.ensure")]
    assert ensures, env.journal
    assert " 192.0.2.10:" in ensures[-1], ensures[-1]
    assert "198.51.100.9" not in ensures[-1]
    # 防火墙提醒落在第 50 步 detail，点名只放行入口机
    assert "防火墙" in steps_["ng_create"]["detail"]
    assert "203.0.113.10" in steps_["ng_create"]["detail"]

    async with env.db.session() as s:
        inst = await repo.get_instance(s, iid)
        assert inst.frp_proxy_id is None
        # 端口就是池子起点：假机 ss 没人听、库里没有同机实例
        assert inst.frp_remote_port == env.settings.frp_port_start
        assert inst.container_port == inst.frp_remote_port
    # 端口占用探测走的是本机 ss，不是 frp-panel
    assert any("ss -ltn" in c for c in env.providers.executor.commands)
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["network"]["upstream_mode"] == "direct"
    assert d["network"]["frp_proxy_id"] is None
    assert d["state"] == "running", d["last_error"]


async def test_direct_upstream_mode_visible_on_frp_host(env):
    """frp 主机零变化 + 详情页给出 upstream_mode 供前端区分展示。"""
    r = await create_instance(env.client)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    d = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert d["network"]["upstream_mode"] == "frp"
    assert any(ln.startswith("frp.ensure") for ln in env.journal)
    ensures = [ln for ln in env.journal if ln.startswith("ng.ensure")]
    assert " 198.51.100.9:" in ensures[-1], ensures[-1]


async def test_direct_port_probe_uses_local_ss_not_panel(direct_env, env):
    """占用探测在目标机上做：ss 报 20001 在听就跳 20002；
    frp-panel 说 20002 被占不算数——direct 不问它。"""
    env.providers.executor.handlers.insert(
        0, ("ss -ltn", ExecResult(0, "22\n80\n20001\n", "")))
    env.providers.frp.taken.add(20002)
    env.providers.frp.tunnels[777] = Tunnel(proxy_id=777, name="other_x",
                                            client_id="office-frpc@1",
                                            server_id="s", remote_port=20002)
    body = await deploy_on_direct(env)
    async with env.db.session() as s:
        inst = await repo.get_instance(s, body["instance_id"])
        assert inst.frp_remote_port == 20002, "本机实听让位、面板的谎话不信"
    assert env.providers.frp.tunnels.keys() == {777}   # 没向面板建任何东西


async def test_direct_second_instance_avoids_sibling_port(direct_env, env):
    """库里同机实例在用的端口是占用事实的另一半。"""
    first = await deploy_on_direct(env)
    async with env.db.session() as s:
        a = await repo.get_instance(s, first["instance_id"])
        port_a = a.frp_remote_port
    second = await deploy_on_direct(env)
    async with env.db.session() as s:
        b = await repo.get_instance(s, second["instance_id"])
        assert b.frp_remote_port == port_a + 1


async def test_direct_release_skips_frp_and_restore_reclaims_port(direct_env, env):
    """释放对 direct 无需改动：proxy_id 为空自然 skipped；恢复原端口原回源回来。"""
    body = await deploy_on_direct(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    job = (await env.client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    assert job["state"] == "succeeded", job["error"]
    frp_step = next(st for st in job["steps"] if st["key"] == "frp_delete")
    assert frp_step["state"] == "skipped"
    assert "无 frp 隧道登记" in frp_step["detail"]
    assert not [ln for ln in env.journal if "frp.delete" in ln]
    kinds = {e["kind"] for e in
             (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]}
    assert "release_frp_deleted" not in kinds

    # 恢复：赎回原端口（假机此刻没人在听），回源指回 ssh_host:原端口
    port = await _port(env, iid)
    r = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    assert (await _port(env, iid)) == port
    d = (await env.client.get(f"/api/instances/{iid}")).json()
    assert d["state"] == "running", d["last_error"]
    ensures = [ln for ln in env.journal if ln.startswith("ng.ensure")]
    assert f" 192.0.2.10:{port}" in ensures[-1], ensures[-1]


async def _port(env, iid: int) -> int:
    async with env.db.session() as s:
        return int((await repo.get_instance(s, iid)).frp_remote_port)


async def test_direct_restore_taken_over_by_other_falls_back(direct_env, env):
    """恢复时原端口被**别家**进程占了（ss 在听、docker 映射对不上自己）：
    回落挑新端口并 event 说破。"""
    body = await deploy_on_direct(env)
    iid = body["instance_id"]
    r = await env.client.post(f"/api/instances/{iid}/release",
                              json={"confirm": "dsh-01"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    env.providers.executor.handlers.insert(
        0, ("ss -ltn", ExecResult(0, "20001\n", "")))
    r = await env.client.post(f"/api/instances/{iid}/deploy", headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    assert await _port(env, iid) == 20002
    evs = (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]
    fb = next(e for e in evs if e["kind"] == "frp_port_fallback")
    assert fb["data"] == {"saved_port": 20001, "new_port": 20002, "mode": "direct"}


async def test_direct_rerun_keeps_port_when_own_container_holds_it(direct_env, env):
    """running 直连机重跑 deploy（切镜像）：ss 里那 20001 就是自家容器在听
    （docker 端口映射对得上）——照常赎回，不许自己吓自己换端口。"""
    body = await deploy_on_direct(env)
    iid = body["instance_id"]
    env.providers.executor.handlers.insert(
        0, ("ss -ltn", ExecResult(0, "20001\n", "")))
    env.providers.executor.handlers.insert(
        0, ("HostConfig.PortBindings", ExecResult(0, "20001 \n", "")))
    r = await env.client.post(f"/api/instances/{iid}/deploy",
                              json={"image": "dsh-test:v12"}, headers=XRW)
    await wait_job(env.app, r.json()["job_id"])
    assert (await env.client.get(f"/api/jobs/{r.json()['job_id']}")).json()["state"] \
        == "succeeded"
    assert await _port(env, iid) == 20001
    kinds = {e["kind"] for e in
             (await env.client.get(f"/api/instances/{iid}/events")).json()["items"]}
    assert "frp_port_fallback" not in kinds


async def test_direct_mem_cpu_limits_written_to_env(direct_env, env):
    """direct_env 挂了 mem_limit=1g / cpu_limit=2：第 60 步改写实例 .env，
    沿用 DSH_IMAGE 的 if-grep-sed-else-printf 写法。"""
    body = await deploy_on_direct(env)
    await steps(env, body["job_id"])
    patch = [c for c in env.providers.executor.commands if "f=/srv/dsh-family" in c]
    assert patch, env.providers.executor.commands
    script = "\n".join(patch)
    assert 'sed -i "s|^MEM_LIMIT=.*|MEM_LIMIT=1g|"' in script
    assert 'sed -i "s|^CPU_LIMIT=.*|CPU_LIMIT=2|"' in script
    # 回显白名单带上这两行，日志里看得见落没落
    assert "MEM_LIMIT|CPU_LIMIT)=" in script or "MEM_LIMIT|CPU_LIMIT" in script


async def test_limits_written_on_frp_host_too_and_absent_by_default(env):
    """配额是主机属性不分模式：frp 主机设了照样写；不设一行都不出现（吃 kit 默认）。"""
    async with env.db.session() as s:
        h = await repo.get_host(s, env.host_nas)
        h.mem_limit = "512m"
        await s.commit()
    r = await create_instance(env.client, extra={"host_id": env.host_nas})
    body = r.json()
    await wait_job(env.app, body["job_id"])
    script = "\n".join(c for c in env.providers.executor.commands if "instances/" in c)
    assert "MEM_LIMIT=512m" in script
    assert "CPU_LIMIT=" not in script, "没登记 cpu_limit 就不许写这一行"


async def test_junk_limit_value_is_refused_not_injected(direct_env, env):
    """配额值只认安全字符集：含分号引号的一律跳过写入并 warn，绝不进 shell。"""
    async with env.db.session() as s:
        h = await repo.get_host(s, env.host_nas)
        h.mem_limit = '1g"; reboot'
        await s.commit()
    body = await deploy_on_direct(env)
    await steps(env, body["job_id"])
    script = "\n".join(c for c in env.providers.executor.commands if "instances/" in c)
    assert "MEM_LIMIT" not in script, "脏值一个字符都不许摸到目标机"
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs")).json()["items"]
    assert any("含意外字符" in ln["line"] for ln in logs)


async def test_direct_backend_repoints_origin_and_bind(direct_env, env):
    """云轻量服务器（SWAS 这类，2026-10-22 dsh-05 现网教训）：EIP 是云端 NAT 映射
    不在网卡上，入口机=本机时拨 EIP 回源是 hairpin 黑洞。登记 direct_backend 后：
    第 50 步回源拨登记址而不是 ssh_host；第 60 步把 HOST_BIND_IP 写进实例 .env，
    端口只绑登记址，公网够不到。"""
    async with env.db.session() as s:
        h = await repo.get_host(s, env.host_nas)
        h.direct_backend = "198.51.100.77"
        await s.commit()
    body = await deploy_on_direct(env)
    steps_ = await steps(env, body["job_id"])
    ensures = [ln for ln in env.journal if ln.startswith("ng.ensure")]
    assert ensures and " 198.51.100.77:" in ensures[-1], ensures[-1]
    assert " 192.0.2.10:" not in ensures[-1], "登记了回源址就不许再拨 EIP"
    assert "hairpin" in steps_["ng_create"]["detail"]
    script = "\n".join(c for c in env.providers.executor.commands
                       if "f=/srv/dsh-family" in c)
    assert "HOST_BIND_IP=198.51.100.77" in script


async def test_direct_backend_junk_skips_bind_but_keeps_origin(direct_env, env):
    """direct_backend 过不了字符白名单：HOST_BIND_IP 一行不写（绝不进 shell）并
    warn；NG 那侧走 API 不经 shell，回源照用登记址。"""
    async with env.db.session() as s:
        h = await repo.get_host(s, env.host_nas)
        h.direct_backend = '198.51.100.77; reboot'
        await s.commit()
    body = await deploy_on_direct(env)
    await steps(env, body["job_id"])
    script = "\n".join(c for c in env.providers.executor.commands if "instances/" in c)
    assert "HOST_BIND_IP=198.51.100.77" not in script, "脏值一个字符都不许摸到目标机"
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs")).json()["items"]
    assert any("direct_backend" in ln["line"] and "意外字符" in ln["line"] for ln in logs)


async def test_host_llm_base_url_overrides_site_default(direct_env, env):
    """按主机的模型入口覆盖：站点 llm_base_url 是局域网网关，云上机器拨不到；
    登记了就换地址（密钥不动，同一网关的门不同）。"""
    default = env.settings.llm_base_url
    body = await deploy_on_direct(env)
    await steps(env, body["job_id"])
    cmds = [c for c in env.providers.executor.commands if "put LLM_BASE_URL" in c]
    assert cmds and default in cmds[-1], "未登记覆盖时吃站点默认"
    async with env.db.session() as s:
        h = await repo.get_host(s, env.host_nas)
        h.llm_base_url = "http://198.51.100.77:7777/v1"
        await s.commit()
    # 第二次开通：假容器的 dump 也换成覆盖值，否则安全门（第 90 步）会先拦下
    from tests.conftest import SAFE_DUMP
    env.providers.executor.handlers.insert(
        0, ("dump-config", ExecResult(
            0, SAFE_DUMP.replace(default, "http://198.51.100.77:7777/v1"), "")))
    body = await deploy_on_direct(env)
    await steps(env, body["job_id"])
    last = [c for c in env.providers.executor.commands if "put LLM_BASE_URL" in c][-1]
    assert "http://198.51.100.77:7777/v1" in last
    assert f"put LLM_BASE_URL {default}" not in last, "覆盖值必须顶掉站点默认"
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs")).json()["items"]
    assert any("模型入口覆盖" in ln["line"] for ln in logs)


OVERRIDE_LLM = "http://198.51.100.77:7777/v1"


async def test_safety_gate_follows_host_override(direct_env, env):
    """安全门的期望端点跟着主机覆盖走：实例真实端点=库里登记的覆盖值就放行
    （人登记的自家门，不是漂移），log 里点名这是覆盖。"""
    from tests.conftest import SAFE_DUMP
    dump = SAFE_DUMP.replace(env.settings.llm_base_url, OVERRIDE_LLM)
    env.providers.executor.handlers.insert(
        0, ("dump-config", ExecResult(0, dump, "")))
    async with env.db.session() as s:
        h = await repo.get_host(s, env.host_nas)
        h.llm_base_url = OVERRIDE_LLM
        await s.commit()
    body = await deploy_on_direct(env)
    steps_ = await steps(env, body["job_id"])
    assert steps_["verify"]["state"] == "succeeded"
    logs = (await env.client.get(f"/api/jobs/{body['job_id']}/logs")).json()["items"]
    assert any("主机登记的入口覆盖" in ln["line"] for ln in logs)


async def test_endpoint_matching_neither_site_nor_override_fails_gate(direct_env, env):
    """两头都对不上（容器端点=站点默认、主机登记了别的覆盖）：安全门照拦——
    覆盖是给登记开的口子，不是给漂移开的。"""
    async with env.db.session() as s:
        h = await repo.get_host(s, env.host_nas)
        h.llm_base_url = OVERRIDE_LLM
        await s.commit()
    body = await deploy_on_direct(env)  # 假容器 dump 里还是站点默认值
    job = (await env.client.get(f"/api/jobs/{body['job_id']}")).json()
    assert job["state"] == "failed", job
    assert "安全门" in (job.get("error") or "")
