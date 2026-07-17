#!/bin/sh

knowbase_normalize_access_modes() {
    kb_modes_value=${1:-}
    kb_modes_legacy=${2:-}
    kb_modes_result=""
    for kb_modes_token in $(printf '%s' "$kb_modes_value" | tr ',;' '  '); do
        kb_modes_token=$(printf '%s' "$kb_modes_token" | tr '[:upper:]' '[:lower:]')
        case "$kb_modes_token" in
            local|localhost) kb_modes_normalized=local ;;
            lan|internal) kb_modes_normalized=lan ;;
            domain|public|external) kb_modes_normalized=domain ;;
            cloudflare|tunnel) kb_modes_normalized=cloudflare ;;
            *) kb_modes_normalized="" ;;
        esac
        [ -n "$kb_modes_normalized" ] || continue
        case ",$kb_modes_result," in
            *",$kb_modes_normalized,"*) ;;
            *) kb_modes_result=${kb_modes_result:+$kb_modes_result,}$kb_modes_normalized ;;
        esac
    done

    if [ -z "$kb_modes_result" ]; then
        case "$(printf '%s' "$kb_modes_legacy" | tr '[:upper:]' '[:lower:]')" in
            local) kb_modes_result=local ;;
            lan) kb_modes_result=lan ;;
            domain) kb_modes_result=lan,domain ;;
            cloudflare) kb_modes_result=local,cloudflare ;;
            *) kb_modes_result=lan ;;
        esac
    fi

    kb_modes_ordered=""
    for kb_modes_candidate in local lan domain cloudflare; do
        case ",$kb_modes_result," in
            *",$kb_modes_candidate,"*) kb_modes_ordered=${kb_modes_ordered:+$kb_modes_ordered,}$kb_modes_candidate ;;
        esac
    done
    printf '%s\n' "$kb_modes_ordered"
}

knowbase_access_modes_contains() {
    kb_modes_contains_value=$(knowbase_normalize_access_modes "${1:-}")
    case ",$kb_modes_contains_value," in
        *",${2:-},"*) return 0 ;;
        *) return 1 ;;
    esac
}

knowbase_legacy_access_mode() {
    kb_modes_legacy_value=$(knowbase_normalize_access_modes "${1:-}")
    case "$kb_modes_legacy_value" in
        *,*) printf '%s\n' hybrid ;;
        *) printf '%s\n' "$kb_modes_legacy_value" ;;
    esac
}

knowbase_access_mode_choices() {
    kb_modes_choices_value=$(knowbase_normalize_access_modes "${1:-}")
    kb_modes_choices=""
    for kb_modes_choice in local:1 lan:2 domain:3 cloudflare:4; do
        kb_modes_name=${kb_modes_choice%%:*}
        kb_modes_number=${kb_modes_choice#*:}
        if knowbase_access_modes_contains "$kb_modes_choices_value" "$kb_modes_name"; then
            kb_modes_choices=${kb_modes_choices:+$kb_modes_choices,}$kb_modes_number
        fi
    done
    printf '%s\n' "$kb_modes_choices"
}

knowbase_access_mode_labels() {
    kb_modes_labels_value=$(knowbase_normalize_access_modes "${1:-}")
    kb_modes_labels=""
    for kb_modes_label_entry in 'local:仅本机' 'lan:局域网' 'domain:公网' 'cloudflare:Cloudflare Tunnel'; do
        kb_modes_label_name=${kb_modes_label_entry%%:*}
        kb_modes_label=${kb_modes_label_entry#*:}
        if knowbase_access_modes_contains "$kb_modes_labels_value" "$kb_modes_label_name"; then
            kb_modes_labels=${kb_modes_labels:+$kb_modes_labels、}$kb_modes_label
        fi
    done
    printf '%s\n' "$kb_modes_labels"
}

knowbase_update_access_modes_for_tunnel() {
    kb_modes_update_value=$(knowbase_normalize_access_modes "${1:-}")
    kb_modes_update_tunnel=${2:-off}
    kb_modes_update_result=""
    kb_modes_update_old_ifs=$IFS
    IFS=,
    for kb_modes_update_item in $kb_modes_update_value; do
        [ "$kb_modes_update_item" = cloudflare ] && continue
        kb_modes_update_result=${kb_modes_update_result:+$kb_modes_update_result,}$kb_modes_update_item
    done
    IFS=$kb_modes_update_old_ifs
    if [ "$kb_modes_update_tunnel" = cloudflare ]; then
        kb_modes_update_result=${kb_modes_update_result:+$kb_modes_update_result,}cloudflare
    fi
    [ -n "$kb_modes_update_result" ] || kb_modes_update_result=local
    knowbase_normalize_access_modes "$kb_modes_update_result"
}

knowbase_render_access_mode_menu_line() {
    cfg_menu_line_index=$1
    cfg_menu_line_mode=$2
    cfg_menu_line_number=$3
    cfg_menu_line_label=$4
    case "$cfg_menu_line_mode" in
        local) cfg_menu_line_checked=$cfg_menu_local ;;
        lan) cfg_menu_line_checked=$cfg_menu_lan ;;
        domain) cfg_menu_line_checked=$cfg_menu_domain ;;
        cloudflare) cfg_menu_line_checked=$cfg_menu_cloudflare ;;
    esac
    if [ "$cfg_menu_line_checked" = true ]; then cfg_menu_line_mark=x; else cfg_menu_line_mark=' '; fi
    if [ "$cfg_menu_cursor" -eq "$cfg_menu_line_index" ]; then cfg_menu_line_pointer='>'; else cfg_menu_line_pointer=' '; fi
    printf '\033[2K\r  %s [%s] %s) %s\n' "$cfg_menu_line_pointer" "$cfg_menu_line_mark" "$cfg_menu_line_number" "$cfg_menu_line_label"
}

knowbase_render_access_mode_menu() {
    if [ "$cfg_menu_rendered" = true ]; then printf '\033[5A'; fi
    knowbase_render_access_mode_menu_line 1 local 1 仅本机
    knowbase_render_access_mode_menu_line 2 lan 2 局域网
    knowbase_render_access_mode_menu_line 3 domain 3 公网
    knowbase_render_access_mode_menu_line 4 cloudflare 4 'Cloudflare Tunnel'
    printf '\033[2K\r  %s\n' "$cfg_menu_status"
    cfg_menu_rendered=true
}

knowbase_restore_access_mode_terminal() {
    if [ "${cfg_menu_terminal_active:-false}" = true ]; then
        stty "$cfg_menu_saved_stty" 2>/dev/null || true
        printf '\033[?25h'
        cfg_menu_terminal_active=false
    fi
}

knowbase_read_access_mode_menu_byte() {
    cfg_menu_read_value=$(od -An -tu1 -N1 2>/dev/null | tr -d '[:space:]')
    [ -n "$cfg_menu_read_value" ] || return 1
    KNOWBASE_ACCESS_MENU_BYTE=$cfg_menu_read_value
}

knowbase_select_access_modes_by_keys() {
    cfg_menu_current=$(knowbase_normalize_access_modes "${1:-}")
    [ -t 0 ] && [ -t 1 ] || return 1
    case "${TERM:-}" in ''|dumb) return 1 ;; esac
    command -v stty >/dev/null 2>&1 || return 1
    command -v od >/dev/null 2>&1 || return 1
    cfg_menu_saved_stty=$(stty -g 2>/dev/null) || return 1

    if knowbase_access_modes_contains "$cfg_menu_current" local; then cfg_menu_local=true; else cfg_menu_local=false; fi
    if knowbase_access_modes_contains "$cfg_menu_current" lan; then cfg_menu_lan=true; else cfg_menu_lan=false; fi
    if knowbase_access_modes_contains "$cfg_menu_current" domain; then cfg_menu_domain=true; else cfg_menu_domain=false; fi
    if knowbase_access_modes_contains "$cfg_menu_current" cloudflare; then cfg_menu_cloudflare=true; else cfg_menu_cloudflare=false; fi
    cfg_menu_cursor=1
    cfg_menu_status=""
    cfg_menu_rendered=false
    cfg_menu_terminal_active=true

    if ! stty -echo -icanon min 1 time 0; then
        stty "$cfg_menu_saved_stty" 2>/dev/null || true
        cfg_menu_terminal_active=false
        return 1
    fi
    trap 'knowbase_restore_access_mode_terminal; exit 130' HUP INT TERM
    printf '\n访问方式（可多选）\n'
    printf '  ↑/↓ 移动  Space 勾选/取消  Enter 提交\n'
    printf '\033[?25l'
    knowbase_render_access_mode_menu

    while :; do
        if ! knowbase_read_access_mode_menu_byte; then
            knowbase_restore_access_mode_terminal
            trap - HUP INT TERM
            return 1
        fi
        cfg_menu_key=$KNOWBASE_ACCESS_MENU_BYTE
        case "$cfg_menu_key" in
            27)
                if ! knowbase_read_access_mode_menu_byte; then continue; fi
                cfg_menu_escape_prefix=$KNOWBASE_ACCESS_MENU_BYTE
                if ! knowbase_read_access_mode_menu_byte; then continue; fi
                cfg_menu_escape_key=$KNOWBASE_ACCESS_MENU_BYTE
                case "$cfg_menu_escape_prefix:$cfg_menu_escape_key" in
                    91:65|79:65)
                        if [ "$cfg_menu_cursor" -le 1 ]; then cfg_menu_cursor=4; else cfg_menu_cursor=$((cfg_menu_cursor - 1)); fi
                        cfg_menu_status=""
                        ;;
                    91:66|79:66)
                        if [ "$cfg_menu_cursor" -ge 4 ]; then cfg_menu_cursor=1; else cfg_menu_cursor=$((cfg_menu_cursor + 1)); fi
                        cfg_menu_status=""
                        ;;
                esac
                ;;
            32)
                case "$cfg_menu_cursor" in
                    1) if [ "$cfg_menu_local" = true ]; then cfg_menu_local=false; else cfg_menu_local=true; fi ;;
                    2) if [ "$cfg_menu_lan" = true ]; then cfg_menu_lan=false; else cfg_menu_lan=true; fi ;;
                    3) if [ "$cfg_menu_domain" = true ]; then cfg_menu_domain=false; else cfg_menu_domain=true; fi ;;
                    4) if [ "$cfg_menu_cloudflare" = true ]; then cfg_menu_cloudflare=false; else cfg_menu_cloudflare=true; fi ;;
                esac
                cfg_menu_status=""
                ;;
            10|13)
                cfg_menu_selected=""
                [ "$cfg_menu_local" = false ] || cfg_menu_selected=local
                [ "$cfg_menu_lan" = false ] || cfg_menu_selected=${cfg_menu_selected:+$cfg_menu_selected,}lan
                [ "$cfg_menu_domain" = false ] || cfg_menu_selected=${cfg_menu_selected:+$cfg_menu_selected,}domain
                [ "$cfg_menu_cloudflare" = false ] || cfg_menu_selected=${cfg_menu_selected:+$cfg_menu_selected,}cloudflare
                if [ -z "$cfg_menu_selected" ]; then
                    cfg_menu_status="至少勾选一种访问方式。"
                else
                    cfg_menu_status="已提交。"
                    knowbase_render_access_mode_menu
                    KNOWBASE_SELECTED_ACCESS_MODES=$(knowbase_normalize_access_modes "$cfg_menu_selected")
                    knowbase_restore_access_mode_terminal
                    trap - HUP INT TERM
                    return 0
                fi
                ;;
        esac
        knowbase_render_access_mode_menu
    done
}

knowbase_select_access_modes_by_numbers() {
    cfg_access_current=$(knowbase_normalize_access_modes "${1:-}")
    while :; do
        echo ""
        echo "访问方式（兼容输入模式，可多选）"
        for cfg_access_entry in '1:local:仅本机' '2:lan:局域网' '3:domain:公网' '4:cloudflare:Cloudflare Tunnel'; do
            cfg_access_number=${cfg_access_entry%%:*}
            cfg_access_remainder=${cfg_access_entry#*:}
            cfg_access_mode=${cfg_access_remainder%%:*}
            cfg_access_label=${cfg_access_remainder#*:}
            if knowbase_access_modes_contains "$cfg_access_current" "$cfg_access_mode"; then cfg_access_mark=x; else cfg_access_mark=' '; fi
            echo "  [$cfg_access_mark] $cfg_access_number) $cfg_access_label"
        done

        prompt_value "请输入要启用的编号，多个用逗号分隔" "$(knowbase_access_mode_choices "$cfg_access_current")"
        cfg_access_selected=""
        cfg_access_invalid=""
        for cfg_access_choice in $(printf '%s' "$PROMPT_VALUE" | tr ',;' '  '); do
            cfg_access_choice=$(printf '%s' "$cfg_access_choice" | tr '[:upper:]' '[:lower:]')
            case "$cfg_access_choice" in
                1|local|localhost) cfg_access_mode=local ;;
                2|lan|internal) cfg_access_mode=lan ;;
                3|domain|public|external) cfg_access_mode=domain ;;
                4|cloudflare|tunnel) cfg_access_mode=cloudflare ;;
                *) cfg_access_mode=""; cfg_access_invalid=${cfg_access_invalid:+$cfg_access_invalid, }$cfg_access_choice ;;
            esac
            [ -n "$cfg_access_mode" ] || continue
            case ",$cfg_access_selected," in
                *",$cfg_access_mode,"*) ;;
                *) cfg_access_selected=${cfg_access_selected:+$cfg_access_selected,}$cfg_access_mode ;;
            esac
        done
        if [ -n "$cfg_access_invalid" ]; then
            echo "输入包含无效选项：$cfg_access_invalid。" >&2
            continue
        fi
        if [ -z "$cfg_access_selected" ]; then
            echo "至少选择一种访问方式。" >&2
            continue
        fi
        KNOWBASE_SELECTED_ACCESS_MODES=$(knowbase_normalize_access_modes "$cfg_access_selected")
        return 0
    done
}

knowbase_select_access_modes() {
    cfg_access_current=$(knowbase_normalize_access_modes "${1:-}")
    if knowbase_select_access_modes_by_keys "$cfg_access_current"; then
        return 0
    fi
    echo "[提示] 当前终端不支持逐键菜单，已切换为编号输入。" >&2
    knowbase_select_access_modes_by_numbers "$cfg_access_current"
}
