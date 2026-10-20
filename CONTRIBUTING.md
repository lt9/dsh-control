# 参与贡献

## 开始前

```bash
uv venv .venv && uv pip install -e ".[dev]"
cp .env.example .env          # 填数据库地址
cd web && npm ci              # 前端
make check                    # ruff + pytest + 前端构建
```

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

## 安全漏洞

不要开公开 issue，见 [SECURITY.md](SECURITY.md)。
