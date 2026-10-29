# 参与贡献

## 开始前

```bash
uv venv .venv && uv pip install -e ".[dev]"
cp .env.example .env          # 填数据库地址
make check                    # ruff + pytest
cd frontend && npm ci && npx tsc -b && npm run build
```

改界面不必先搭后端。仓库自带契约一致的假接口：

```bash
cd frontend
node mock/server.mjs &                                 # 127.0.0.1:13300，密钥 dshc-demo
VITE_API_TARGET=http://127.0.0.1:13300 npx vite        # 打开 http://127.0.0.1:13301
```

假数据全是文档占位值（`*.example.com`、RFC5737 地址），所以截图与录屏可以直接放进
文档；`MOCK=empty` 看空台账，`MOCK_NO_SSE=1` 演练 SSE 断开后的轮询回退。

## 必须遵守的约定

- 契约先行：接口路径、响应字段、表结构、状态机的改动，先改
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，再改代码。PR 里说明改的是哪一节。
- 数据库三条规矩（详见 ARCHITECTURE §2.0）：不建外键、不写联表、每个字段带中文备注。
  评审会逐条查，缺备注的列不合入。
- 集成层只操作本系统创建的对象。任何"修改或删除不是我建的东西"的功能都会被拒。
- 密钥不进日志、不进错误信息、不进 URL query，也不提交进仓库。
- 交互做减法（§9）：除登录与改备注名外，界面不应要求用户打字。

## 提交与变更日志

提交信息用祈使句，一次提交讲一件事。面向用户或运维的变更写进
[CHANGELOG.md](CHANGELOG.md)，按 [Keep a Changelog](https://keepachangelog.com/) 组织，
版本号遵循 [SemVer](https://semver.org/)。

## 密钥扫描（三道闸）

2026-10-29 一次 `git add -A` 把 `.env` 备份卷进了公开仓（阿里云主账号 AK 公网挂了
6 分钟），此后立了机器闸，不靠自觉：

1. **pre-commit**：凭据/备份形状的文件名直接拒（`.env*`、`*.bak*`、`*.pem`……），
   暂存内容再过 gitleaks；
2. **pre-push**：扫"这次要离开本机的提交"——GitHub 有 push protection，
   Gitee/Gogs 没有，这闸对所有远端一视同仁；
3. **make upgrade**：出网版本前全历史扫一遍，不过不许上线。

启用（clone 后一次）：装 [gitleaks](https://github.com/gitleaks/gitleaks/releases)
（≥8.24，旧版 allowlist 支持不全）放 `PATH` 或 `~/.local/bin/`，然后 `make hooks`。
规则与豁免在 [.gitleaks.toml](.gitleaks.toml)；测试与 mock 里的假密钥已豁免，
新误报往 allowlist 里加，别 `--no-verify` 硬闯。

## 安全漏洞

不要开公开 issue，见 [SECURITY.md](SECURITY.md)。
