#!/usr/bin/env bash
# Removes everything this campaign created and nothing else. Every name it
# touches carries a prefix this campaign owns.
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ES=$D/harness/es.sh
for r in $($ES GET "/_cat/repositories?h=id" | tr -d '\r' | grep '^blast-'); do
  $ES DELETE "/_snapshot/$r" >/dev/null
done
for pat in 'bxr*' 'bxms*' 'blast-*'; do
  L=$($ES GET "/_cat/indices/$pat?h=index" | tr -d '\r ' | tr '\n' ',' | sed 's/,$//')
  [ -n "$L" ] && $ES DELETE "/$L" >/dev/null
done
python3 $D/harness/s3lib.py purge blastrm "" >/dev/null
python3 -c "
import sys; sys.path.insert(0,'$D/harness'); import s3lib
print('bucket delete ->', s3lib.request('DELETE','blastrm')[0])"
pkill -f 'port-forward svc/minio 19045' 2>/dev/null
echo "cleanup done"
