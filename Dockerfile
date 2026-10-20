# dsh-control：单容器单进程（FastAPI 同时 serve API 与 frontend/dist）。
# 显式列依赖而非 `pip install .`：pyproject 里 bech102 在任何源都不存在
# （已报 lead 修依赖），cli init 有 `dshc-` 前缀回退，不阻塞构建。
# 镜像源参数化：默认走上游，境内构建时传 build arg，不把任何地区的镜像写死。
#   docker build --build-arg APT_MIRROR=https://mirrors.aliyun.com \
#                --build-arg PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/simple .
ARG APT_MIRROR=""
ARG PIP_INDEX_URL=https://pypi.org/simple

FROM python:3.12-slim

# FROM 之前声明的 ARG 不会自动带进构建阶段，必须在下面重新声明，否则展开成空串
ARG APT_MIRROR
ARG PIP_INDEX_URL
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

# openssh-client 是必需的：主机侧执行器走 `ssh` 二进制（契约 §0.5，不在被管机装 agent）
RUN if [ -n "$APT_MIRROR" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "$f" ] && sed -i \
                -e "s|http://deb.debian.org/debian-security|${APT_MIRROR}/debian-security|g" \
                -e "s|http://deb.debian.org/debian|${APT_MIRROR}/debian|g" \
                -e "s|http://security.debian.org/debian-security|${APT_MIRROR}/debian-security|g" \
                "$f" || true; \
        done; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends openssh-client curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "fastapi>=0.115,<1" \
    "uvicorn[standard]>=0.30,<1" \
    "sqlalchemy[asyncio]>=2.0,<3" \
    "asyncpg>=0.30,<1" \
    "alembic>=1.14,<2" \
    "pydantic>=2.9,<3" \
    "pydantic-settings>=2.6,<3" \
    "httpx>=0.27,<1" \
    "sse-starlette>=2.1,<3" \
    "cryptography>=43,<46"

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
# 前端产物由 frontend 侧构建后随镜像打包（单容器契约）
COPY frontend/dist ./frontend/dist

RUN pip install --no-cache-dir --no-deps .

ENV DSHC_LISTEN_HOST=0.0.0.0 \
    DSHC_LISTEN_PORT=13300

EXPOSE 13300

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:13300/api/healthz || exit 1

CMD ["python", "-m", "dsh_control.cli", "serve"]
