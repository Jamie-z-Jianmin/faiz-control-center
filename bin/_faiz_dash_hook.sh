#!/usr/bin/env bash
# Auto-launch hook for Faiz Control Center (faiz-dash) inside Rex terminal windows.
# Guarded against recursion, non-TTY, headless (-p), and control subcommands.

_faiz_dash_spawn_if_needed() {
    local rex_blk="$1"
    local cache_dir="$HOME/.agents/cache"
    local lock_dir="$cache_dir/dash_spawn.lock.d"

    command -v rex >/dev/null 2>&1 || return 0
    local jq_bin=""
    if [ -x /usr/bin/jq ]; then
        jq_bin=/usr/bin/jq
    elif command -v jq >/dev/null 2>&1; then
        jq_bin="$(command -v jq)"
    else
        return 0
    fi

    mkdir -p "$cache_dir" 2>/dev/null || return 0
    if ! mkdir "$lock_dir" 2>/dev/null; then
        # Clean stale lock older than 15s
        if [ -d "$lock_dir" ]; then
            local now mtime
            now=$(date +%s 2>/dev/null || echo 0)
            mtime=$(stat -c %Y "$lock_dir" 2>/dev/null || stat -f %m "$lock_dir" 2>/dev/null || echo "$now")
            if [ $((now - mtime)) -gt 15 ]; then
                rmdir "$lock_dir" 2>/dev/null || true
                mkdir "$lock_dir" 2>/dev/null || return 0
            else
                return 0
            fi
        else
            return 0
        fi
    fi

    local cur_win has_dash
    cur_win=$(rex whoami --json 2>/dev/null | "$jq_bin" -r '.window_id // empty' 2>/dev/null)
    if [ -n "$cur_win" ]; then
        has_dash=$(rex block list --json 2>/dev/null | "$jq_bin" -r --arg w "$cur_win" '[.blocks[]? | select(.window_id == $w and .label == "faiz-dash")] | length' 2>/dev/null)
        if [ "${has_dash:-0}" -eq 0 ]; then
            local dash_bin="${FAIZ_DASH_BIN:-$HOME/.local/bin/faiz-dash}"
            rex split -b "$rex_blk" --split=below --ratio 22 --focus=false --label "faiz-dash" --shell=none -- "$dash_bin" --dock >/dev/null 2>&1
        fi
    fi

    rmdir "$lock_dir" 2>/dev/null || true
}

_maybe_launch_faiz_dash() {
    [ "${FAIZ_NO_DASH:-0}" = "1" ] && return 0
    [ -t 0 ] && [ -t 1 ] || return 0

    local rex_blk="${REX_BLOCK_ID:-${REX_BLOCK:-}}"
    [ -z "$rex_blk" ] && return 0
    [ -f "$HOME/.agents/cache/dash_disabled" ] && return 0

    local arg
    for arg in "$@"; do
        case "$arg" in
            --)
                break
                ;;
            -p|--print|-p=*|--print=*|--prompt|--prompt=*)
                return 0
                ;;
            acp|agent|agents|models|help|--help|-h|--version|-v|plugin|plugins|update|install|changelog|remote-control|mic-serve|mcp)
                return 0
                ;;
        esac
    done

    command -v rex >/dev/null 2>&1 || return 0
    ( _faiz_dash_spawn_if_needed "$rex_blk" ) >/dev/null 2>&1 &
}
