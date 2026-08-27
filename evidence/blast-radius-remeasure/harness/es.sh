#!/bin/bash
# $WORK is a scratch directory. Set it, or the default below is used.
WORK="${WORK:-/tmp/gc-evidence}"
# es.sh METHOD PATH [json-body-or-@file]
D=$WORK/blast-remeasure
PW=$(cat $D/env/es_pass)
M=$1; P=$2; B=$3
K="kubectl --context rancher-desktop -n es-rig"
if [ -n "$B" ]; then
  $K exec -i rig-es-default-0 -c elasticsearch -- \
    curl -s -X "$M" -u "elastic:$PW" -H 'Content-Type: application/json' -d "$B" "http://127.0.0.1:9200$P"
else
  $K exec rig-es-default-0 -c elasticsearch -- \
    curl -s -X "$M" -u "elastic:$PW" "http://127.0.0.1:9200$P"
fi
