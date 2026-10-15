# Eventide 部署镜像：uv 托管依赖，非 root 运行。
# 容器只是部署形态，Eventide 的权限策略与本地 Shell 执行器不是操作系统沙箱。
FROM node:22-bookworm-slim AS web-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# 工作区规范化与 Continue 证据依赖 git
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再拷源码，源码改动不打散依赖层；--locked 保证与 uv.lock 一致
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY eventide/ eventide/
RUN rm -rf /app/eventide/workbench
COPY --from=web-build /build/eventide/workbench/ eventide/workbench/
COPY README.md LICENSE ./
RUN uv sync --locked

ENV EVENTIDE_STATE_DIR=/data/eventide \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin eventide \
    && mkdir -p /data/eventide /workspaces/demo \
    && chown -R eventide:eventide /data/eventide /app /workspaces/demo

USER eventide
WORKDIR /workspaces/demo

VOLUME /data/eventide
EXPOSE 8000

ENTRYPOINT ["eventide"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
