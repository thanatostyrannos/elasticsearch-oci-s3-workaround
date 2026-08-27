#!/usr/bin/env bash
# Run the audit-and-reclaim loop against a repository, repeatedly.
#
# Wraps reclaim_test_protocol.py with the preflight checks that turn a
# confusing failure on cycle 40 into a clear one before cycle 1. Everything it
# does can be done by hand; this exists so it does not have to be.
#
# Configuration comes from a file, not from arguments, because two of the
# values are secrets and an argument is visible in `ps` to every user on the
# host. Copy test-cycle.conf.example, fill it in, chmod 600 it.
#
#     ./scripts/run-test-cycle.sh my.conf
#
set -euo pipefail

die() { printf '\n%s\n' "$*" >&2; exit 1; }
say() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

CONF="${1:-}"
[ -n "$CONF" ] || die "usage: $0 <config-file>
Copy scripts/test-cycle.conf.example and fill it in first."
[ -f "$CONF" ] || die "no such config file: $CONF"

# A config file holds credentials paths and, in the ES case, is read by tools
# that refuse a world-readable credential. Refuse early rather than let one of
# them refuse later with less context.
mode=$(stat -c '%a' "$CONF" 2>/dev/null || stat -f '%Lp' "$CONF")
case "$mode" in
  600|400) ;;
  *) die "$CONF is mode $mode and must be 600 or 400. Run: chmod 600 $CONF" ;;
esac

# shellcheck disable=SC1090
. "$CONF"

for required in ENDPOINT REGION BUCKET PREFIX CREDENTIALS REPOSITORY OUT; do
  eval "value=\${$required:-}"
  [ -n "$value" ] || die "$CONF does not set $required"
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CYCLES="${CYCLES:-100}"
MODE="${MODE:-mixed}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-5}"
SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-300}"
DRY_RUN_ONLY="${DRY_RUN_ONLY:-yes}"

say "preflight"

command -v python3 >/dev/null || die "python3 is not on PATH"
python3 - <<'PY' || die "python3 is older than 3.9"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

[ -f "$CREDENTIALS" ] || die "credentials file not found: $CREDENTIALS"
cmode=$(stat -c '%a' "$CREDENTIALS" 2>/dev/null || stat -f '%Lp' "$CREDENTIALS")
case "$cmode" in
  600|400) ;;
  *) die "$CREDENTIALS is mode $cmode and must be 600 or 400.
Any other mode lets other users on this host read a credential." ;;
esac

python3 - "$CREDENTIALS" <<'PY' || die "the credentials file is not usable"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"  {sys.argv[1]} is not readable JSON: {exc}", file=sys.stderr)
    raise SystemExit(1)
if "s3" not in d:
    print("  it has no 's3' section", file=sys.stderr)
    raise SystemExit(1)
print("  credentials file has sections:", ", ".join(sorted(d)))
PY

# The audit refuses corroboration it cannot complete, so find out now rather
# than on cycle 1 after a long derive.
if [ -n "${ELASTICSEARCH:-}" ]; then
  [ -n "${ES_PASSWORD_FILE:-}" ] || die "ELASTICSEARCH is set but ES_PASSWORD_FILE is not.
The harness needs it for its own calls to the cluster."
  [ -f "$ES_PASSWORD_FILE" ] || die "no such file: $ES_PASSWORD_FILE"
  python3 - "$CREDENTIALS" <<'PY' || die "add an 'elasticsearch' section to the credentials file.
The audit reads its cluster credential from there. ES_PASSWORD_FILE
authenticates this harness only and never reaches the audit."
import json, sys
d = json.load(open(sys.argv[1]))
section = d.get("elasticsearch") or {}
ok = "api_key" in section or ("username" in section and "password" in section)
raise SystemExit(0 if ok else 1)
PY
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
         -u "elastic:$(cat "$ES_PASSWORD_FILE")" "$ELASTICSEARCH/_cluster/health" || true)
  [ "$code" = "200" ] || die "Elasticsearch at $ELASTICSEARCH answered HTTP ${code:-nothing}.
If it answered 401 the password is stale: it is regenerated whenever the
cluster is rebuilt. If nothing, check the endpoint is reachable."
  say "  elasticsearch reachable, corroboration will be re-checked at execute time"
else
  say "  no ELASTICSEARCH set: running without corroboration"
fi

case "$ENDPOINT" in
  https://*) ;;
  http://127.0.0.1*|http://localhost*|http://\[::1\]*) ;;
  http://*) die "ENDPOINT is plain http to a non-loopback host: $ENDPOINT
A manifest names which production objects are about to be deleted. Use https,
or add --insecure-http yourself if this really is a lab store." ;;
  *) die "ENDPOINT does not look like a URL: $ENDPOINT" ;;
esac

mkdir -p "$OUT"
say "  writing to $OUT"

args=(
  --cycles "$CYCLES" --mode "$MODE"
  --sleep "$SLEEP_BETWEEN" --settle-timeout "$SETTLE_TIMEOUT"
  --transport s3 --endpoint "$ENDPOINT" --region "$REGION"
  --bucket "$BUCKET" --prefix "$PREFIX"
  --credentials "$CREDENTIALS"
  --repository "$REPOSITORY" --out "$OUT"
)
[ -n "${DATA_STREAM:-}" ] && args+=(--data-stream "$DATA_STREAM")
[ -n "${ELASTICSEARCH:-}" ] && args+=(--elasticsearch "$ELASTICSEARCH" --es-password-file "$ES_PASSWORD_FILE")
[ "$DRY_RUN_ONLY" = "yes" ] && args+=(--dry-run-only)

if [ "$DRY_RUN_ONLY" = "yes" ]; then
  say "DRY RUN ONLY. Nothing will be deleted. Set DRY_RUN_ONLY=no to delete."
else
  say "DELETES ARE ENABLED. This will remove objects from $BUCKET/$PREFIX"
  say "Ctrl-C within 10 seconds to stop."
  sleep 10
fi

say "starting $CYCLES cycle(s)"
python3 reclaim_test_protocol.py "${args[@]}"
status=$?

say "done, exit $status"
say "totals, read from the per-cycle execute files rather than the summary:"
if ls "$OUT"/exec-*.txt >/dev/null 2>&1; then
  awk '/^deleted:/{d+=$2} /^failed:/{f+=$2} /^unconfirmed:/{u+=$2}
       END {printf "    deleted=%d failed=%d unconfirmed=%d\n", d, f, u}' \
      "$OUT"/exec-*.txt
else
  echo "    no execute files: nothing was deleted"
fi
echo "    cycles recorded: $(( $(wc -l < "$OUT/cycles.tsv") - 1 ))"
exit "$status"
