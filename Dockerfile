# ForChat 运行镜像（只装引擎，不装开发依赖）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY forchat ./forchat
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 forchat

# 工作区通过 volume 挂载；引擎只往这里写 .runs/
WORKDIR /workspace
USER forchat

ENTRYPOINT ["python", "-m", "forchat"]
