#!/usr/bin/env bash
# Reproduce every number in the report.
#
# Point it at a different build of the tool with GENCHAIN_TOOL_ROOT, which is
# the directory holding the `generation_chain` package. Sizes and failure
# rates are flags on each bench, so a later version can be pushed harder or
# less hard without editing anything here.
#
#   GENCHAIN_TOOL_ROOT=/path/to/worktree ./run_all.sh
#
# The MinIO parts need a port-forward and the rig credentials:
#   kubectl -n es-rig port-forward svc/minio 19000:9000 &
#   export MINIO_ENDPOINT=http://127.0.0.1:19000
#   export MINIO_ACCESS=$(kubectl -n es-rig get secret s3-credentials \
#       -o jsonpath='{.data.s3\.client\.default\.access_key}' | base64 -d)
#   export MINIO_SECRET=$(kubectl -n es-rig get secret s3-credentials \
#       -o jsonpath='{.data.s3\.client\.default\.secret_key}' | base64 -d)
set -u
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
run() { echo; echo "### $*"; python3 "$@"; }

run validate_generator.py
run realcheck.py
run bench_depth.py --generations 10,100,1000,5000,10000
run bench_depth.py --generations 100,200 --latency 0.04 --out depth-latency-40ms
run bench_depth.py --generations 894 --latency 0.04 --out depth-894-at-40ms
run bench_breadth.py --generations 20 --shapes 1x1,2x5,5x10,10x20,20x50,50x50
run bench_memory.py --shapes 10x20x20,20x50x20,50x50x20,50x50x45,100x50x45
run bench_critical.py
GENCHAIN_SNAPSHOT_DOCUMENTS=1 run bench_critical.py --out critical-snapshot-documents
run bench_refusal.py --trials 400
run bench_failures.py --generations 100 --indices 3 --shards 3 --trials 12 --out failures-probe
for op in exists fetch list; do
  run bench_failures.py --generations 100 --indices 3 --shards 3 --trials 12 \
      --rates 0.001,0.01 --fail-ops "$op" --out "failures-op-$op"
done
run bench_failures.py --generations 1000 --indices 5 --shards 4 --trials 30 \
    --rates 0,0.0001,0.001,0.01 --out failures-large
run bench_failures.py --generations 1000 --indices 5 --shards 4 --trials 20 \
    --rates 0.0001,0.001,0.01 --fail-ops exists --out failures-large-headonly
run bench_signing.py
run bench_corroboration.py
run bench_http_faults.py --generations 60 --trials 6

# MinIO only past this point.
run upload_repos.py
run bench_listing.py --repeats 3
run bench_endtoend_s3.py
