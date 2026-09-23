#!/usr/bin/env bash
set -euo pipefail

URL="${HEALTHCHECK_URL:-http://127.0.0.1:8080/health}"
TIMEOUT="${HEALTHCHECK_TIMEOUT_SECONDS:-5}"

if command -v curl >/dev/null 2>&1; then
  curl -fsS --max-time "$TIMEOUT" "$URL" >/dev/null
else
  python - "$URL" "$TIMEOUT" <<'PY'
import sys, urllib.request
url, timeout = sys.argv[1], float(sys.argv[2])
with urllib.request.urlopen(url, timeout=timeout) as resp:
    if resp.status >= 400:
        raise SystemExit(1)
PY
fi

exit 0
