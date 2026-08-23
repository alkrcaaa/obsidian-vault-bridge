#!/usr/bin/env bash
# Install vault-search MCP server as a Python venv.
# Usage: ./install.sh [--vault-dir /path/to/vault]
#
# After install, configure in your MCP settings:
#   - Claude: add to ~/.claude/claude.json stdio block
#   - Qwen: add to ~/.qwen/settings.json stdio block
#
# Or set VAULT_DIR env var:
#   export VAULT_DIR="/home/user/Notes/AliNotes"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VAULT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault-dir)
      VAULT_DIR="$2"
      shift 2
      ;;
    --help|-h)
      head -10 "$0" | tail -8
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# Create venv
if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

# Activate & install
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q

echo ""
echo "vault-search MCP server installed at $VENV_DIR"
echo ""
echo "To use:"
echo "  1. Set VAULT_DIR env var:"
echo "     export VAULT_DIR=\"/path/to/your/obsidian-vault\""
echo "  2. Or pass vault_dir parameter to each tool call."
echo ""
echo "  Claude (stdio):"
echo "    \"vault-search\": {"
echo "      \"command\": \"${VENV_DIR}/bin/python3\","
echo "      \"args\": [\"${SCRIPT_DIR}/server.py\"],"
echo "      \"env\": {\"VAULT_DIR\": \"${VAULT_DIR:-YOUR_VAULT_PATH}\"}"
echo "    }"
echo ""
echo "  Qwen (stdio):"
echo "    Same config in ~/.qwen/settings.json mcp block."
echo ""
