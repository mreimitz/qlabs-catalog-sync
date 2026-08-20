#!/usr/bin/env bash
# Generate console/src/api/generated/schema.ts -- a pure TypeScript *type* declaration
# file -- from the committed console/src/api/generated/openapi.json (WP12/T12.8).
#
# Usage:
#   scripts/gen_api_client.sh            # (re)write console/src/api/generated/schema.ts
#   scripts/gen_api_client.sh --check    # exit 1 if the committed file is stale
#   scripts/gen_api_client.sh --help     # this message
#
# --check is the CI gate this task's verify command and scripts/gen_openapi.py --check
# both exist to feed: it regenerates the client to a throwaway temp location, diffs it
# against what is committed, and touches nothing under console/ either way. Run
# scripts/gen_openapi.py first (with or without --check, matching this script's own
# mode) -- this script trusts the committed openapi.json as its input and never talks to
# a running server or a Python process itself.
#
# Why openapi-typescript rather than a generator that emits a runtime client: T13.2 owns
# console/src/api/client.ts and wraps this file's typed `paths`/`components` with cookie
# credentials and the X-CSRF-Token header (C7) itself. A generator that baked in its own
# fetch/transport would fight that wrapper instead of feeding it -- see this repo's
# CLAUDE.md-adjacent task notes for the exact seam. So this script's output is a plain
# `.ts` *type* file: no runtime code, no bundled HTTP client, nothing to import except
# `import type { paths, components } from "./schema"`.
#
# The generator version is pinned exactly (OPENAPI_TYPESCRIPT_VERSION below) so that
# `npx -y openapi-typescript@<version>` resolves to the identical package on every
# machine and in CI -- an unpinned `npx openapi-typescript` would happily pick up a newer
# major version on a different day and silently reshape the committed file.

set -euo pipefail

# ---------------------------------------------------------------------------------------
# Pinned generator version -- bump deliberately, then regenerate and commit the diff.
# ---------------------------------------------------------------------------------------
OPENAPI_TYPESCRIPT_VERSION="7.13.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
GENERATED_DIR="${REPO_ROOT}/console/src/api/generated"
OPENAPI_JSON="${GENERATED_DIR}/openapi.json"
SCHEMA_TS="${GENERATED_DIR}/schema.ts"

CHECK_MODE=0

usage() {
  cat <<'EOF'
Usage: scripts/gen_api_client.sh [--check] [--help]

Generate console/src/api/generated/schema.ts, a pure TypeScript type declaration file
(no runtime code, no bundled fetch/transport) from the committed
console/src/api/generated/openapi.json, using a pinned openapi-typescript version.

  (no flags)   Regenerate schema.ts in place from the committed openapi.json.
  --check      Regenerate to a temp location and diff against the committed schema.ts.
               Exits 1 and prints a diff if they differ; writes nothing under console/.
               Exits 0 if they match exactly.
  --help       Show this message.

Run scripts/gen_openapi.py (in the matching mode) first -- this script trusts
console/src/api/generated/openapi.json as-is and never builds it itself.
EOF
}

for arg in "$@"; do
  case "${arg}" in
    --check)
      CHECK_MODE=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[gen_api_client] unknown argument: ${arg}" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v npx >/dev/null 2>&1; then
  echo "[gen_api_client] npx not found on PATH -- Node.js is required to generate the" >&2
  echo "[gen_api_client] TypeScript client. Install Node (see the console's own" >&2
  echo "[gen_api_client] toolchain once T13.1 lands) and try again." >&2
  exit 1
fi

if [[ ! -f "${OPENAPI_JSON}" ]]; then
  echo "[gen_api_client] ${OPENAPI_JSON} does not exist -- run" >&2
  echo "[gen_api_client]   uv run python scripts/gen_openapi.py" >&2
  echo "[gen_api_client] first." >&2
  exit 1
fi

generate() {
  # $1: output path
  npx -y "openapi-typescript@${OPENAPI_TYPESCRIPT_VERSION}" "${OPENAPI_JSON}" -o "$1"
}

if [[ "${CHECK_MODE}" -eq 0 ]]; then
  mkdir -p "${GENERATED_DIR}"
  generate "${SCHEMA_TS}"
  echo "[gen_api_client] wrote ${SCHEMA_TS}"
  exit 0
fi

# --check: generate into a scratch temp directory (never under console/), diff against
# the committed file, and always clean up -- even on an early exit.
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gen-api-client.XXXXXX")"
trap 'rm -rf "${TMP_DIR}"' EXIT

TMP_SCHEMA_TS="${TMP_DIR}/schema.ts"
generate "${TMP_SCHEMA_TS}" >/dev/null

if [[ ! -f "${SCHEMA_TS}" ]]; then
  echo "[gen_api_client] ${SCHEMA_TS} does not exist -- run without --check." >&2
  exit 1
fi

if diff -q "${SCHEMA_TS}" "${TMP_SCHEMA_TS}" >/dev/null; then
  echo "[gen_api_client] ${SCHEMA_TS} matches the committed openapi.json."
  exit 0
fi

echo "[gen_api_client] ${SCHEMA_TS} is STALE -- it does not match what" >&2
echo "[gen_api_client] openapi-typescript@${OPENAPI_TYPESCRIPT_VERSION} generates from the" >&2
echo "[gen_api_client] committed openapi.json right now. Run" >&2
echo "[gen_api_client]   scripts/gen_api_client.sh" >&2
echo "[gen_api_client] (no --check) to regenerate it, then commit the result." >&2
echo "" >&2
diff -u "${SCHEMA_TS}" "${TMP_SCHEMA_TS}" >&2 || true
exit 1
