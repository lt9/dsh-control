PY := .venv/bin/python
UV ?= uv
MIRROR := https://pypi.tuna.tsinghua.edu.cn/simple
SITE ?= deploy/site.toml

.PHONY: venv install migrate init seed serve test lint docker

venv:
	$(UV) venv .venv || python3 -m venv .venv

# bech102 在任何镜像源都不存在（已报 lead 处理 pyproject）；逐包安装绕开，
# 再 --no-deps 装本体。bech102 修好后可简化为 uv pip install -e '.[dev]'。
install: venv
	$(UV) pip install --index-url $(MIRROR) \
		fastapi "uvicorn[standard]" "sqlalchemy[asyncio]" asyncpg aiosqlite \
		alembic pydantic pydantic-settings httpx sse-starlette \
		cryptography paramiko pytest pytest-asyncio ruff
	$(UV) pip install -e . --no-deps --no-build-isolation

migrate:
	$(PY) -m dsh_control.cli migrate

# 只做迁移 + 生成运维密钥（零站点数据）
init:
	$(PY) -m dsh_control.cli init

seed:
	$(PY) -m dsh_control.cli seed --site $(SITE)

serve:
	$(PY) -m dsh_control.cli serve

test:
	$(PY) -m pytest -q

lint:
	$(UV) run ruff check src tests migrations || .venv/bin/ruff check src tests migrations

docker:
	docker build -t dsh-control:latest .
