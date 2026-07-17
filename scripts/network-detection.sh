#!/bin/sh

knowbase_is_usable_ipv4() {
    kb_address=${1:-}
    case "$kb_address" in
        ""|*[!0-9.]*) return 1 ;;
    esac

    kb_old_ifs=$IFS
    IFS=.
    set -- $kb_address
    IFS=$kb_old_ifs
    [ "$#" -eq 4 ] || return 1
    for kb_octet in "$@"; do
        [ -n "$kb_octet" ] && [ "$kb_octet" -ge 0 ] 2>/dev/null && [ "$kb_octet" -le 255 ] 2>/dev/null || return 1
    done
    [ "$1" -ne 0 ] && [ "$1" -ne 127 ] && [ "$1" -lt 224 ] || return 1
    [ "$1" -ne 169 ] || [ "$2" -ne 254 ] || return 1
    return 0
}

knowbase_detect_hostname() {
    kb_hostname_override=${1:-}
    if [ -n "$kb_hostname_override" ]; then
        printf '%s\n' "$kb_hostname_override"
        return
    fi
    if [ -n "${HOSTNAME:-}" ]; then
        printf '%s\n' "$HOSTNAME"
        return
    fi
    hostname 2>/dev/null | awk 'NF { print; exit }'
}

knowbase_detect_lan_ipv4s() {
    kb_ipv4_override=${1:-}
    kb_raw_addresses=""
    if [ -n "$kb_ipv4_override" ]; then
        kb_raw_addresses=$(printf '%s' "$kb_ipv4_override" | tr ',;' '  ')
    elif command -v ip >/dev/null 2>&1; then
        kb_interfaces=$(ip -4 route show default 2>/dev/null | awk '
            { for (i = 1; i <= NF; i++) if ($i == "dev" && $(i + 1) != "") print $(i + 1) }
        ' | awk '!seen[$0]++')
        for kb_interface in $kb_interfaces; do
            kb_raw_addresses="$kb_raw_addresses $(ip -o -4 addr show dev "$kb_interface" scope global 2>/dev/null | awk '{ split($4, value, "/"); print value[1] }')"
        done
    fi
    if [ -z "$(printf '%s' "$kb_raw_addresses" | tr -d '[:space:]')" ] && command -v hostname >/dev/null 2>&1; then
        kb_raw_addresses=$(hostname -I 2>/dev/null || true)
    fi

    kb_seen_addresses=" "
    for kb_candidate in $kb_raw_addresses; do
        knowbase_is_usable_ipv4 "$kb_candidate" || continue
        case "$kb_seen_addresses" in
            *" $kb_candidate "*) ;;
            *)
                printf '%s\n' "$kb_candidate"
                kb_seen_addresses="$kb_seen_addresses$kb_candidate "
                ;;
        esac
    done
}

knowbase_normalize_internal_host_value() {
    printf '%s\n' "${1:-}" | tr ',;' '  ' | awk '
        {
            for (i = 1; i <= NF; i++) {
                key = tolower($i)
                if (!seen[key]++) {
                    if (result != "") result = result ","
                    result = result $i
                }
            }
        }
        END { print result }
    '
}

knowbase_detect_internal_host_value() {
    kb_detected_hostname=$(knowbase_detect_hostname "${1:-}")
    kb_detected_ipv4s=$(knowbase_detect_lan_ipv4s "${2:-}")
    knowbase_normalize_internal_host_value "$kb_detected_hostname $kb_detected_ipv4s"
}

knowbase_suggest_internal_host_value() {
    kb_current_value=$(knowbase_normalize_internal_host_value "${1:-}")
    kb_detected_value=$(knowbase_normalize_internal_host_value "${2:-}")
    kb_retained_names=""
    kb_old_ifs=$IFS
    IFS=,
    for kb_current_token in $kb_current_value; do
        case "$kb_current_token" in
            ""|localhost|127.0.0.1) continue ;;
        esac
        if ! knowbase_is_usable_ipv4 "$kb_current_token"; then
            kb_retained_names="$kb_retained_names,$kb_current_token"
        fi
    done
    IFS=$kb_old_ifs
    kb_suggested=$(knowbase_normalize_internal_host_value "$kb_retained_names,$kb_detected_value")
    if [ -n "$kb_suggested" ]; then
        printf '%s\n' "$kb_suggested"
    elif [ -n "$kb_current_value" ]; then
        printf '%s\n' "$kb_current_value"
    else
        printf '%s\n' localhost
    fi
}
