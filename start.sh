#!/bin/sh
# Docker deployment adapter for Linux/macOS/WSL.
# Interface: sh ./start.sh [up|down|status|logs|init|configure|cli-install|cli-uninstall|cli-status] [--cpu|--gpu] [--profile NAME] [--tunnel MODE] [--source MODE]

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="$ROOT_DIR/.env"
NETWORK_DETECTION_SCRIPT="$ROOT_DIR/scripts/network-detection.sh"
ACCESS_MODES_SCRIPT="$ROOT_DIR/scripts/access-modes.sh"
for REQUIRED_SCRIPT in "$NETWORK_DETECTION_SCRIPT" "$ACCESS_MODES_SCRIPT"; do
    [ -f "$REQUIRED_SCRIPT" ] || { echo "[错误] 部署辅助脚本不存在：$REQUIRED_SCRIPT" >&2; exit 1; }
done
. "$NETWORK_DETECTION_SCRIPT"
. "$ACCESS_MODES_SCRIPT"
COMMAND=up
GPU_MODE=""
PROFILE_MODE=auto
TUNNEL_MODE=""
IMAGE_SOURCE_MODE=""
NON_INTERACTIVE=false
INSTALL_CLI=false
HAS_DEPLOYMENT_OVERRIDES=false
SOURCE_FALLBACK=false
ENV_CREATED=false

case "${1:-}" in
    up|down|status|logs|init|configure|cli-install|cli-uninstall|cli-status|help|-h|--help)
        COMMAND=$1
        shift
        ;;
esac

while [ "$#" -gt 0 ]; do
    case "$1" in
        --cpu) GPU_MODE=cpu; HAS_DEPLOYMENT_OVERRIDES=true; shift ;;
        --gpu) GPU_MODE=gpu; HAS_DEPLOYMENT_OVERRIDES=true; shift ;;
        --profile)
            [ "$#" -ge 2 ] || { echo "[错误] --profile 缺少档位名称。" >&2; exit 2; }
            PROFILE_MODE=$2
            [ "$PROFILE_MODE" = auto ] || HAS_DEPLOYMENT_OVERRIDES=true
            shift 2
            ;;
        --profile=*) PROFILE_MODE=${1#*=}; [ "$PROFILE_MODE" = auto ] || HAS_DEPLOYMENT_OVERRIDES=true; shift ;;
        --tunnel)
            [ "$#" -ge 2 ] || { echo "[错误] --tunnel 缺少模式名称。" >&2; exit 2; }
            TUNNEL_MODE=$2
            HAS_DEPLOYMENT_OVERRIDES=true
            shift 2
            ;;
        --tunnel=*) TUNNEL_MODE=${1#*=}; HAS_DEPLOYMENT_OVERRIDES=true; shift ;;
        --source)
            [ "$#" -ge 2 ] || { echo "[错误] --source 缺少模式名称。" >&2; exit 2; }
            IMAGE_SOURCE_MODE=$2
            HAS_DEPLOYMENT_OVERRIDES=true
            shift 2
            ;;
        --source=*) IMAGE_SOURCE_MODE=${1#*=}; HAS_DEPLOYMENT_OVERRIDES=true; shift ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        --install-cli) INSTALL_CLI=true; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

case "$PROFILE_MODE" in
    auto|minimum|recommended|high-performance) ;;
    *) echo "[错误] 硬件档位只能是 auto、minimum、recommended 或 high-performance。" >&2; exit 2 ;;
esac

[ -z "$TUNNEL_MODE" ] || case "$TUNNEL_MODE" in
    auto|off|cloudflare) ;;
    *) echo "[错误] 穿透模式只能是 auto、off 或 cloudflare。" >&2; exit 2 ;;
esac

[ -z "$IMAGE_SOURCE_MODE" ] || case "$IMAGE_SOURCE_MODE" in
    auto|mainland|official) ;;
    *) echo "[错误] 镜像源只能是 auto、mainland 或 official。" >&2; exit 2 ;;
esac

cd "$ROOT_DIR"

usage() {
    cat <<'EOF'
用法: sh ./start.sh [up|down|status|logs|init|configure|cli-install|cli-uninstall|cli-status] [--cpu|--gpu] [--profile NAME] [--tunnel MODE] [--source MODE] [--non-interactive] [--install-cli]

  up         首次部署时运行交互向导，然后构建并等待 Gateway 就绪（默认）
  configure  交互式查看并重新配置硬件、镜像、网络、存储或初始管理员
  init       非交互创建/修复 .env，不启动服务
  down       停止 Docker 服务
  status     查看容器状态并检查 Gateway
  logs       跟踪所有容器日志
  cli-install   注册用户级全局 knowbase 命令并加入 PATH
  cli-uninstall 删除全局 knowbase 命令及 PATH 配置
  cli-status    检查全局 knowbase 命令状态

  首次 up 无参数时显示向导；显式参数或 --non-interactive 保持自动化兼容。
  选择结果会写入 .env，后续 up/down/status/logs 自动复用。
  示例: sh ./start.sh up --tunnel cloudflare --install-cli / sh ./start.sh cli-install / knowbase gateway restart
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

ensure_env_value() {
    ensure_key=$1
    ensure_default=$2
    ensure_current=$(read_env_value "$ensure_key")
    [ -n "$ensure_current" ] || set_env_value "$ensure_key" "$ensure_default"
}

env_value_or_default() {
    value_key=$1
    value_default=$2
    value_current=$(read_env_value "$value_key")
    if [ -n "$value_current" ]; then
        printf '%s\n' "$value_current"
    else
        printf '%s\n' "$value_default"
    fi
}

is_interactive() {
    [ "$NON_INTERACTIVE" = false ] && [ -t 0 ]
}

prompt_value() {
    prompt_label=$1
    prompt_default=$2
    printf '%s [%s]: ' "$prompt_label" "$prompt_default" >&2
    IFS= read -r prompt_answer || prompt_answer=""
    if [ -n "$prompt_answer" ]; then
        PROMPT_VALUE=$prompt_answer
    else
        PROMPT_VALUE=$prompt_default
    fi
}

prompt_secret() {
    prompt_label=$1
    prompt_current=$2
    printf '%s（留空保持当前值）: ' "$prompt_label" >&2
    if [ -t 0 ] && command -v stty >/dev/null 2>&1; then
        prompt_stty=$(stty -g)
        trap 'stty "$prompt_stty" 2>/dev/null || true' EXIT INT TERM
        stty -echo
        IFS= read -r prompt_answer || prompt_answer=""
        stty "$prompt_stty"
        trap - EXIT INT TERM
        printf '\n' >&2
    else
        IFS= read -r prompt_answer || prompt_answer=""
    fi
    if [ -n "$prompt_answer" ]; then
        PROMPT_VALUE=$prompt_answer
    else
        PROMPT_VALUE=$prompt_current
    fi
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

initialize_deployment_metadata() {
    metadata_access_modes=$(read_env_value DEPLOY_ACCESS_MODES)
    metadata_access=$(read_env_value DEPLOY_ACCESS_MODE)
    if [ -z "$metadata_access" ]; then
        metadata_external=$(read_env_value EXTERNAL_DOMAIN)
        metadata_internal_ip=$(read_env_value INTERNAL_IP)
        if [ -n "$metadata_external" ] && [ "$metadata_external" != "kb.company.com" ]; then
            metadata_access=domain
        elif [ "$metadata_internal_ip" = "127.0.0.1" ]; then
            metadata_access=local
        else
            metadata_access=lan
        fi
    fi

    metadata_access_modes=$(knowbase_normalize_access_modes "$metadata_access_modes" "$metadata_access")
    if [ "$(env_value_or_default DEPLOY_TUNNEL_MODE off)" = cloudflare ] && ! knowbase_access_modes_contains "$metadata_access_modes" cloudflare; then
        metadata_access_modes=$(knowbase_update_access_modes_for_tunnel "$metadata_access_modes" cloudflare)
    fi
    set_access_modes_metadata "$metadata_access_modes"

    metadata_external=$(read_env_value EXTERNAL_DOMAIN)
    if knowbase_access_modes_contains "$metadata_access_modes" domain && [ -z "$(read_env_value PUBLIC_DOMAIN)" ] && [ -n "$metadata_external" ]; then
        set_env_value PUBLIC_DOMAIN "$metadata_external"
    fi
    if knowbase_access_modes_contains "$metadata_access_modes" cloudflare && [ -z "$(read_env_value CLOUDFLARE_PUBLIC_HOSTNAME)" ] && [ -n "$metadata_external" ]; then
        set_env_value CLOUDFLARE_PUBLIC_HOSTNAME "$metadata_external"
    fi

    metadata_source=$(read_env_value DEPLOY_IMAGE_SOURCE)
    if [ -z "$metadata_source" ]; then
        if [ -n "$(read_env_value MIRROR_PREFIX)" ]; then
            metadata_source=mainland
        else
            metadata_source=official
        fi
        set_env_value DEPLOY_IMAGE_SOURCE "$metadata_source"
    fi

    ensure_env_value DEPLOY_GPU_MODE auto
    ensure_env_value DEPLOY_TUNNEL_MODE off
    ensure_env_value DEPLOY_CONFIGURED true
}

deployment_is_configured() {
    [ "$(env_value_or_default DEPLOY_CONFIGURED true)" = true ]
}

set_access_modes_metadata() {
    access_metadata_modes=$(knowbase_normalize_access_modes "${1:-}")
    set_env_value DEPLOY_ACCESS_MODES "$access_metadata_modes"
    set_env_value DEPLOY_ACCESS_MODE "$(knowbase_legacy_access_mode "$access_metadata_modes")"
    if knowbase_access_modes_contains "$access_metadata_modes" cloudflare; then
        set_env_value DEPLOY_TUNNEL_MODE cloudflare
    else
        set_env_value DEPLOY_TUNNEL_MODE off
    fi
}

apply_command_configuration_overrides() {
    [ -z "$GPU_MODE" ] || set_env_value DEPLOY_GPU_MODE "$GPU_MODE"
    if [ -n "$TUNNEL_MODE" ] && [ "$TUNNEL_MODE" != auto ]; then
        override_access_modes=$(env_value_or_default DEPLOY_ACCESS_MODES lan)
        set_access_modes_metadata "$(knowbase_update_access_modes_for_tunnel "$override_access_modes" "$TUNNEL_MODE")"
    fi
    if [ -n "$IMAGE_SOURCE_MODE" ] && [ "$IMAGE_SOURCE_MODE" != auto ]; then
        set_env_value DEPLOY_IMAGE_SOURCE "$IMAGE_SOURCE_MODE"
    fi
}

configure_hardware() {
    echo ""
    echo "硬件与并发档位"
    echo "  1) minimum          4 核 / 8 GB / 1–5 人"
    echo "  2) recommended      8 核 / 16 GB / 10–20 人间歇使用"
    echo "  3) high-performance 12 核+ / 32 GB+ / 持续并发"
    cfg_current=$(env_value_or_default HARDWARE_PROFILE recommended)
    while :; do
        prompt_value "请选择档位" "$cfg_current"
        case "$PROMPT_VALUE" in
            1|minimum) cfg_profile=minimum; break ;;
            2|recommended) cfg_profile=recommended; break ;;
            3|high-performance) cfg_profile=high-performance; break ;;
            *) echo "输入无效，请重新选择。" >&2 ;;
        esac
    done
    apply_hardware_profile "$cfg_profile"

    echo "  GPU: 1) 自动检测  2) 强制 CPU  3) NVIDIA GPU"
    cfg_current=$(env_value_or_default DEPLOY_GPU_MODE auto)
    while :; do
        prompt_value "请选择 GPU 模式" "$cfg_current"
        case "$PROMPT_VALUE" in
            1|auto) cfg_gpu=auto; break ;;
            2|cpu) cfg_gpu=cpu; break ;;
            3|gpu) cfg_gpu=gpu; break ;;
            *) echo "输入无效，请重新选择。" >&2 ;;
        esac
    done
    set_env_value DEPLOY_GPU_MODE "$cfg_gpu"
}

configure_image_source() {
    echo ""
    echo "镜像与软件源"
    echo "  1) 中国大陆镜像优先，失败后自动回退官方源"
    echo "  2) 直接使用 Docker Hub / PyPI / Debian 官方源"
    cfg_current=$(env_value_or_default DEPLOY_IMAGE_SOURCE mainland)
    while :; do
        prompt_value "请选择镜像源" "$cfg_current"
        case "$PROMPT_VALUE" in
            1|mainland) cfg_source=mainland; break ;;
            2|official) cfg_source=official; break ;;
            *) echo "输入无效，请重新选择。" >&2 ;;
        esac
    done
    set_env_value DEPLOY_IMAGE_SOURCE "$cfg_source"
}

prompt_required_domain() {
    cfg_domain_default=$1
    cfg_domain_label=${2:-请输入访问域名}
    while :; do
        prompt_value "$cfg_domain_label" "$cfg_domain_default"
        case "$PROMPT_VALUE" in
            ''|*' '*) echo "域名不能为空且不能包含空格。" >&2 ;;
            *) REQUIRED_DOMAIN=$PROMPT_VALUE; return 0 ;;
        esac
    done
}

configure_internal_hosts() {
    cfg_internal_current=$1
    cfg_detected_hostname=$(knowbase_detect_hostname)
    cfg_detected_ipv4s=$(knowbase_detect_lan_ipv4s)
    cfg_detected_value=$(knowbase_normalize_internal_host_value "$cfg_detected_hostname $cfg_detected_ipv4s")
    cfg_internal_default=$(knowbase_suggest_internal_host_value "$cfg_internal_current" "$cfg_detected_value")
    cfg_ipv4_summary=$(printf '%s\n' "$cfg_detected_ipv4s" | awk 'NF { if (result != "") result = result ", "; result = result $0 } END { print result }')
    [ -n "$cfg_detected_hostname" ] || cfg_detected_hostname=未检测到
    [ -n "$cfg_ipv4_summary" ] || cfg_ipv4_summary=未检测到，请确认网卡已连接
    echo "[检测] 计算机名：$cfg_detected_hostname"
    echo "[检测] 局域网 IPv4：$cfg_ipv4_summary"
    prompt_value "内网访问名称（计算机名/IP；多个值用逗号分隔，回车使用检测值）" "$cfg_internal_default"
    CONFIGURED_INTERNAL_HOSTS=$(knowbase_normalize_internal_host_value "$PROMPT_VALUE")
    [ -n "$CONFIGURED_INTERNAL_HOSTS" ] || CONFIGURED_INTERNAL_HOSTS=localhost
}

configure_network() {
    cfg_current_modes=$(env_value_or_default DEPLOY_ACCESS_MODES lan)
    knowbase_select_access_modes "$cfg_current_modes"
    cfg_access_modes=$KNOWBASE_SELECTED_ACCESS_MODES
    echo ""
    echo "具体配置"

    if knowbase_access_modes_contains "$cfg_access_modes" local; then
        echo "[本机] 无需额外设置，将保留 localhost 访问。"
    fi

    if knowbase_access_modes_contains "$cfg_access_modes" lan; then
        echo ""
        echo "局域网访问设置"
        configure_internal_hosts "$(env_value_or_default INTERNAL_DOMAIN localhost)"
        cfg_internal_domain=$CONFIGURED_INTERNAL_HOSTS
    else
        cfg_internal_domain=localhost
    fi

    cfg_public_domain=$(read_env_value PUBLIC_DOMAIN)
    cfg_active_public_domain=""
    if knowbase_access_modes_contains "$cfg_access_modes" domain; then
        echo ""
        echo "公网访问设置"
        if [ -z "$cfg_public_domain" ]; then
            if knowbase_access_modes_contains "$cfg_current_modes" domain; then
                cfg_public_domain=$(env_value_or_default EXTERNAL_DOMAIN kb.example.com)
            else
                cfg_public_domain=kb.example.com
            fi
        fi
        prompt_required_domain "$cfg_public_domain" "公网访问域名"
        cfg_public_domain=$REQUIRED_DOMAIN
        cfg_active_public_domain=$cfg_public_domain
        set_env_value PUBLIC_DOMAIN "$cfg_public_domain"
        echo "[提示] 请把证书放入 nginx/ssl/$cfg_public_domain/。" >&2
    fi

    cfg_cloudflare_hostname=$(read_env_value CLOUDFLARE_PUBLIC_HOSTNAME)
    cfg_active_cloudflare_hostname=""
    if knowbase_access_modes_contains "$cfg_access_modes" cloudflare; then
        echo ""
        echo "Cloudflare Tunnel 设置"
        if [ -z "$cfg_cloudflare_hostname" ]; then
            if knowbase_access_modes_contains "$cfg_current_modes" cloudflare && ! knowbase_access_modes_contains "$cfg_current_modes" domain; then
                cfg_cloudflare_hostname=$(env_value_or_default EXTERNAL_DOMAIN kb-tunnel.example.com)
            elif [ -n "$cfg_active_public_domain" ]; then
                cfg_cloudflare_hostname=tunnel.$cfg_active_public_domain
            else
                cfg_cloudflare_hostname=kb-tunnel.example.com
            fi
        fi
        if [ -n "$cfg_active_public_domain" ] && [ "$(printf '%s' "$cfg_cloudflare_hostname" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$cfg_active_public_domain" | tr '[:upper:]' '[:lower:]')" ]; then
            cfg_cloudflare_hostname=tunnel.$cfg_active_public_domain
        fi
        while :; do
            prompt_required_domain "$cfg_cloudflare_hostname" "Cloudflare Public Hostname"
            cfg_cloudflare_hostname=$REQUIRED_DOMAIN
            if [ -z "$cfg_active_public_domain" ] || [ "$(printf '%s' "$cfg_cloudflare_hostname" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$cfg_active_public_domain" | tr '[:upper:]' '[:lower:]')" ]; then
                break
            fi
            echo "公网直连域名和 Tunnel Hostname 应使用不同名称，例如 tunnel.$cfg_active_public_domain。" >&2
            cfg_cloudflare_hostname=tunnel.$cfg_active_public_domain
        done
        prompt_secret "Cloudflare Tunnel Token" "$(read_env_value CLOUDFLARE_TUNNEL_TOKEN)"
        [ -n "$PROMPT_VALUE" ] || { echo "[错误] Cloudflare Tunnel 必须提供 Token。" >&2; exit 1; }
        cfg_active_cloudflare_hostname=$cfg_cloudflare_hostname
        set_env_value CLOUDFLARE_PUBLIC_HOSTNAME "$cfg_cloudflare_hostname"
        set_env_value CLOUDFLARE_TUNNEL_TOKEN "$PROMPT_VALUE"
    fi

    if [ -n "$cfg_active_public_domain" ]; then
        cfg_runtime_external_domain=$cfg_active_public_domain
    else
        cfg_runtime_external_domain=$cfg_active_cloudflare_hostname
    fi
    if knowbase_access_modes_contains "$cfg_access_modes" lan || knowbase_access_modes_contains "$cfg_access_modes" domain; then
        cfg_internal_bind=0.0.0.0
    else
        cfg_internal_bind=127.0.0.1
    fi
    if knowbase_access_modes_contains "$cfg_access_modes" domain; then cfg_external_bind=0.0.0.0; else cfg_external_bind=127.0.0.1; fi
    cfg_cors_origins=""
    [ -z "$cfg_active_public_domain" ] || cfg_cors_origins=https://$cfg_active_public_domain
    [ -z "$cfg_active_cloudflare_hostname" ] || cfg_cors_origins=${cfg_cors_origins:+$cfg_cors_origins,}https://$cfg_active_cloudflare_hostname
    if knowbase_access_modes_contains "$cfg_access_modes" lan || [ -z "$cfg_cors_origins" ]; then cfg_cors_origins='*'; fi

    set_env_value INTERNAL_DOMAIN "$cfg_internal_domain"
    set_env_value EXTERNAL_DOMAIN "$cfg_runtime_external_domain"
    set_env_value INTERNAL_IP "$cfg_internal_bind"
    set_env_value EXTERNAL_IP "$cfg_external_bind"
    set_env_value CORS_ORIGINS "$cfg_cors_origins"
    set_access_modes_metadata "$cfg_access_modes"
    if knowbase_access_modes_contains "$cfg_access_modes" cloudflare; then
        echo "[提示] Cloudflare Public Hostname 上游应配置为 http://nginx:80。" >&2
    fi
}

configure_storage() {
    echo ""
    echo "数据与模型"
    prompt_value "宿主机数据目录（Windows 绝对路径建议使用正斜杠）" "$(env_value_or_default HOST_KBDATA_DIR ./kbdata)"
    cfg_data_path=$PROMPT_VALUE
    prompt_value "Ollama Embedding 模型" "$(env_value_or_default OLLAMA_MODEL bge-m3)"
    set_env_value HOST_KBDATA_DIR "$cfg_data_path"
    set_env_value OLLAMA_MODEL "$PROMPT_VALUE"
}

configure_admin() {
    echo ""
    echo "初始管理员（仅账号库为空时生效）"
    prompt_value "管理员用户名" "$(env_value_or_default ADMIN_INITIAL_USERNAME admin)"
    cfg_admin_user=$PROMPT_VALUE
    prompt_secret "管理员初始密码" "$(env_value_or_default ADMIN_INITIAL_PASSWORD 123456)"
    set_env_value ADMIN_INITIAL_USERNAME "$cfg_admin_user"
    set_env_value ADMIN_INITIAL_PASSWORD "$PROMPT_VALUE"
    [ "$PROMPT_VALUE" != 123456 ] || echo "[提示] 当前仍使用 Demo 默认密码 123456。局域网多人使用前建议修改。" >&2
}

show_configuration_summary() {
    if [ -n "$(read_env_value CLOUDFLARE_TUNNEL_TOKEN)" ]; then
        cfg_token_status='是（已隐藏）'
    else
        cfg_token_status=否
    fi
    echo ""
    echo "当前部署配置"
    echo "  硬件档位: $(env_value_or_default HARDWARE_PROFILE recommended)"
    echo "  GPU 模式:  $(env_value_or_default DEPLOY_GPU_MODE auto)"
    echo "  镜像源:    $(env_value_or_default DEPLOY_IMAGE_SOURCE mainland)"
    cfg_summary_modes=$(env_value_or_default DEPLOY_ACCESS_MODES lan)
    echo "  访问方式:  $(knowbase_access_mode_labels "$cfg_summary_modes")"
    if knowbase_access_modes_contains "$cfg_summary_modes" lan; then echo "  局域网名称/IP: $(env_value_or_default INTERNAL_DOMAIN localhost)"; fi
    if knowbase_access_modes_contains "$cfg_summary_modes" domain; then echo "  公网域名:  $(env_value_or_default PUBLIC_DOMAIN 未配置)"; fi
    if knowbase_access_modes_contains "$cfg_summary_modes" cloudflare; then
        echo "  Tunnel:    $(env_value_or_default CLOUDFLARE_PUBLIC_HOSTNAME 未配置) / Token $cfg_token_status"
    fi
    echo "  数据目录:  $(env_value_or_default HOST_KBDATA_DIR ./kbdata)"
    echo "  模型:      $(env_value_or_default OLLAMA_MODEL bge-m3)"
    echo "  初始管理员: $(env_value_or_default ADMIN_INITIAL_USERNAME admin)"
    echo "  配置文件:  $ENV_FILE"
}

run_configuration_wizard() {
    cfg_initial=$1
    is_interactive || { echo "[错误] 配置向导需要交互式终端；自动化环境请使用 init/configure --non-interactive 及 --profile/--cpu/--gpu/--source/--tunnel 参数。" >&2; exit 1; }

    echo ""
    echo "============================================"
    echo "  Knowledge Base Management 部署配置向导"
    echo "============================================"

    if [ "$cfg_initial" = true ]; then
        configure_hardware
        configure_image_source
        configure_network
        configure_storage
        configure_admin
    else
        while :; do
            show_configuration_summary
            echo ""
            echo "重新配置：1) 硬件/GPU  2) 镜像源  3) 访问方式  4) 数据/模型  5) 初始管理员  6) 全部  0) 完成"
            prompt_value "请选择要修改的部分" 0
            case "$PROMPT_VALUE" in
                0|done) break ;;
                1|hardware) configure_hardware ;;
                2|source) configure_image_source ;;
                3|network) configure_network ;;
                4|storage) configure_storage ;;
                5|admin) configure_admin ;;
                6|all)
                    configure_hardware
                    configure_image_source
                    configure_network
                    configure_storage
                    configure_admin
                    ;;
                *) echo "输入无效，请重新选择。" >&2 ;;
            esac
        done
    fi
    set_env_value DEPLOY_CONFIGURED true
    show_configuration_summary
    echo "[完成] 配置已保存。服务已运行时，请重新执行 up 使修改生效。"
}

initialize_env() {
    ENV_CREATED=false
    if [ ! -f "$ENV_FILE" ]; then
        cp "$ROOT_DIR/.env.example" "$ENV_FILE"
        ENV_CREATED=true
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

    if [ "$ENV_CREATED" = true ]; then
        set_env_value EXTERNAL_DOMAIN ""
        set_env_value INTERNAL_DOMAIN "localhost"
    fi
    initialize_deployment_metadata
    if [ "$PROFILE_MODE" != "auto" ]; then
        apply_hardware_profile "$PROFILE_MODE"
    elif [ "$ENV_CREATED" = true ]; then
        apply_hardware_profile recommended
    fi
    apply_command_configuration_overrides
    chmod 600 "$ENV_FILE" 2>/dev/null || true
}

resolve_deployment_options() {
    if [ -z "$GPU_MODE" ]; then
        GPU_MODE=$(env_value_or_default DEPLOY_GPU_MODE auto)
    fi
    if [ -z "$TUNNEL_MODE" ] || [ "$TUNNEL_MODE" = auto ]; then
        TUNNEL_MODE=$(env_value_or_default DEPLOY_TUNNEL_MODE off)
    fi
    if [ -z "$IMAGE_SOURCE_MODE" ] || [ "$IMAGE_SOURCE_MODE" = auto ]; then
        IMAGE_SOURCE_MODE=$(env_value_or_default DEPLOY_IMAGE_SOURCE mainland)
    fi

    case "$GPU_MODE" in auto|cpu|gpu) ;; *) echo "[错误] DEPLOY_GPU_MODE 只能是 auto、cpu 或 gpu。" >&2; exit 2 ;; esac
    case "$TUNNEL_MODE" in off|cloudflare) ;; *) echo "[错误] DEPLOY_TUNNEL_MODE 只能是 off 或 cloudflare。" >&2; exit 2 ;; esac
    case "$IMAGE_SOURCE_MODE" in mainland|official) ;; *) echo "[错误] DEPLOY_IMAGE_SOURCE 只能是 mainland 或 official。" >&2; exit 2 ;; esac

    if [ "$IMAGE_SOURCE_MODE" = official ]; then
        SOURCE_FALLBACK=true
    fi
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
    tunnel_hostname=$(env_value_or_default CLOUDFLARE_PUBLIC_HOSTNAME 已在Cloudflare控制台配置的Hostname)
    echo "[穿透] Cloudflare Tunnel 已启用：$tunnel_hostname；上游应配置为 http://nginx:80"
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
    if compose up -d --build --remove-orphans; then
        return 0
    fi
    if [ "$IMAGE_SOURCE_MODE" = official ]; then
        echo "[错误] 官方镜像源启动失败，请检查上方 Docker 输出。" >&2
        return 1
    fi
    echo "[回退] 中国大陆镜像拉取或构建失败，改用 Docker Hub、PyPI、Debian 官方源重试。" >&2
    SOURCE_FALLBACK=true
    compose up -d --build --remove-orphans
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

show_deployment_access_urls() {
    deployment_access_modes=$(env_value_or_default DEPLOY_ACCESS_MODES lan)
    deployment_internal_hosts=""
    if knowbase_access_modes_contains "$deployment_access_modes" local; then deployment_internal_hosts=localhost; fi
    if knowbase_access_modes_contains "$deployment_access_modes" lan; then
        deployment_lan_hosts=$(env_value_or_default INTERNAL_DOMAIN localhost)
        deployment_internal_hosts=${deployment_internal_hosts:+$deployment_internal_hosts,}$deployment_lan_hosts
    fi
    deployment_old_ifs=$IFS
    IFS=',; '
    echo ""
    echo "部署完成："
    for deployment_internal_host in $deployment_internal_hosts; do
        [ -n "$deployment_internal_host" ] || continue
        echo "  管理后台: http://$deployment_internal_host/admin"
        echo "  MCP:      http://$deployment_internal_host/mcp"
    done
    IFS=$deployment_old_ifs
    if knowbase_access_modes_contains "$deployment_access_modes" domain; then
        deployment_public_domain=$(env_value_or_default PUBLIC_DOMAIN "$(read_env_value EXTERNAL_DOMAIN)")
        echo "  公网访问: https://$deployment_public_domain/"
        echo "  公网 MCP: https://$deployment_public_domain/mcp"
    fi
    if knowbase_access_modes_contains "$deployment_access_modes" cloudflare; then
        deployment_tunnel_hostname=$(env_value_or_default CLOUDFLARE_PUBLIC_HOSTNAME "$(read_env_value EXTERNAL_DOMAIN)")
        echo "  Tunnel:   https://$deployment_tunnel_hostname/"
        echo "  Tunnel MCP: https://$deployment_tunnel_hostname/mcp"
    fi
}

cli_installer() {
    cli_action=$1
    shift
    sh "$ROOT_DIR/scripts/install-cli.sh" "$cli_action" "$@"
}

cli_is_registered() {
    cli_installer status --quiet >/dev/null 2>&1
}

install_cli_if_requested() {
    if [ "$INSTALL_CLI" = true ]; then
        cli_installer install
        return
    fi
    if [ "$NON_INTERACTIVE" = true ] || ! is_interactive || cli_is_registered; then
        return
    fi
    printf '是否注册全局 knowbase 命令，可在任意新终端使用？[Y/n] '
    IFS= read -r cli_choice || cli_choice=n
    case "$cli_choice" in
        ""|y|Y|yes|YES|Yes) cli_installer install ;;
        *) echo "[跳过] 稍后可运行 sh ./start.sh cli-install。" ;;
    esac
}

case "$COMMAND" in
    cli-install)
        cli_installer install
        ;;
    cli-uninstall)
        cli_installer uninstall
        ;;
    cli-status)
        cli_installer status
        ;;
    init)
        initialize_env
        set_env_value DEPLOY_CONFIGURED true
        resolve_deployment_options
        show_configuration_summary
        echo "[完成] 配置位于 $ENV_FILE"
        ;;
    configure)
        initialize_env
        CONFIGURATION_NEEDS_FULL_SETUP=false
        if [ "$ENV_CREATED" = true ] || ! deployment_is_configured; then
            CONFIGURATION_NEEDS_FULL_SETUP=true
        fi
        if [ "$NON_INTERACTIVE" = true ]; then
            set_env_value DEPLOY_CONFIGURED true
            show_configuration_summary
            echo "[完成] 已按命令参数更新配置。"
        else
            run_configuration_wizard "$CONFIGURATION_NEEDS_FULL_SETUP"
        fi
        ;;
    up)
        initialize_env
        CONFIGURATION_NEEDS_FULL_SETUP=false
        if [ "$ENV_CREATED" = true ] || ! deployment_is_configured; then
            CONFIGURATION_NEEDS_FULL_SETUP=true
        fi
        if [ "$CONFIGURATION_NEEDS_FULL_SETUP" = true ] && [ "$HAS_DEPLOYMENT_OVERRIDES" = false ] && is_interactive; then
            run_configuration_wizard true
        elif [ "$CONFIGURATION_NEEDS_FULL_SETUP" = true ]; then
            set_env_value DEPLOY_CONFIGURED true
        fi
        resolve_deployment_options
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
        show_deployment_access_urls
        [ "$TUNNEL_MODE" != cloudflare ] || echo "  穿透:     Cloudflare Tunnel 已启动"
        install_cli_if_requested
        ;;
    down)
        require_docker
        resolve_deployment_options
        select_gpu
        select_tunnel
        compose down --remove-orphans
        ;;
    status)
        require_docker
        resolve_deployment_options
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
        resolve_deployment_options
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
