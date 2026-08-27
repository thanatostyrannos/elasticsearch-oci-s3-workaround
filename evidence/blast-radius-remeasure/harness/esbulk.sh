#!/bin/bash
# $WORK is a scratch directory. Set it, or the default below is used.
WORK="${WORK:-/tmp/gc-evidence}"
# esbulk.sh INDEX START COUNT
D=$WORK/blast-remeasure
PW=$(cat $D/env/es_pass)
python3 $D/harness/bulk.py "$1" "$2" "$3" | kubectl --context rancher-desktop -n es-rig exec -i rig-es-default-0 -c elasticsearch -- \
  curl -s -u "elastic:$PW" -H 'Content-Type: application/x-ndjson' --data-binary @- "http://127.0.0.1:9200/_bulk?refresh=true" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('bulk errors:', d.get('errors'), 'items:', len(d.get('items',[])))"
