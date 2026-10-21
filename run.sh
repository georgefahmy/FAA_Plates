#!/usr/bin/env bash
set -euo pipefail

# Ensure script runs from the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

STREAMLIT_BIN="$HOME/.virtualenvs/plates/bin/streamlit"

if [[ ! -x "$STREAMLIT_BIN" ]]; then
    if command -v streamlit >/dev/null 2>&1; then
        STREAMLIT_BIN="$(command -v streamlit)"
    else
        echo "[!] Error: Streamlit executable not found at '$STREAMLIT_BIN'." >&2
        echo "    Please verify that your 'plates' virtualenv exists in ~/.virtualenvs/plates." >&2
        exit 1
    fi
fi

exec "$STREAMLIT_BIN" run "$SCRIPT_DIR/app.py" "$@"
