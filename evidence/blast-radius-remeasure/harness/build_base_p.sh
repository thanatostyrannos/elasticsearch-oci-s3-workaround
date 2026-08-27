#!/bin/bash
# $WORK is a scratch directory. Set it, or the default below is used.
WORK="${WORK:-/tmp/gc-evidence}"
# BASE-P: the shape docs/blast-radius.md measured its 906-byte pointer case on.
# One index of 2,000 documents, one snapshot, nine objects.
set -e
D=$WORK/blast-remeasure
ES=$D/harness/es.sh
$ES DELETE "/_snapshot/blast-base-p" >/dev/null 2>&1 || true
$ES DELETE "/blast-pointer" >/dev/null 2>&1 || true
python3 $D/harness/s3lib.py purge blastrm base-p >/dev/null
$ES PUT "/_snapshot/blast-base-p?verify=false" \
  '{"type":"s3","settings":{"bucket":"blastrm","client":"default","base_path":"base-p"}}'; echo
$ES PUT "/blast-pointer" '{"settings":{"number_of_shards":1,"number_of_replicas":0,"index.refresh_interval":"-1"}}'; echo
$D/harness/esbulk.sh blast-pointer 0 2000
$ES POST "/blast-pointer/_forcemerge?max_num_segments=1" >/dev/null
$ES POST "/blast-pointer/_flush" >/dev/null
$ES PUT "/_snapshot/blast-base-p/blast-p-1?wait_for_completion=true" \
  '{"indices":"blast-pointer","include_global_state":false}' | head -c 160; echo
