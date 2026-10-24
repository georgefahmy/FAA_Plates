#!/bin/bash
# Launch the native macOS desktop applet (no web browser needed)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

PYTHON_BIN="$HOME/.virtualenvs/plates/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(which python3)"
fi

exec "$PYTHON_BIN" "$DIR/desktop_app.py" "$@"
