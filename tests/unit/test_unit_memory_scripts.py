"""容器侧脚本（memory_hub 生成的 POSIX sh）用真 sh 跑的行为测试。

这些脚本是记忆接线的落盘面：合并写不能碰 records、marker 段删不能吞邻居、
P0 旧段没有段尾记号就不许删。全部在 tmp_path 里用 `sh -c` 真执行——生成的
脚本里全是引号与转义，只看字符串断言等于没测。
"""

from __future__ import annotations

import os
import subprocess

from dsh_control.integrations import memory_hub as mh


def run(script: str, stdin: str = "") -> tuple[int, str]:
    p = subprocess.run(["sh", "-c", script], input=stdin,
                       capture_output=True, text=True, check=False)
    return p.returncode, p.stdout + p.stderr


def creds(tmp, body: str):
    (tmp / ".credentials.yaml").write_text(body, encoding="utf-8")
    return tmp / ".credentials.yaml"


def read(p) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- refs 合并写

def test_合并写_只动我们这一个键(tmp_path):
    f = creds(tmp_path, "version: 1\nrecords:\n  a/b:\n    kind: grant\n"
                        "    payload:\n      secret: s3cr3t\nrefs:\n"
                        "  LLAMA_API_KEY: keepme\n  PROXY_USER_KEY: sk-mem-old1\n")
    rc, out = run(mh.script_refs_merge(str(tmp_path)), "sk-mem-new2222\n")
    assert rc == 0, out
    body = read(f)
    assert "s3cr3t" in body and "LLAMA_API_KEY: keepme" in body
    assert "sk-mem-new2222" in body and "sk-mem-old1" not in body


def test_合并写_幂等重跑键不翻倍(tmp_path):
    f = creds(tmp_path, "refs:\n  PROXY_USER_KEY: sk-mem-a111\n")
    key = "sk-mem-b2222222\n"
    for _ in range(3):
        rc, out = run(mh.script_refs_merge(str(tmp_path)), key)
        assert rc == 0, out
    assert read(f).count("PROXY_USER_KEY") == 1
    assert read(f).count("sk-mem-b2222222") == 1


def test_合并写_没有_refs_段就在文件尾建段(tmp_path):
    f = creds(tmp_path, "version: 1\nrecords: {}\n")
    rc, out = run(mh.script_refs_merge(str(tmp_path)), "sk-mem-c3333333\n")
    assert rc == 0, out
    assert read(f) == "version: 1\nrecords: {}\n\nrefs:\n" \
                      "  PROXY_USER_KEY: sk-mem-c3333333\n"


def test_合并写_文件不存在也建并_600(tmp_path):
    rc, out = run(mh.script_refs_merge(str(tmp_path)), "sk-mem-d4444444\n")
    assert rc == 0, out
    f = tmp_path / ".credentials.yaml"
    assert "PROXY_USER_KEY" in read(f)
    assert oct(os.stat(f).st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------- home 层补丁

def test_补丁铺设_幂等_带成对记号(tmp_path):
    (tmp_path / "cordis.patch.yml").write_text(
        "- id: agent-team\n  config: {}\n", encoding="utf-8")
    script = mh.script_home_patch(str(tmp_path), "http://198.51.100.9:8096/dsh",
                                  model_id="TestModel-1")
    rc, out = run(script)
    assert rc == 0 and "PATCH_WIRED" in out
    body = read(tmp_path / "cordis.patch.yml")
    assert "agent-team" in body and mh.MEMORY_MARKER in body
    assert mh.MEMORY_MARKER_END in body and "baseURL: http://198.51.100.9:8096/dsh" in body
    rc2, out2 = run(script)
    assert rc2 == 0 and "PATCH_ALREADY" in out2
    assert read(tmp_path / "cordis.patch.yml").count(mh.MEMORY_MARKER) == 2  # 起+止


def test_补丁探针_三态读回(tmp_path):
    (tmp_path / "cordis.patch.yml").write_text(
        "# dsh-control-p0: 手工接线\n", encoding="utf-8")
    creds(tmp_path, "refs:\n  PROXY_USER_KEY: x\n")
    rc, out = run(mh.script_patch_state(str(tmp_path)))
    assert rc == 0
    assert mh.parse_patch_state(out) == {"memory": False, "p0": True, "refs": True}


def test_旧记号在场时补丁不重复铺(tmp_path):
    (tmp_path / "cordis.patch.yml").write_text(
        "# dsh-control-p0: 手工接线\n- id: llm-deepseek\n", encoding="utf-8")
    rc, out = run(mh.script_home_patch(str(tmp_path), "http://x.test/dsh",
                                       model_id="M"))
    assert rc == 0 and "PATCH_P0_LEGACY" in out
    assert read(tmp_path / "cordis.patch.yml").count("- id:") == 1


# ---------------------------------------------------------------- 摘除

def test_摘除_成对记号段整删_邻居段不动(tmp_path):
    f = tmp_path / "cordis.patch.yml"
    f.write_text("- id: agent-team\n  config: {}\n", encoding="utf-8")
    run(mh.script_home_patch(str(tmp_path), "http://x.test/dsh", model_id="M"))
    run(mh.script_refs_merge(str(tmp_path)), "sk-mem-e5555555\n")
    rc, out = run(mh.script_home_strip(str(tmp_path)))
    assert rc == 0, out
    body = read(f)
    assert "agent-team" in body and "dsh-control-memory" not in body
    assert "llm-deepseek" not in body
    assert "refs=removed" in out and "RESTART_NEEDED" in out
    assert "PROXY_USER_KEY" not in read(tmp_path / ".credentials.yaml")


def test_摘除_p0_段在文件尾才敢删(tmp_path):
    f = tmp_path / "cordis.patch.yml"
    # 照 dsh-02 实机：P0 段就是最后一段
    f.write_text("- id: web-deepseek-api\n# dsh-control-p0: 接线\n"
                 "- id: llm-deepseek\n  disabled: false\n"
                 "- id: agent-default-model\n  config: {}\n", encoding="utf-8")
    rc, out = run(mh.script_home_strip(str(tmp_path)))
    assert rc == 0 and "p0_removed" in out
    body = read(f)
    assert "web-deepseek-api" in body and "dsh-control-p0" not in body


def test_摘除_p0_段后面压着别的段就不删(tmp_path):
    f = tmp_path / "cordis.patch.yml"
    f.write_text("# dsh-control-p0\n- id: llm-deepseek\n- id: agent-team\n",
                 encoding="utf-8")
    creds(tmp_path, "refs:\n  PROXY_USER_KEY: sk-mem-f6666666\n")
    rc, out = run(mh.script_home_strip(str(tmp_path)))
    assert rc == 0, out
    assert "PATCH_P0_LEGACY" in out          # 说破，不装删了
    assert "dsh-control-p0" in read(f) and "agent-team" in read(f)
    # refs 照删：记忆通路照断（停用不半途而废）
    assert "PROXY_USER_KEY" not in read(tmp_path / ".credentials.yaml")


def test_摘除_什么都没铺时静默无事(tmp_path):
    rc, out = run(mh.script_home_strip(str(tmp_path)))
    assert rc == 0 and "RESTART_NEEDED" not in out
    assert not (tmp_path / ".credentials.yaml").exists()


def test_摘后重铺_接得回(tmp_path):
    (tmp_path / "cordis.patch.yml").write_text("", encoding="utf-8")
    run(mh.script_home_patch(str(tmp_path), "http://x.test/dsh", model_id="M"))
    run(mh.script_home_strip(str(tmp_path)))
    rc, out = run(mh.script_home_patch(str(tmp_path), "http://x.test/dsh",
                                       model_id="M"))
    assert rc == 0 and "PATCH_WIRED" in out  # 摘干净了，能再接


# ---------------------------------------------------------------- 接管（过户）

def test_接管_拆p0段重铺控制台段_邻居段不动(tmp_path):
    f = tmp_path / "cordis.patch.yml"
    # 照 dsh-02 实机：P0 段在前，后面压着控制台 agent-team 段
    f.write_text("# 头注\n# dsh-control-p0: memory-hub 接线\n"
                 "- id: llm-deepseek\n  config:\n    apiKeyEnv: PROXY_USER_KEY\n"
                 "    baseURL: http://x.test/dsh\n"
                 "- id: agent-default-model\n  config: {}\n"
                 "# dsh-control: 智能体团队由控制台铺设\n- id: team-patch\n",
                 encoding="utf-8")
    rc, out = run(mh.script_home_takeover(str(tmp_path), "http://x.test/dsh",
                                          model_id="M"))
    assert rc == 0, out
    assert "TAKEOVER_P0_REMOVED" in out and "PATCH_WIRED" in out
    body = read(f)
    assert "# 头注" in body and "team-patch" in body       # 邻居一根毫毛不少
    assert "dsh-control-p0" not in body
    assert mh.MEMORY_MARKER in body and mh.MEMORY_MARKER_END in body
    assert body.count("- id: llm-deepseek") == 1           # 旧段真拆了，不是叠一段


def test_接管_幂等_再跑就是已接线(tmp_path):
    f = tmp_path / "cordis.patch.yml"
    f.write_text("# dsh-control-p0: 接线\n- id: llm-deepseek\n  config:\n"
                 "    apiKeyEnv: PROXY_USER_KEY\n    baseURL: http://x.test/dsh\n",
                 encoding="utf-8")
    run(mh.script_home_takeover(str(tmp_path), "http://x.test/dsh", model_id="M"))
    rc, out = run(mh.script_home_takeover(str(tmp_path), "http://x.test/dsh", model_id="M"))
    assert rc == 0 and "PATCH_ALREADY" in out
    assert read(f).count("- id: llm-deepseek") == 1


def test_接管_切出来不像记忆段就拒绝动手(tmp_path):
    f = tmp_path / "cordis.patch.yml"
    original = ("# dsh-control-p0: 谁手滑写的\n- id: someone-else\n"
                "  config:\n    a: 1\n")
    f.write_text(original, encoding="utf-8")
    rc, out = run(mh.script_home_takeover(str(tmp_path), "http://x.test/dsh", model_id="M"))
    assert rc == 0 and "PATCH_TAKEOVER_UNSAFE" in out
    assert read(f) == original                              # 拒绝=一字不动


def test_接管_没有p0段直说没有(tmp_path):
    (tmp_path / "cordis.patch.yml").write_text("- id: other\n", encoding="utf-8")
    rc, out = run(mh.script_home_takeover(str(tmp_path), "http://x.test/dsh", model_id="M"))
    assert rc == 0 and "TAKEOVER_NO_P0" in out
