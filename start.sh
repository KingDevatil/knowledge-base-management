#!/bin/sh
# Docker deployment adapter for Linux/macOS/WSL.
# Interface: sh ./start.sh [up|down|status|logs|init] [--cpu|--gpu] [--profile NAME] [--tunnel cloudflare]

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="$ROOT_DIR/.env"
COMMAND=up
GPU_MODE=${KB_GPU:-auto}
PROFILE_MODE=auto
TUNNEL_MODE=off
SOURCE_FALLBACK=false

case "${1:-}" in
    up|down|status|logs|init|help|-h|--help)
        COMMAND=$1
        shift
        ;;
esac

while [ "$#" -gt 0 ]; do
    case "$1" in
        --cpu) GPU_MODE=cpu; shift ;;
        --gpu) GPU_MODE=gpu; shift ;;
        --profile)
            [ "$#" -ge 2 ] || { echo "[错误] --profile 缺少档位名称。" >&2; exit 2; }
            PROFILE_MODE=$2
            shift 2
            ;;
        --profile=*) PROFILE_MODE=${1#*=}; shift ;;
        --tunnel)
            [ "$#" -ge 2 ] || { echo "[错误] --tunnel 缺少模式名称。" >&2; exit 2; }
            TUNNEL_MODE=$2
            shift 2
            ;;
        --tunnel=*) TUNNEL_MODE=${1#*=}; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

case "$PROFILE_MODE" in
    auto|minimum|recommended|high-performance) ;;
    *) echo "[错误] 硬件档位只能是 auto、minimum、recommended 或 high-performance。" >&2; exit 2 ;;
esac

case "$TUNNEL_MODE" in
    off|cloudflare) ;;
    *) echo "[错误] 穿透模式只能是 off 或 cloudflare。" >&2; exit 2 ;;
esac

cd "$ROOT_DIR"

usage() {
    cat <<'EOF'
用法: sh ./start.sh [up|down|status|logs|init] [--cpu|--gpu] [--profile NAME] [--tunnel cloudflare]

  up      初始化配置、构建并启动，等待模型和 Gateway 就绪（默认）
  down    停止 Docker 服务
  status  查看容器状态并检查 Gateway
  logs    跟踪所有容器日志
  init    仅创建/修复 .env，不启动服务

  --profile 显式覆盖硬件档位；auto 只在首次创建 .env 时应用 recommended
  --tunnel cloudflare 读取 .env 中的 Token 并启动可选内网穿透容器
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

apply_hardware_profile() {
    profile_name=$1
    [ -n "$profile_name" ] && [ "$profile_name" != "auto" ] || return 0
    profile_file="$ROOT_DIR/deploy/profiles/$profile_name.env"
    if [ ! -f "$profile_file" ]; then
        echo "[错误] 硬件配置档位不存在: $profile_file" >&2
        exit 1
    fi
    while IFS= read -r raw_line || [ -n "$raw_line" ]; do
        line=$(printf '%s' "$raw_line" | tr -d '\r')
        case "$line" in
            ''|'#'*) continue ;;
            *=*)
                profile_key=${line%%=*}
                profile_value=${line#*=}
                set_env_value "$profile_key" "$profile_value"
                ;;
        esac
    done < "$profile_file"
    echo "[配置] 已应用硬件档位: $profile_name"
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
    if [ "$PROFILE_MODE" != "auto" ]; then
        apply_hardware_profile "$PROFILE_MODE"
    elif [ "$env_created" = true ]; then
        apply_hardware_profile recommended
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

select_tunnel() {
    [ "$TUNNEL_MODE" = cloudflare ] || return 0
    tunnel_token=$(read_env_value CLOUDFLARE_TUNNEL_TOKEN)
    if [ -z "$tunnel_token" ]; then
        echo "[错误] 启用 Cloudflare Tunnel 前请在 .env 设置 CLOUDFLARE_TUNNEL_TOKEN。" >&2
        exit 1
    fi
    echo "[穿透] Cloudflare Tunnel 已启用；Public Hostname 上游应配置为 http://nginx:80"
}

compose() {
    if [ "$TUNNEL_MODE" = cloudflare ]; then
        set -- --profile tunnel "$@"
    fi
    if [ "$GPU_ENABLED" = true ] && [ "$SOURCE_FALLBACK" = true ]; then
        docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.official.yml "$@"
    elif [ "$GPU_ENABLED" = true ]; then
        docker compose -f docker-compose.yml -f docker-compose.gpu.yml "$@"
    elif [ "$SOURCE_FALLBACK" = true ]; then
        docker compose -f docker-compose.yml -f docker-compose.official.yml "$@"
    else
        docker compose -f docker-compose.yml "$@"
    fi
}

compose_up_with_fallback() {
    if compose up -d --build; then
        return 0
    fi
    echo "[回退] 中国大陆镜像拉取或构建失败，改用 Docker Hub、PyPI、Debian 官方源重试。" >&2
    SOURCE_FALLBACK=true
    compose up -d --build
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
        select_tunnel
        if [ "$GPU_ENABLED" = true ]; then
            echo "[启动] NVIDIA GPU 模式"
        else
            echo "[启动] CPU 模式"
        fi
        echo "[等待] Compose 将依次检查依赖、拉取模型并等待 Gateway 健康。"
        compose_up_with_fallback
        wait_for_gateway
        echo ""
        echo "部署完成："
        echo "  管理后台: http://localhost/admin"
        echo "  MCP:      http://localhost/mcp"
        echo "  局域网:   将 localhost 替换为本机 IP"
        [ "$TUNNEL_MODE" != cloudflare ] || echo "  穿透:     Cloudflare Tunnel 已启动"
        ;;
    down)
        require_docker
        select_gpu
        select_tunnel
        compose down --remove-orphans
        ;;
    status)
        require_docker
        select_gpu
        select_tunnel
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
        select_tunnel
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
