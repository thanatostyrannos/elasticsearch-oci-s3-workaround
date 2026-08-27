#!/bin/bash
# $WORK is a scratch directory. Set it, or the default below is used.
WORK="${WORK:-/tmp/gc-evidence}"
# BASE-MS: one index, one snapshot, built to be mounted as a searchable snapshot.
set -e
D=$WORK/blast-remeasure
ES=$D/harness/es.sh
$ES DELETE "/_snapshot/blast-base-ms" >/dev/null 2>&1 || true
$ES DELETE "/blast-mount" >/dev/null 2>&1 || true
python3 $D/harness/s3lib.py purge blastrm base-ms >/dev/null
$ES PUT "/_snapshot/blast-base-ms?verify=false" \
  '{"type":"s3","settings":{"bucket":"blastrm","client":"default","base_path":"base-ms"}}'; echo
$ES PUT "/blast-mount" '{"settings":{"number_of_shards":1,"number_of_replicas":0,"index.refresh_interval":"-1"}}'; echo
$D/harness/esbulk.sh blast-mount 0 600
$ES POST "/blast-mount/_forcemerge?max_num_segments=1" >/dev/null
$ES POST "/blast-mount/_flush" >/dev/null
$ES PUT "/_snapshot/blast-base-ms/blast-ms-1?wait_for_completion=true" \
  '{"indices":"blast-mount","include_global_state":false}' | head -c 160; echo
