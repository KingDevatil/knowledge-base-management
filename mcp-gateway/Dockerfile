FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源代码
COPY src/ ./src/

# 设置 Python 路径
ENV PYTHONPATH=/app/src

# 暴露端口
EXPOSE 8000

# 启动命令（生产环境去掉 --reload）
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
