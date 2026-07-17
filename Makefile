.PHONY: help configure up down logs restart build backup health clean test pull-model

# 可用档位：auto / minimum / recommended / high-performance
PROFILE ?= auto

# ── GPU 自动检测 ──
# 有 NVIDIA GPU → 加载 docker-compose.gpu.yml
# 无 GPU / Intel / AMD → 仅用 docker-compose.yml (CPU)
NVIDIA_SMI := $(shell command -v nvidia-smi 2>/dev/null)
ifdef NVIDIA_SMI
  COMPOSE_FILES := -f docker-compose.yml -f docker-compose.gpu.yml
else
  COMPOSE_FILES := -f docker-compose.yml
endif

# 默认目标
help:
	@echo "企业中央知识库 + MCP Gateway 管理命令"
	@echo ""
	@echo "  make up PROFILE=recommended - 按硬件档位启动所有服务"
	@echo "  make configure - 交互式生成或重新配置 .env"
	@echo "  make down    - 停止所有服务"
	@echo "  make restart - 重启所有服务"
	@echo "  make build   - 重新构建 Gateway 镜像"
	@echo "  make logs    - 查看 Gateway 日志"
	@echo "  make logs-all- 查看所有服务日志"
	@echo "  make health  - 检查服务健康状态"
	@echo "  make metrics - 查看运行指标"
	@echo "  make backup  - 备份 Chroma 和 MinIO 数据"
	@echo "  make clean   - 清理所有数据和卷 (⚠️ 危险)"
	@echo "  make test    - 运行基础连通性测试"
	@echo "  make pull-model - 手动刷新 Ollama 模型（正常启动会自动拉取）"
	@echo ""
	@echo "GPU 检测: $$(if [ -n '$(NVIDIA_SMI)' ]; then echo '🟢 NVIDIA GPU 已启用'; else echo '🟡 CPU 模式'; fi)"

# 启动服务
configure:
	sh ./start.sh configure

up:
	sh ./start.sh up --profile $(PROFILE)

# 停止服务
down:
	sh ./start.sh down

# 重启服务
restart:
	sh ./start.sh down
	sh ./start.sh up --profile $(PROFILE)

# 重新构建
build:
	docker compose $(COMPOSE_FILES) build --no-cache mcp-gateway
	docker compose $(COMPOSE_FILES) up -d mcp-gateway

# 查看 Gateway 日志
logs:
	sh ./start.sh logs

# 查看所有日志
logs-all:
	docker compose $(COMPOSE_FILES) logs -f

# 健康检查
health:
	sh ./start.sh status

# 运行指标
metrics:
	@echo "=== 运行指标 ==="
	@curl -s http://localhost:8000/metrics | python -m json.tool || echo "服务未就绪，请稍后再试"

# 拉取模型
pull-model:
	docker compose exec ollama ollama pull bge-m3

# 备份数据
backup:
	@mkdir -p backups/$$(date +%Y%m%d)
	@echo "备份 Chroma 数据..."
	@docker compose exec chroma tar czf /tmp/chroma-backup.tar.gz /chroma/chroma 2>/dev/null || echo "Chroma 备份跳过"
	@docker compose cp chroma:/tmp/chroma-backup.tar.gz backups/$$(date +%Y%m%d)/ 2>/dev/null || true
	@echo "备份完成，保存在 backups/$$(date +%Y%m%d)/"

# 清理所有数据 (危险)
clean:
	@echo "⚠️  警告: 这将删除所有数据卷，包括知识库内容！"
	@read -p "确定要继续吗? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	docker compose down -v
	docker volume prune -f
	@echo "已清理所有数据"

# 基础测试
test:
	@echo "=== 测试服务连通性 ==="
	@curl -s -o /dev/null -w "Nginx: %{http_code}\n" http://localhost:80/health || echo "Nginx: 未运行"
	@curl -s -o /dev/null -w "Gateway: %{http_code}\n" http://localhost:8000/health || echo "Gateway: 未运行"
	@curl -s -o /dev/null -w "Chroma: %{http_code}\n" http://localhost:8001/api/v2/heartbeat || echo "Chroma: 未运行"
	@curl -s -o /dev/null -w "MinIO: %{http_code}\n" http://localhost:9000/minio/health/live || echo "MinIO: 未运行"
	@echo "Redis: $$(docker compose exec redis redis-cli ping 2>/dev/null || echo '未运行')"
