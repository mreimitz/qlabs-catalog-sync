#!/usr/bin/env bash
# Tolerant post-edit formatter for the code monorepo.
# Runs ruff format + a non-blocking autofix if ruff is available.
# ALWAYS exits 0 — it must never block an edit.
set -u

# No ruff on PATH? Do nothing, succeed.
command -v ruff >/dev/null 2>&1 || exit 0

# Operate from the repo root when the hook exposes it.
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

# Never touch the separately-governed OKF planning bundle.
ruff format packages 2>/dev/null || true
ruff check --fix --exit-zero packages 2>/dev/null || true

exit 0
