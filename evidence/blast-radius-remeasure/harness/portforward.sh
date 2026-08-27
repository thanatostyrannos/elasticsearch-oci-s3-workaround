#!/usr/bin/env bash
# Port 19045 is this campaign's own. Other agents on this rig use 19000 and 19043.
set -euo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$D/logs"
if curl -s -o /dev/null -m 2 "http://127.0.0.1:19045/" ; then exit 0; fi
( setsid kubectl --context rancher-desktop -n es-rig port-forward svc/minio 19045:9000 \
    > "$D/logs/pf-minio.log" 2>&1 < /dev/null & )
sleep 6
