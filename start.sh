#!/bin/sh
# Docker deployment adapter for Linux/macOS/WSL.
# Interface: sh ./start.sh [up|down|status|logs|init] [--cpu|--gpu]

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="$ROOT_DIR/.env"
COMMAND=${1:-up}
GPU_MODE=${KB_GPU:-auto}

case "${2:-}" in
    --cpu) GPU_MODE=cpu ;;
    --gpu) GPU_MODE=gpu ;;
    "") ;;
    *) echo "未知参数: ${2}" >&2; exit 2 ;;
esac

cd "$ROOT_DIR"

usage() {
    cat <<'EOF'
用法: sh ./start.sh [up|down|status|logs|init] [--cpu|--gpu]

  up      初始化配置、构建并启动，等待模型和 Gateway 就绪（默认）
  down    停止 Docker 服务
  status  查看容器状态并检查 Gateway
  logs    跟踪所有容器日志
  init    仅创建/修复 .env，不启动服务
EOF
}

generate_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

read_env_value() {
    awk -F= -v key="$1" '
        $1 == key {
            sub(/^[^=]*=/, "")
            gsub(/\r$/, "")
            gsub(/^['\''\"]|['\''\"]$/, "")
            print
            exit
        }
    ' "$ENV_FILE" 2>/dev/null || true
}

set_env_value() {
    env_key=$1
    env_value=$2
    env_tmp="${ENV_FILE}.tmp.$$"
    awk -v key="$env_key" -v value="$env_value" '
        BEGIN { found = 0 }
        index($0, key "=") == 1 { print key "=" value; found = 1; next }
        { print }
        END { if (!found) print key "=" value }
    ' "$ENV_FILE" > "$env_tmp"
    mv "$env_tmp" "$ENV_FILE"
}

initialize_env() {
    env_created=false
    if [ ! -f "$ENV_FILE" ]; then
        cp "$ROOT_DIR/.env.example" "$ENV_FILE"
        env_created=true
        echo "[配置] 已从 .env.example 创建 .env"
    fi

    session_secret=$(read_env_value SESSION_SECRET)
    if [ ${#session_secret} -lt 32 ] || [ "$session_secret" = "change-me-to-a-random-long-string-at-least-32-chars" ]; then
        set_env_value SESSION_SECRET "$(generate_secret)"
        echo "[配置] 已生成 SESSION_SECRET"
    fi

    minio_password=$(read_env_value MINIO_ROOT_PASSWORD)
    if [ -z "$minio_password" ] || [ "$minio_password" = "change-me-strong-password" ]; then
        generated_minio_password=$(generate_secret)
        set_env_value MINIO_ROOT_PASSWORD "$generated_minio_password"
        set_env_value MINIO_SECRET_KEY "$generated_minio_password"
        echo "[配置] 已生成 MinIO 密码"
    fi

    if [ "$env_created" = true ]; then
        set_env_value EXTERNAL_DOMAIN ""
        set_env_value INTERNAL_DOMAIN "localhost"
    fi
    chmod 600 "$ENV_FILE" 2>/dev/null || true
}

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "[错误] 未找到 Docker。请先安装 Docker Engine 或 Docker Desktop。" >&2
        exit 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        echo "[错误] 未找到 Docker Compose v2（docker compose）。" >&2
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "[错误] Docker daemon 未运行。" >&2
        exit 1
    fi
}

select_gpu() {
    case "$GPU_MODE" in
        gpu) GPU_ENABLED=true ;;
        cpu) GPU_ENABLED=false ;;
        auto)
            if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
                GPU_ENABLED=true
            else
                GPU_ENABLED=false
            fi
            ;;
        *) echo "[错误] KB_GPU 只能是 auto、cpu 或 gpu。" >&2; exit 2 ;;
    esac
}

compose() {
    if [ "$GPU_ENABLED" = true ]; then
        docker compose -f docker-compose.yml -f docker-compose.gpu.yml "$@"
    else
        docker compose -f docker-compose.yml "$@"
    fi
}

gateway_ready() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1
    else
        compose exec -T mcp-gateway python -c \
            "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" \
            >/dev/null 2>&1
    fi
}

wait_for_gateway() {
    echo "[等待] 首次启动会自动拉取 Embedding 模型，可能需要几分钟。"
    wait_attempt=0
    wait_max=300
    while [ "$wait_attempt" -lt "$wait_max" ]; do
        if gateway_ready; then
            echo "[就绪] Gateway 健康检查通过"
            return 0
        fi
        wait_attempt=$((wait_attempt + 1))
        if [ $((wait_attempt % 15)) -eq 0 ]; then
            echo "[等待] 已等待 $((wait_attempt * 2)) 秒..."
        fi
        sleep 2
    done

    echo "[错误] 10 分钟内未就绪。以下是关键容器状态和日志：" >&2
    compose ps >&2 || true
    compose logs --tail=80 ollama-model-init mcp-gateway >&2 || true
    return 1
}

case "$COMMAND" in
    init)
        initialize_env
        echo "[完成] 配置位于 $ENV_FILE"
        ;;
    up)
        initialize_env
        require_docker
        select_gpu
        if [ "$GPU_ENABLED" = true ]; then
            echo "[启动] NVIDIA GPU 模式"
        else
            echo "[启动] CPU 模式"
        fi
        echo "[等待] Compose 将依次检查依赖、拉取模型并等待 Gateway 健康。"
        compose up -d --build
        wait_for_gateway
        echo ""
        echo "部署完成："
        echo "  管理后台: http://localhost/admin"
        echo "  MCP:      http://localhost/mcp"
        echo "  局域网:   将 localhost 替换为本机 IP"
        ;;
    down)
        require_docker
        select_gpu
        compose down
        ;;
    status)
        require_docker
        select_gpu
        compose ps
        if gateway_ready; then
            echo "[健康] Gateway 正常"
        else
            echo "[健康] Gateway 尚未就绪" >&2
            exit 1
        fi
        ;;
    logs)
        require_docker
        select_gpu
        compose logs -f
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
