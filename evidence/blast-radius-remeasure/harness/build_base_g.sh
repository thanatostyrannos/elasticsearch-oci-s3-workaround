#!/bin/bash
# $WORK is a scratch directory. Set it, or the default below is used.
WORK="${WORK:-/tmp/gc-evidence}"
# BASE-G: one snapshot taken WITH global cluster state, so the root
# meta-<uuid>.dat blob is a blob a restore actually reads.
set -e
D=$WORK/blast-remeasure
ES=$D/harness/es.sh
$ES DELETE "/_snapshot/blast-base-g" >/dev/null 2>&1 || true
$ES DELETE "/blast-gstate" >/dev/null 2>&1 || true
python3 $D/harness/s3lib.py purge blastrm base-g >/dev/null
$ES PUT "/_snapshot/blast-base-g?verify=false" \
  '{"type":"s3","settings":{"bucket":"blastrm","client":"default","base_path":"base-g"}}'; echo
$ES PUT "/blast-gstate" '{"settings":{"number_of_shards":1,"number_of_replicas":0,"index.refresh_interval":"-1"}}'; echo
$D/harness/esbulk.sh blast-gstate 0 500
$ES POST "/blast-gstate/_forcemerge?max_num_segments=1" >/dev/null
$ES POST "/blast-gstate/_flush" >/dev/null
$ES PUT "/_snapshot/blast-base-g/blast-g-1?wait_for_completion=true" \
  '{"indices":"blast-gstate","include_global_state":true}' | head -c 160; echo
