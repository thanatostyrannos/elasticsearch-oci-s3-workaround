#!/usr/bin/env bash
# Credentials land in files, never on a command line and never in the shell
# history, because this rig shares a namespace with other work.
set -euo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K="kubectl --context rancher-desktop -n es-rig"
mkdir -p "$D/env"
$K get secret rig-es-elastic-user -o go-template='{{.data.elastic | base64decode}}' > "$D/env/es_pass"
$K get secret s3-credentials -o jsonpath='{.data.s3\.client\.default\.access_key}' | base64 -d > "$D/env/s3_access"
$K get secret s3-credentials -o jsonpath='{.data.s3\.client\.default\.secret_key}' | base64 -d > "$D/env/s3_secret"
chmod 600 "$D/env"/*
