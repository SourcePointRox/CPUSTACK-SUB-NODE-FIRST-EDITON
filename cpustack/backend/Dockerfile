FROM python:3.11-slim AS base

# 系统依赖：PostgreSQL 客户端、编译工具（llama.cpp）、psutil 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    g++ \
    cmake \
    make \
    git \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY pyproject.toml .
RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache .

# 复制应用代码
COPY cpustack/ ./cpustack/

# 创建数据目录
RUN mkdir -p /var/lib/cpustack/cache /var/lib/cpustack/ui

ENV CPUSTACK_DATA_DIR=/var/lib/cpustack
ENV CPUSTACK_MODEL_CACHE_DIR=/var/lib/cpustack/cache

EXPOSE 80

# 默认启动 Server（可通过 CMD 覆盖为 worker）
CMD ["python", "-m", "cpustack.cli", "serve", "--host", "0.0.0.0", "--port", "80"]
