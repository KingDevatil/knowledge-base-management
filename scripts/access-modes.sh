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
