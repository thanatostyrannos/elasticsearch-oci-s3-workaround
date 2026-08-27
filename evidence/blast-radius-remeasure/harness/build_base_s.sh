#!/bin/bash
# $WORK is a scratch directory. Set it, or the default below is used.
WORK="${WORK:-/tmp/gc-evidence}"
# Builds BASE-S: the shape docs/blast-radius.md measured its 14-anomaly headline
# on. Two indices over three snapshots, the second index written once and never
# touched, so its blobs are uploaded in snapshot 1 and referenced by all three.
set -e
D=$WORK/blast-remeasure
ES=$D/harness/es.sh
BULK=$D/harness/esbulk.sh

$ES DELETE "/_snapshot/blast-base-s" >/dev/null 2>&1 || true
$ES DELETE "/blast-share1,blast-share2" >/dev/null 2>&1 || true
python3 $D/harness/s3lib.py purge blastrm base-s >/dev/null

$ES PUT "/_snapshot/blast-base-s?verify=false" \
  '{"type":"s3","settings":{"bucket":"blastrm","client":"default","base_path":"base-s"}}'; echo

for IDX in blast-share1 blast-share2; do
  $ES PUT "/$IDX" '{"settings":{"number_of_shards":1,"number_of_replicas":0,"index.refresh_interval":"-1"}}'; echo
done
$BULK blast-share1 0 2000
$BULK blast-share2 0 2000
$ES POST "/blast-share1,blast-share2/_forcemerge?max_num_segments=1" >/dev/null; echo
$ES POST "/blast-share1,blast-share2/_flush" >/dev/null

$ES PUT "/_snapshot/blast-base-s/blast-snap-1?wait_for_completion=true" \
  '{"indices":"blast-share1,blast-share2","include_global_state":false}' | head -c 200; echo

# Snapshot 2 changes nothing at all. Every blob it references was uploaded by
# snapshot 1, which is the sharing this whole document is about.
$ES PUT "/_snapshot/blast-base-s/blast-snap-2?wait_for_completion=true" \
  '{"indices":"blast-share1,blast-share2","include_global_state":false}' | head -c 200; echo

# Snapshot 3 adds 50 documents to one index, so it uploads new blobs for that
# index and keeps referencing the old ones.
$BULK blast-share1 2000 50
$ES POST "/blast-share1/_flush" >/dev/null
$ES PUT "/_snapshot/blast-base-s/blast-snap-3?wait_for_completion=true" \
  '{"indices":"blast-share1,blast-share2","include_global_state":false}' | head -c 200; echo
