"""put() 删行助手：2026-10-22 现网 .env 垃圾堆的根因回归。

原实现把匹配式写成单引号 '^$1='——函数体内 $1 不展开，grep 删的是字面量，
等于从不删行；每次开通都往实例 .env 追加一行 LLM_BASE_URL/LATEST 同款键，
现网实测一个文件里堆了 7 行。docker compose --env-file 取最后一次出现的值，
所以线上没炸，但这是运气不是设计。这里用真 sh 跑，防止再退回单引号。
"""

import subprocess

from dsh_control.jobs.deploy import _PUT_FN


def _run(tmp_path, body: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", f"f={tmp_path / '.env'}\n{_PUT_FN}\n{body}"],
                          capture_output=True, text=True, check=False)


def test_put_replaces_every_existing_line(tmp_path):
    (tmp_path / ".env").write_text("A=1\nLLM_BASE_URL=old1\nB=2\nLLM_BASE_URL=old2\n",
                                   encoding="utf-8")
    r = _run(tmp_path, 'put LLM_BASE_URL "http://198.51.100.77:7777/v1"')
    assert r.returncode == 0, r.stderr
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert text.count("LLM_BASE_URL=") == 1, f"重复行必须一次清光：\n{text}"
    assert "LLM_BASE_URL=http://198.51.100.77:7777/v1" in text
    assert "A=1" in text and "B=2" in text, "别的键一根毛都不许动"


def test_put_appends_when_key_absent(tmp_path):
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    r = _run(tmp_path, "put NEW_KEY some-value")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".env").read_text(encoding="utf-8").splitlines() == ["A=1",
                                                                            "NEW_KEY=some-value"]


def test_put_keeps_value_with_slashes_and_ampersands(tmp_path):
    (tmp_path / ".env").write_text("KEY=old\n", encoding="utf-8")
    r = _run(tmp_path, 'put KEY "http://a:1/v1?x=1&y=2"')
    assert r.returncode == 0, r.stderr
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "KEY=http://a:1/v1?x=1&y=2\n" in text, "值必须原样落盘（这就是不用 sed 的原因）"
