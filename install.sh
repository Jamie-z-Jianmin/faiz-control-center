#!/usr/bin/env bash
# Installer for Faiz Control Center (`faiz-dash`) & Antigravity CLI StatusLine.
set -euo pipefail

REPO_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="link"

usage() {
    cat <<'EOF'
Usage: ./install.sh [--link | --copy] [--help]

Options:
  --link   Symlink repo scripts/skills into ~/.agents and ~/.local/bin (recommended for development)
  --copy   Copy scripts/skills into ~/.agents and ~/.local/bin (standalone install)
  -h       Show this help message
EOF
}

case "${1:-}" in
    --link|"")
        MODE="link"
        ;;
    --copy)
        MODE="copy"
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        echo "Unknown option: $1" >&2
        usage >&2
        exit 1
        ;;
esac

_install_file() {
    local src="$1" dst="$2" exec_flag="${3:-1}"
    mkdir -p "$(dirname "$dst")"
    rm -rf "$dst"
    if [ "$MODE" = "link" ]; then
        ln -s "$src" "$dst"
    else
        cp "$src" "$dst"
    fi
    if [ "$exec_flag" = "1" ]; then
        chmod +x "$src"
        [ "$MODE" = "copy" ] && chmod +x "$dst" || true
    fi
    echo "Installed ($MODE): $dst -> $src"
}

_install_file "$REPO_DIR/scripts/statusline.sh" "$HOME/.agents/scripts/statusline.sh" 1
_install_file "$REPO_DIR/bin/faiz-dash" "$HOME/.local/bin/faiz-dash" 1
_install_file "$REPO_DIR/bin/_faiz_dash_hook.sh" "$HOME/.local/bin/_faiz_dash_hook.sh" 1
_install_file "$REPO_DIR/skills/faiz-dash/SKILL.md" "$HOME/.agents/skills/faiz-dash/SKILL.md" 0

cat <<'EOF'

Next steps:
1. Ensure Python >=3.12 with `textual` and `rich` is installed (`pip install -e .`).
2. Ensure `~/.local/bin` is on your PATH (`export PATH="$HOME/.local/bin:$PATH"`).
3. Configure `~/.gemini/antigravity-cli/settings.json` statusLine:
   {
     "statusLine": {
       "type": "command",
       "command": "~/.agents/scripts/statusline.sh"
     }
   }
4. Quick test:
   faiz-dash config
   faiz-dash inspect
EOF
