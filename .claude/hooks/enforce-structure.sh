#!/usr/bin/env bash
# enforce-structure.sh — PreToolUse hook wrapper.
# Passes the hook payload (stdin) straight through to the Python guard.
# Fails open (allow) if python3 is unavailable.

if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/enforce-structure.py" "${CLAUDE_PROJECT_DIR:-$(pwd)}"
