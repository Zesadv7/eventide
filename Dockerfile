# Eventide 部署镜像：uv 托管依赖，非 root 运行。
# 容器只是部署形态，Eventide 的权限策略与本地 Shell 执行器不是操作系统沙箱。
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
COPY README.md LICENSE ./
RUN uv sync --locked

ENV EVENTIDE_STATE_DIR=/data/eventide \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin eventide \
    && mkdir -p /data/eventide \
    && chown -R eventide:eventide /data/eventide /app

USER eventide

VOLUME /data/eventide
EXPOSE 8000

ENTRYPOINT ["eventide"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
