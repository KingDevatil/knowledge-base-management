#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ACTION=${1:-install}
[ "$#" -eq 0 ] || shift
BIN_DIR=${KNOWBASE_CLI_BIN_DIR:-"$HOME/.local/bin"}
CONFIG_DIR=${XDG_CONFIG_HOME:-"$HOME/.config"}/knowbase
QUIET=false
START_MARKER="# >>> knowbase CLI >>>"
END_MARKER="# <<< knowbase CLI <<<"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --bin-dir)
            [ "$#" -ge 2 ] || { echo "[错误] --bin-dir 缺少目录。" >&2; exit 2; }
            BIN_DIR=$2
            shift 2
            ;;
        --bin-dir=*) BIN_DIR=${1#*=}; shift ;;
        --quiet) QUIET=true; shift ;;
        *) echo "[错误] 未知 CLI 安装参数：$1" >&2; exit 2 ;;
    esac
done

case "$ACTION" in install|uninstall|status) ;; *) echo "用法: install-cli.sh install|uninstall|status" >&2; exit 2 ;; esac

message() {
    [ "$QUIET" = true ] || printf '%s\n' "$1"
}

shell_profiles() {
    printf '%s\n' "$HOME/.profile"
    case "${SHELL:-}" in
        */bash) printf '%s\n' "$HOME/.bashrc" ;;
        */zsh) printf '%s\n' "$HOME/.zshrc" ;;
        */fish) : ;;
    esac
}

remove_path_block() {
    profile_file=$1
    [ -f "$profile_file" ] || return 0
    profile_tmp="${profile_file}.knowbase.$$"
    awk -v start="$START_MARKER" -v end="$END_MARKER" '
        $0 == start { skipping = 1; next }
        $0 == end { skipping = 0; next }
        !skipping { print }
    ' "$profile_file" > "$profile_tmp"
    mv "$profile_tmp" "$profile_file"
}

add_path_block() {
    profile_file=$1
    [ -f "$profile_file" ] || : > "$profile_file"
    remove_path_block "$profile_file"
    {
        printf '\n%s\n' "$START_MARKER"
        printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac\n' "$BIN_DIR" "$BIN_DIR"
        printf '%s\n' "$END_MARKER"
    } >> "$profile_file"
}

is_installed() {
    [ -x "$BIN_DIR/knowbase" ] || return 1
    [ -f "$CONFIG_DIR/home" ] || return 1
    registered_root=$(sed -n '1p' "$CONFIG_DIR/home")
    [ "$registered_root" = "$ROOT_DIR" ] || return 1
    grep -q 'KNOWBASE_CLI_WRAPPER=1' "$BIN_DIR/knowbase"
}

case "$ACTION" in
    install)
        mkdir -p "$BIN_DIR" "$CONFIG_DIR"
        cp "$ROOT_DIR/scripts/knowbase" "$BIN_DIR/knowbase"
        chmod 755 "$BIN_DIR/knowbase"
        printf '%s\n' "$ROOT_DIR" > "$CONFIG_DIR/home"
        shell_profiles | while IFS= read -r profile_file; do
            add_path_block "$profile_file"
        done
        message "[完成] knowbase 已安装到 $BIN_DIR"
        message "[提示] 请重新打开终端，然后运行：knowbase health"
        ;;
    uninstall)
        if [ -f "$BIN_DIR/knowbase" ] && grep -q 'KNOWBASE_CLI_WRAPPER=1' "$BIN_DIR/knowbase"; then
            rm -f "$BIN_DIR/knowbase"
        fi
        rm -f "$CONFIG_DIR/home"
        rmdir "$CONFIG_DIR" 2>/dev/null || true
        shell_profiles | while IFS= read -r profile_file; do
            remove_path_block "$profile_file"
        done
        rmdir "$BIN_DIR" 2>/dev/null || true
        message "[完成] knowbase 全局命令已卸载。"
        ;;
    status)
        if is_installed; then
            message "[已安装] knowbase -> $ROOT_DIR"
        else
            message "[未安装] 运行 sh ./start.sh cli-install 注册全局命令。"
            exit 1
        fi
        ;;
esac
