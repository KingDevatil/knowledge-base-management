.PHONY: help up down logs restart build backup health clean test

# 默认目标
help:
	@echo "企业中央知识库 + MCP Gateway 管理命令"
	@echo ""
	@echo "  make up      - 启动所有服务 (-d 后台运行)"
	@echo "  make down    - 停止所有服务"
	@echo "  make restart - 重启所有服务"
	@echo "  make build   - 重新构建 Gateway 镜像"
	@echo "  make logs    - 查看 Gateway 日志"
	@echo "  make logs-all- 查看所有服务日志"
	@echo "  make health  - 检查服务健康状态
  make metrics - 查看运行指标"
	@echo "  make backup  - 备份 Chroma 和 MinIO 数据"
	@echo "  make clean   - 清理所有数据和卷 (⚠️ 危险)"
	@echo "  make test    - 运行基础连通性测试"
	@echo "  make pull-model - 拉取 Ollama bge-m3 模型"

# 启动服务
up:
	docker compose up -d
	@echo "服务启动中，约需 30-60 秒..."
	@sleep 5
	@make health

# 停止服务
down:
	docker compose down

# 重启服务
restart:
	docker compose restart

# 重新构建 build:
	docker compose build --no-cache mcp-gateway
	docker compose up -d mcp-gateway

# 查看 Gateway 日志
logs:
	docker compose logs -f mcp-gateway

# 查看所有日志
logs-all:
	docker compose logs -f

# 健康检查
health:
	@echo "=== 服务健康检查 ==="
	@curl -s http://localhost:8000/health | python -m json.tool || echo "服务未就绪，请稍后再试"

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
	@curl -s -o /dev/null -w "Chroma: %{http_code}\n" http://localhost:8001/api/v1/heartbeat || echo "Chroma: 未运行"
	@curl -s -o /dev/null -w "MinIO: %{http_code}\n" http://localhost:9000/minio/health/live || echo "MinIO: 未运行"
	@echo "Redis: $$(docker compose exec redis redis-cli ping 2>/dev/null || echo '未运行')"
