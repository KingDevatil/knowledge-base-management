#!/bin/sh
# 中央知识库 + MCP Gateway 自动启动脚本
# 自动检测 GPU，有独立显卡则启用加速，无则用 CPU

set -e

echo "=== 中央知识库 MCP Gateway ==="

# GPU 自动检测
COMPOSE_FILES="-f docker-compose.yml"
if command -v nvidia-smi >/dev/null 2>&1; then
    echo "[GPU] 检测到 NVIDIA GPU，启用硬件加速"
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu.yml"
else
    echo "[CPU] 未检测到 NVIDIA GPU，使用 CPU 模式"
fi

echo "[启动] docker compose $COMPOSE_FILES up -d"
docker compose $COMPOSE_FILES up -d

echo ""
echo "等待服务就绪..."
sleep 5

echo "[健康检查]"
curl -s http://localhost:8000/health | python -m json.tool 2>/dev/null || echo "服务还在启动中，稍后请运行: make health"

echo ""
echo "服务已启动。访问 http://localhost:8000/admin 进入管理面板。"
