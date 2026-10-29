"""manifest 解析与发布校验（ADR-0001 决策二 + U-3 阈值定案）。

发布是校验的主战场：未知字段、没声明的插值、超阈值——全部当场拒，不等到铺到
一半。宁可不装，不要半装。
"""

from __future__ import annotations

import pytest

from dsh_control.integrations import capability_engine as e

MIN_SKILL = """
id = "demo"
version = "1.0.0"
kind = "skill"
title = "演示"
compatible_dsh = ">=0.2.0-rc.1"
marker = "# dsh-control: 演示由控制台铺设"
status = "test -f ${DSH_HOME}/skills/demo/SKILL.md"
healthcheck = "test -f ${DSH_HOME}/skills/demo/SKILL.md"
uninstall = "reverse"
files = ["SKILL.md"]

[params]
on_missing = "skipped"
"""


def _files(**extra: bytes) -> dict[str, bytes]:
    out = {"manifest.toml": MIN_SKILL.encode(), "SKILL.md": b"# hi\n"}
    out.update(extra)
    return out


def test_最小skill包通过() -> None:
    man = e.parse_manifest_text(MIN_SKILL)
    assert e.validate_manifest(man, files=_files()) == []


def test_未知字段当场拒() -> None:
    bad = MIN_SKILL.replace('title = "演示"', 'title = "演示"\nauthor = "某人"')
    man = e.parse_manifest_text(bad)
    errs = e.validate_manifest(man, files=_files())
    assert any("未知字段" in x and "author" in x for x in errs)


def test_缺必填字段拒() -> None:
    man = e.parse_manifest_text(MIN_SKILL.replace('status = "test -f ${DSH_HOME}/skills/demo/SKILL.md"\n', ""))
    errs = e.validate_manifest(man, files=_files())
    assert any("缺必填字段" in x and "status" in x for x in errs)


def test_单文件超两兆拒收并提示制品库() -> None:
    big = _files(**{"SKILL.md": b"x" * (e.MAX_PACK_FILE_BYTES + 1)})
    man = e.parse_manifest_text(MIN_SKILL)
    errs = e.validate_manifest(man, files=big)
    assert any("制品库" in x and "SKILL.md" in x for x in errs)


def test_整包超八兆拒收() -> None:
    chunk = b"x" * (1 << 20)
    files = {"manifest.toml": MIN_SKILL.encode()}
    for i in range(9):
        files[f"big-{i}.md"] = chunk
    man = e.parse_manifest_text(MIN_SKILL)
    errs = e.validate_manifest(man, files=files)
    assert any("整包" in x and "制品库" in x for x in errs)


def test_未声明的插值按未知字段拒() -> None:
    bad = MIN_SKILL.replace('healthcheck = "test -f ${DSH_HOME}/skills/demo/SKILL.md"',
                            'healthcheck = "curl -sf {{probe_url}}"')
    man = e.parse_manifest_text(bad)
    errs = e.validate_manifest(man, files=_files())
    assert any("probe_url" in x and "params" in x for x in errs)


def test_声明过的插值放行() -> None:
    ok = (MIN_SKILL.replace('[params]\non_missing = "skipped"',
                            '[params]\non_missing = "skipped"\n\n'
                            '[params.probe_url]\ntype = "string"\nrequired = true')
          .replace('healthcheck = "test -f ${DSH_HOME}/skills/demo/SKILL.md"',
                   'healthcheck = "curl -sf {{probe_url}}"'))
    man = e.parse_manifest_text(ok)
    assert e.validate_manifest(man, files=_files()) == []


def test_uninstall只认reverse() -> None:
    bad = MIN_SKILL.replace('uninstall = "reverse"', 'uninstall = "purge"')
    errs = e.validate_manifest(e.parse_manifest_text(bad), files=_files())
    assert any("uninstall" in x and "reverse" in x for x in errs)


def test_skill文件声明了包里没有拒() -> None:
    man = e.parse_manifest_text(MIN_SKILL)
    errs = e.validate_manifest(man, files={"manifest.toml": MIN_SKILL.encode()})
    assert any("SKILL.md" in x and "包里没有" in x for x in errs)


def test_patch的marker必须以dsh_control开头() -> None:
    man = e.parse_manifest_text(MIN_SKILL)
    man["kind"] = "patch"
    man["marker"] = "见！"
    man.pop("files")
    man["source"] = {"bundle": "@deepseek-ai/x", "file": "cordis.patch.yml"}
    man["append"] = "max_members = 3"
    errs = e.validate_manifest(man, files=_files())
    assert any("marker" in x and "# dsh-control:" in x for x in errs)


def test_id命名不合拒() -> None:
    bad = MIN_SKILL.replace('id = "demo"', 'id = "Demo_1"')
    errs = e.validate_manifest(e.parse_manifest_text(bad), files=_files())
    assert any("id 命名" in x for x in errs)


def test_asset类只留解析位执行拒() -> None:
    man = e.parse_manifest_text("""
id = "fonts"
version = "2026.10"
kind = "asset"
title = "字体库"
compatible_dsh = ""
marker = "# dsh-control: fonts"
status = "test -d ${DSH_HOME}/fonts"
healthcheck = "test -d ${DSH_HOME}/fonts"
uninstall = "reverse"
files = ["fonts.tar.ref"]
target = "/usr/share/fonts"

[params]
on_missing = "skipped"
""")
    assert e.validate_manifest(man, files={"manifest.toml": b"", "fonts.tar.ref": b""}) == []
    import asyncio
    res = asyncio.run(e.run_action(None, "c", man, "/data", "install",
                                   e.ResolvedParams()))
    assert res.state == "failed" and "P2" in res.reason


def test_speckit包原样可发布() -> None:
    packs = e.packs_from_dir("kit/capabilities/speckit")
    assert len(packs) == 1
    p = packs[0]
    assert p.capability_id == "speckit" and p.kind == "skill"
    assert p.manifest["compatible_dsh"] == ">=0.2.0-rc.1"
    assert p.manifest["default_on"] is True          # 「默认开」的载体
    assert len(p.files) == 11                        # 十个 SKILL.md + manifest


def test_十个skill条目落位规则() -> None:
    packs = e.packs_from_dir("kit/capabilities/speckit")
    targets = dict(e.skill_targets(packs[0].manifest, "/data"))
    # 带子路径 → skills/<条目>（DSH 发现只扫一层，必须平铺）
    assert targets["speckit-specify/SKILL.md"] == "/data/skills/speckit-specify/SKILL.md"
    # 不带路径 → skills/<id>/<条目>（comfy 先例）
    man = e.parse_manifest_text(MIN_SKILL)
    assert dict(e.skill_targets(man, "/data"))["SKILL.md"] == "/data/skills/demo/SKILL.md"


def test_参数schema分流secret() -> None:
    man = e.parse_manifest_text("""
id = "demo"
version = "1.0.0"
kind = "patch"
title = "x"
compatible_dsh = ""
marker = "# dsh-control: x"
status = "true"
healthcheck = "true"
uninstall = "reverse"
source = { bundle = "@a/b", file = "cordis.patch.yml" }
append = "max_members = {{max_members}}"

[params]
on_missing = "failed"

[params.max_members]
type = "integer"
required = true

[params.token]
type = "string"
required = true
secret = true
""")
    r = e.resolve_params(man, {"max_members": "3"}, {"token": "sk-1"})
    assert r.values == {"max_members": 3}      # integer 已转型
    assert r.secrets == {"token": "sk-1"}      # secret 走 stdin，不进 values
    assert r.missing == []
    assert e.render("max_members = {{max_members}}", r.values) == "max_members = 3"
    miss = e.resolve_params(man, {}, {})
    assert sorted(miss.missing) == ["max_members", "token"]


def test_secret值含shell语法字符拒() -> None:
    man = e.parse_manifest_text("""
id = "demo"
version = "1.0.0"
kind = "patch"
title = "x"
compatible_dsh = ""
marker = "# dsh-control: x"
status = "true"
healthcheck = "true"
uninstall = "reverse"
source = {}
append = ""

[params]
on_missing = "skipped"

[params.token]
type = "string"
secret = true
""")
    with pytest.raises(e.ManifestError):
        e.resolve_params(man, {}, {"token": 'a"b$c'})
