"""随仓库发布的面里不许出现真实站点值。

仓库是 MIT 开源的。写文档时最容易顺手把自己环境的 IP、域名贴进去，而且它不影响任何
功能测试，没人会拦。这里钉两条：

1. **IP**：发布面里出现的 IPv4 只能是 RFC 5737 文档保留段与几个公认示例地址。
   扫文档、配置与后端代码，**不扫前端源码**——SVG 路径里的四段小数会被当成 IP，
   那种误报会把人训练成忽略这条测试，反而更危险。
2. **域名**：只扫文档与示例配置，且只认常见 TLD 结尾的整词，公共镜像源等公开基础设施
   进允许列表。这里防的是"我自己的域名"，不是"互联网上存在的域名"。

要加例外就往下方列表里写，并想清楚它是产品文档的一部分，还是只是我的站点。
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

IP_FILES = ("docs/*.md", "*.md", "deploy/*.example.toml", "*.yml", "Dockerfile",
            "src/**/*.py", "tests/**/*.py", "migrations/**/*.py", "pyproject.toml")
DOMAIN_FILES = ("docs/*.md", "*.md", "deploy/*.example.toml")

IPV4 = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")
KNOWN_TLD = (r"com|cn|net|org|io|test|dev|me|cc|top|xyz|info|edu|gov|sh|so|app|cloud")
DOMAIN = re.compile(
    rf"(?<![\w.@-])((?!-)[a-z0-9-]{{1,63}}(?<!-)(?:\.(?!-)[a-z0-9-]{{1,63}}(?<!-))+\.(?:{KNOWN_TLD}))(?![\w-])")

DOC_NETS = [ipaddress.ip_network(n) for n in
            ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
ALLOWED_IPS = {"0.0.0.0", "127.0.0.1", "172.17.0.1", "1.2.3.4"}   # 本机、docker0、惯用占位
CODE_NETS = [ipaddress.ip_network(n) for n in
             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]   # 代码里的假数据可用私有段

# 公开基础设施（不是"我的站点"）
ALLOWED_DOMAINS = {
    "example.com", "example.test", "example.org", "example.net", "localhost",
    "testserver", "pypi.org", "python.org", "github.com", "docker.io",
    "aliyuncs.com", "postgres.org", "npmjs.org", "npmmirror.com",
    "mirrors.aliyun.com", "mirrors.tuna.tsinghua.edu.cn", "deb.debian.org",
    "security.debian.org", "registry.npmmirror.com", "nodejs.org",
}
ALLOWED_SUFFIXES = (".example.com", ".example.test", ".example.org", ".example.net")


def _files(pats: tuple[str, ...]) -> list[Path]:
    out: set[Path] = set()
    for pat in pats:
        out.update(ROOT.glob(pat))
    return sorted(p for p in out if p.is_file() and ".design-checks" not in p.parts)


def test_shipped_text_only_uses_documentation_addresses() -> None:
    # 文档与配置走严格档：只准文档保留段。代码走宽一档：私有段与回环算合法 fixture，
    # 只拦"公网可路由且不是文档保留地址"的 IP——那才是把真出口 IP 贴进仓库的形态。
    strict = [p for p in _files(IP_FILES) if "/tests/" not in f"/{p.relative_to(ROOT)}"
              and "/src/" not in f"/{p.relative_to(ROOT)}"
              and "/migrations/" not in f"/{p.relative_to(ROOT)}"]
    lenient = [p for p in _files(IP_FILES) if p not in strict]
    bad: list[str] = []
    for path, nets in ((p, DOC_NETS) for p in strict):
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for ip in IPV4.findall(line):
                if ip in ALLOWED_IPS:
                    continue
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if not any(addr in n for n in nets):
                    bad.append(f"{path.relative_to(ROOT)}:{lineno} IP {ip}")
    for path in lenient:
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for ip in IPV4.findall(line):
                if ip in ALLOWED_IPS:
                    continue
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if any(addr in n for n in (*DOC_NETS, *CODE_NETS)):
                    continue
                if addr.is_private or addr.is_loopback or addr.is_unspecified:
                    continue
                bad.append(f"{path.relative_to(ROOT)}:{lineno} IP {ip}")
    assert not bad, "发布面出现真实 IP，换成 RFC 5737 文档保留地址：\n" + "\n".join(bad[:20])


def test_docs_only_use_example_domains() -> None:
    bad: list[str] = []
    for path in _files(DOMAIN_FILES):
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for dom in DOMAIN.findall(line.lower()):
                if dom in ALLOWED_DOMAINS or dom.endswith(ALLOWED_SUFFIXES) \
                        or dom.endswith(".aliyuncs.com"):
                    continue
                bad.append(f"{path.relative_to(ROOT)}:{lineno} 域名 {dom}")
    assert not bad, ("文档里出现了疑似站点域名，示例一律用 example.com / *.example.test"
                     "（真实值放 docs/site/，那里已 gitignore）：\n" + "\n".join(bad[:20]))
