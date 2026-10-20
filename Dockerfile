# dsh-control：单容器单进程（FastAPI 同时 serve API 与 frontend/dist）。
# 显式列依赖而非 `pip install .`：pyproject 里 bech102 在任何源都不存在
# （已报 lead 修依赖），cli init 有 `dshc-` 前缀回退，不阻塞构建。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

RUN apt-get update \
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
    "cryptography>=43,<46" \
    "paramiko>=3.5,<4"

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
