ARG MIRROR_PREFIX=
FROM ${MIRROR_PREFIX}python:3.11-slim

WORKDIR /app

# 安装系统依赖（支持 apt 镜像源）
ARG APT_MIRROR=deb.debian.org
RUN sed -i "s/deb.debian.org/$APT_MIRROR/g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（支持 pip 镜像源）
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i $PIP_INDEX_URL --trusted-host $PIP_TRUSTED_HOST

# 复制源代码
COPY src/ ./src/

# 设置 Python 路径
ENV PYTHONPATH=/app/src

# 暴露端口
EXPOSE 8000

# 启动命令（生产环境去掉 --reload）
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
