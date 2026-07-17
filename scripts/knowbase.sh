#!/bin/sh

set -u

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEPLOY_SCRIPT="$ROOT_DIR/start.sh"
INSTALLER_SCRIPT="$ROOT_DIR/scripts/install-cli.sh"
HEALTH_URL=${KNOWBASE_HEALTH_URL:-http://127.0.0.1:8000/health}

usage() {
    cat <<'EOF'
Knowbase CLI

用法:
  knowbase up|start [部署参数]          启动完整 Docker 服务
  knowbase down|stop                   停止完整 Docker 服务
  knowbase restart [部署参数]           重启完整 Docker 服务
  knowbase status                      查看容器和 Gateway 健康状态
  knowbase logs                        跟踪完整服务日志
  knowbase configure|config [参数]      打开或更新部署配置
  knowbase init [参数]                  非交互初始化部署配置
  knowbase health [--url URL] [--json] 查询 Gateway 健康状态
  knowbase gateway start|stop|restart  单独管理已部署的 Gateway 容器
  knowbase gateway status|logs|health  查看 Gateway 状态、日志或健康
  knowbase cli install|uninstall|status 管理全局命令
  knowbase doctor                      检查目录、Docker、配置和健康状态
  knowbase home                        输出当前绑定的项目目录
  knowbase version                     输出 CLI 版本

示例:
  knowbase gateway restart
  knowbase health
  knowbase configure
  knowbase up --profile recommended
EOF
}

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "[错误] 未找到 Docker。" >&2
        return 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        echo "[错误] 未找到 Docker Compose v2。" >&2
        return 1
    fi
}

health_body() {
    health_target=$1
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 10 "$health_target"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- -T 10 "$health_target"
    else
        echo "[错误] health 需要 curl 或 wget。" >&2
        return 1
    fi
}

health() {
    health_target=$HEALTH_URL
    health_json=false
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --json) health_json=true; shift ;;
            --url)
                [ "$#" -ge 2 ] || { echo "[错误] --url 缺少地址。" >&2; return 2; }
                health_target=$2
                shift 2
                ;;
            --url=*) health_target=${1#*=}; shift ;;
            *) echo "[错误] health 不支持参数：$1" >&2; return 2 ;;
        esac
    done
    if ! health_output=$(health_body "$health_target"); then
        echo "[异常] Gateway 健康检查失败：$health_target" >&2
        return 1
    fi
    if [ "$health_json" = true ]; then
        printf '%s\n' "$health_output"
    else
        echo "[健康] Gateway 正常 ($health_target)"
        printf '%s\n' "$health_output"
    fi
}

wait_gateway() {
    echo "[等待] Gateway 正在启动，将持续检查 $HEALTH_URL（最多 90 秒）..."
    wait_attempt=0
    while [ "$wait_attempt" -lt 45 ]; do
        if health_body "$HEALTH_URL" >/dev/null 2>&1; then
            echo "[就绪] Gateway 健康检查通过。"
            return 0
        fi
        wait_attempt=$((wait_attempt + 1))
        if [ $((wait_attempt % 5)) -eq 0 ]; then
            echo "[等待] Gateway 尚未就绪，已等待 $((wait_attempt * 2)) 秒..."
        fi
        sleep 2
    done
    echo "[错误] Gateway 在 90 秒内未通过健康检查。" >&2
    return 1
}

gateway() {
    gateway_action=${1:-}
    [ "$#" -eq 0 ] || shift
    case "$gateway_action" in
        health) health "$@"; return ;;
        start|stop|restart|status|logs) ;;
        *) echo "用法: knowbase gateway start|stop|restart|status|logs|health" >&2; return 2 ;;
    esac
    require_docker || return 1
    cd "$ROOT_DIR" || return 1
    case "$gateway_action" in
        start) docker compose -f docker-compose.yml start mcp-gateway ;;
        stop) docker compose -f docker-compose.yml stop mcp-gateway ;;
        restart) docker compose -f docker-compose.yml restart mcp-gateway ;;
        status)
            docker compose -f docker-compose.yml ps mcp-gateway || return $?
            health
            return
            ;;
        logs)
            if [ "$#" -gt 0 ]; then
                docker compose -f docker-compose.yml logs "$@" mcp-gateway
            else
                docker compose -f docker-compose.yml logs -f mcp-gateway
            fi
            return
            ;;
    esac
    gateway_code=$?
    if [ "$gateway_code" -ne 0 ]; then
        [ "$gateway_action" != start ] || echo "[提示] Gateway 容器尚未创建时，请先运行 knowbase up。" >&2
        return "$gateway_code"
    fi
    case "$gateway_action" in start|restart) wait_gateway ;; esac
}

cli_manage() {
    cli_action=${1:-status}
    [ "$#" -eq 0 ] || shift
    case "$cli_action" in
        install|uninstall|status) sh "$INSTALLER_SCRIPT" "$cli_action" "$@" ;;
        *) echo "用法: knowbase cli install|uninstall|status" >&2; return 2 ;;
    esac
}

doctor() {
    doctor_failed=false
    echo "[目录] $ROOT_DIR"
    if [ -f "$ROOT_DIR/.env" ]; then
        echo "[配置] .env 已存在"
    else
        echo "[配置] .env 尚未生成；运行 knowbase configure" >&2
        doctor_failed=true
    fi
    if require_docker; then
        echo "[Docker] Compose 可用"
    else
        doctor_failed=true
    fi
    health || doctor_failed=true
    [ "$doctor_failed" = false ]
}

command_name=${1:-help}
[ "$#" -eq 0 ] || shift
case "$command_name" in
    up|start) exec sh "$DEPLOY_SCRIPT" up "$@" ;;
    down|stop) exec sh "$DEPLOY_SCRIPT" down "$@" ;;
    restart)
        sh "$DEPLOY_SCRIPT" down || exit $?
        exec sh "$DEPLOY_SCRIPT" up "$@"
        ;;
    status) exec sh "$DEPLOY_SCRIPT" status "$@" ;;
    logs) exec sh "$DEPLOY_SCRIPT" logs "$@" ;;
    configure|config) exec sh "$DEPLOY_SCRIPT" configure "$@" ;;
    init) exec sh "$DEPLOY_SCRIPT" init "$@" ;;
    health) health "$@" ;;
    gateway) gateway "$@" ;;
    cli) cli_manage "$@" ;;
    doctor) doctor ;;
    native)
        echo "[错误] Linux 不提供原生服务编排，请使用 Docker 命令。" >&2
        exit 2
        ;;
    home) printf '%s\n' "$ROOT_DIR" ;;
    version) echo "knowbase CLI 1.0" ;;
    help|-h|--help) usage ;;
    *)
        echo "[错误] 未知命令：$command_name" >&2
        usage >&2
        exit 2
        ;;
esac
