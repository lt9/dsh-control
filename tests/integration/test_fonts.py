"""开通第 67 步「铺字体」：站点字体库 cp -n 进实例 DATA_DIR 的 home/.fonts。

契约（docs/SITE-CONFIG.md「字体铺放」）：只 cp 不挂载、cp -n 同名不覆盖、
落地后文件 644 / 目录 755；库未配置或宿主上目录不存在 → 记 warning 跳过，
绝不失败工单（先例：生图技能没配就跳过）。DATA_DIR 的口径与第 65 步一致：
优先实例 .env 里的生效值，读不到才按 data_root 拼。
"""

from __future__ import annotations

import re

from dsh_control.integrations.base import ExecResult
from tests.conftest import XRW, create_instance, wait_job

# 铺字体脚本的指纹：目标目录（假主机上不存在第二个用到它的命令）
FONT_MARK = "home/.fonts"
# 防覆盖形态：cp 的参数里必须带 -n（no-clobber），写成 `cp -rn` 也算
NO_CLOBBER = re.compile(r"cp\s+-[a-zA-Z]*n")


def _font_cmds(env) -> list[str]:
    return [c for c in env.providers.executor.commands if FONT_MARK in c]


async def _deploy(env) -> dict:
    r = await create_instance(env.client)
    body = r.json()
    await wait_job(env.app, body["job_id"])
    return body


async def _job(env, job_id: int) -> dict:
    return (await env.client.get(f"/api/jobs/{job_id}")).json()


async def _log_lines(env, job_id: int) -> list[str]:
    items = (await env.client.get(f"/api/jobs/{job_id}/logs",
                                  params={"after": 0})).json()["items"]
    return [it["line"] for it in items]


async def test_font_library_copied_into_data_home_fonts(env):
    """配置了库 → cp 进 <生效 DATA_DIR>/home/.fonts，644/755 权限处理做了，
    步骤落库成功，源目录/目标目录/新增文件数都记进了日志。"""
    env.settings.font_library = "/srv/dsh-fonts/"      # 尾部斜杠要被归一
    env.providers.executor.handlers.insert(
        0, ("cat /srv/dsh-family/instances/dsh-01/.env",
            ExecResult(0, "DATA_DIR=/mnt/kitdata/dsh-family/dsh-01/data\n", "")))
    dst = "/mnt/kitdata/dsh-family/dsh-01/data/home/.fonts"
    env.providers.executor.handlers.insert(
        0, (FONT_MARK, ExecResult(0, f"[fonts] DONE src=/srv/dsh-fonts dst={dst} new=3\n", "")))

    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    st = next(s for s in job["steps"] if s["key"] == "fonts")
    assert st["state"] == "succeeded", st["detail"]

    cmds = _font_cmds(env)
    assert cmds, "配置了字体库却没执行任何铺字体命令"
    cmd = cmds[-1]
    assert "lib=/srv/dsh-fonts" in cmd, cmd                       # 源目录（已去尾斜杠）
    assert f"dst={dst}" in cmd, cmd                               # 目标认 .env 的生效 DATA_DIR
    assert NO_CLOBBER.search(cmd) and "cp" in cmd, cmd            # 防覆盖形态
    assert "chmod 644" in cmd and "chmod 755" in cmd, cmd         # 权限落地
    assert "fc-cache" not in cmd and "mount" not in cmd           # 不跑 fc-cache、不挂载

    assert "本次新增 3 个文件" in (st["detail"] or ""), st["detail"]
    lines = await _log_lines(env, body["job_id"])
    assert any("/srv/dsh-fonts" in ln and dst in ln and "新增 3" in ln for ln in lines), \
        "源目录、目标目录、新增文件数要记进日志"


async def test_font_step_skips_without_library(env):
    """没配库 → 步骤 skipped + 一行 warning，一条铺字体 ssh 都不发，工单整体照常成功。"""
    env.settings.font_library = ""
    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    st = next(s for s in job["steps"] if s["key"] == "fonts")
    assert st["state"] == "skipped", st["detail"]
    assert not _font_cmds(env), "库没配就不该往宿主发铺字体命令"
    lines = await _log_lines(env, body["job_id"])
    assert any("跳过铺字体" in ln and "font_library" in ln for ln in lines)


async def test_font_step_skips_when_library_dir_missing_on_host(env):
    """配了库但目录在实例宿主机上不存在 → 同样 warning + skipped，不失败工单。"""
    env.settings.font_library = "/srv/no-such-fonts"
    env.providers.executor.handlers.insert(
        0, (FONT_MARK, ExecResult(0, "[fonts] LIB_MISSING\n", "")))
    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    st = next(s for s in job["steps"] if s["key"] == "fonts")
    assert st["state"] == "skipped", st["detail"]
    assert "不存在" in (st["detail"] or "")
    lines = await _log_lines(env, body["job_id"])
    assert any("跳过铺字体" in ln and "/srv/no-such-fonts" in ln for ln in lines)


async def test_font_step_copied_new_files_only_when_zero_added(env):
    """库里文件已全在目标里（新增 0）：步骤照样成功，不许当成异常。"""
    env.settings.font_library = "/srv/dsh-fonts"
    env.providers.executor.handlers.insert(
        0, (FONT_MARK, ExecResult(0, "[fonts] DONE src=/srv/dsh-fonts "
                                     "dst=/hdd1/dsh-data/dsh-01/home/.fonts new=0\n", "")))
    body = await _deploy(env)
    job = await _job(env, body["job_id"])
    assert job["state"] == "succeeded", job["error"]
    st = next(s for s in job["steps"] if s["key"] == "fonts")
    assert st["state"] == "succeeded", st["detail"]
    assert "本次新增 0 个文件" in (st["detail"] or "")


async def test_redeploy_relay_keeps_no_clobber_and_does_not_break(env):
    """对已 running 的实例重复部署（redeploy 切镜像场景，§6.1 允许从 running 续建）：
    铺字体要再跑一遍，且仍是 cp -n 防覆盖形态——同名文件一律不动（她的字体她自管）。"""
    env.settings.font_library = "/srv/dsh-fonts"
    body = await _deploy(env)
    assert (await _job(env, body["job_id"]))["state"] == "succeeded"
    first_cmds = _font_cmds(env)
    assert first_cmds and NO_CLOBBER.search(first_cmds[-1]), first_cmds

    env.settings.dsh_image = "dsh-test:v10"          # 切镜像：站点改了目标 tag
    r2 = await env.client.post(f"/api/instances/{body['instance_id']}/deploy", headers=XRW)
    assert r2.status_code == 200, r2.text
    await wait_job(env.app, r2.json()["job_id"])
    job2 = await _job(env, r2.json()["job_id"])
    assert job2["state"] == "succeeded", job2["error"]
    st2 = next(s for s in job2["steps"] if s["key"] == "fonts")
    assert st2["state"] == "succeeded", st2["detail"]

    all_cmds = env.providers.executor.commands
    second_cmds = _font_cmds(env)
    assert len(second_cmds) > len(first_cmds), "重复部署没再执行铺字体"
    assert second_cmds[-1] in all_cmds
    assert all(NO_CLOBBER.search(c) for c in second_cmds), \
        "任何一次铺字体都不许丢掉防覆盖参数"
    d2 = (await env.client.get(f"/api/instances/{body['instance_id']}")).json()
    assert d2["state"] == "running", d2["last_error"]
