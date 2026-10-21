PY := .venv/bin/python
UV ?= uv
MIRROR := https://pypi.tuna.tsinghua.edu.cn/simple
SITE ?= deploy/site.toml

.PHONY: venv install check migrate init seed serve test lint docker

venv:
	$(UV) venv .venv || python3 -m venv .venv

# MIRROR 留空走上游 PyPI；境内构建传 MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
install: venv
	$(UV) pip install -e ".[dev]" $(if $(MIRROR),--index-url $(MIRROR),)

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

check: lint test

docker:
	docker build -t dsh-control:latest .
