PY := .venv/bin/python
UV ?= uv
MIRROR := https://pypi.tuna.tsinghua.edu.cn/simple
SITE ?= deploy/site.toml

.PHONY: venv install check migrate init seed serve test lint docker upgrade

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

# 上线升级（契约 §6.1.2 同一纪律，管到控制台自己）：升级源必须是干净
# commit，镜像烙来源 label，compose 钉具体 tag——latest 只是本地开发用。
#   make upgrade TAG=v0.2.0
REV  := $(shell git rev-parse HEAD)
SHORT := $(shell git rev-parse --short HEAD)
upgrade:
	@[ -n "$(TAG)" ] || { echo "用法：make upgrade TAG=vX.Y.Z（要落账的升级必须带版本号）"; exit 2; }
	@git diff --quiet && git diff --cached --quiet || { echo "工作树不干净：升级源必须是 commit。先提交或还原再跑。"; exit 2; }
	@! git tag -l "$(TAG)" | grep -q . || { echo "tag $(TAG) 已存在，换号"; exit 2; }
	cd frontend && npm run build
	docker build --label "dshc.git-rev=$(REV)" --label "org.opencontainers.image.revision=$(REV)" \
		--label "org.opencontainers.image.version=$(TAG)" \
		-t "dsh-control:$(TAG)" -t dsh-control:latest .
	git tag -a "$(TAG)" -m "dsh-control $(TAG)（镜像 dsh-control:$(TAG)，rev $(SHORT)）"
	git push origin "$(TAG)"
	@grep -q "^DSHC_VERSION=" .env && sed -i "s|^DSHC_VERSION=.*|DSHC_VERSION=$(TAG)|" .env \
		|| printf '\n# 现网钉定的版本（make upgrade 维护，compose 读它）\nDSHC_VERSION=$(TAG)\n' >> .env
	docker compose -f docker-compose.yml -f deploy/site.compose.yml up -d dsh-control
	@echo "已上线 $(TAG)（rev $(SHORT)）；tag 已推 origin，回滚：改 .env 的 DSHC_VERSION 回旧 tag 再 up -d"
