#!/usr/bin/env bash
# Reproduces the whole blast-radius campaign from an empty bucket.
#
# Needs: the es-rig namespace up, a port forward to MinIO on 19045, and
# env/es_pass, env/s3_access, env/s3_secret written by harness/creds.sh.
#
# Every object it creates is under the bucket "blastrm", every repository is
# named blast-*, every index blast-* or bxr*/bxms*. harness/cleanup.sh removes
# all of it and nothing else.
set -euo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$D"
H=harness

$H/creds.sh
$H/portforward.sh
python3 $H/s3lib.py mkbucket blastrm >/dev/null || true

# --- base repositories -------------------------------------------------------
$H/build_base_s.sh      # two indices, three snapshots, six index-snapshot pairs
$H/build_base_p.sh      # one index, one snapshot, nine objects
$H/build_base_g.sh      # one snapshot taken with global cluster state
$H/build_base_ms.sh     # one index, one snapshot, built to be mounted

python3 $H/blobrefs.py blastrm base-s > artifacts/b-base-s-blobrefs.tsv
python3 $H/virtual_blobs.py blastrm base-s blast-share2 > artifacts/b-base-s-virtual-blobs.tsv
python3 $H/composition.py > artifacts/b-base-s-composition.tsv

# --- one delete each, on a byte-identical clone ------------------------------
SHARE1=$(awk -F'\t' '$3==1 {print $1}' artifacts/b-base-s-blobrefs.tsv | head -1 | sed 's|^base-s/||')
SHARE3=$(awk -F'\t' '$3==3 {print $1}' artifacts/b-base-s-blobrefs.tsv | tail -1 | sed 's|^base-s/||')
SNAPS=blast-snap-1,blast-snap-2,blast-snap-3
E="python3 $H/experiment.py --restore-snapshots $SNAPS"

$E --id b0  --base base-s --note "control, nothing deleted"
$E --id b1  --base base-s --note "one shared data blob"                --delete "$SHARE3"
$E --id b2  --base base-s --note "one data blob with a single referrer" --delete "$SHARE1"
$E --id b3  --base base-s --note "every data blob"                      --delete-glob "indices/*/0/__*"
$E --id b4  --base base-s --note "root snap-<uuid>.dat"                 --delete "$(python3 $H/pick.py base-s root-snap)"
$E --id b5  --base base-s --note "root meta-<uuid>.dat"                 --delete "$(python3 $H/pick.py base-s root-meta)"
$E --id b6r --base base-s --note "shard index-<gen>"      --reregister  --delete "$(python3 $H/pick.py base-s shard-gen)"
$E --id b7  --base base-s --note "index metadata blob"                  --delete "$(python3 $H/pick.py base-s index-meta)"
$E --id b8r --base base-s --note "index.latest"           --reregister  --delete "index.latest"
$E --id b9r --base base-s --note "current root index-N"   --reregister  --delete "$(python3 $H/pick.py base-s root-gen)"
$E --id b10 --base base-s --note "shard snap-<uuid>.dat"                --delete "$(python3 $H/pick.py base-s shard-snap)"
$E --id b11 --base base-p --note "shard snap-<uuid>.dat, one-snapshot repository" \
    --restore-snapshots blast-p-1 --delete "$(python3 $H/pick.py base-p shard-snap)"
$E --id b12 --base base-g --note "root meta-<uuid>.dat with global state" \
    --restore-snapshots blast-g-1 --delete "$(python3 $H/pick.py base-g root-meta)"

# --- the three claims --------------------------------------------------------
python3 $H/forward.py                       # does damage move forward
python3 $H/next_snapshot.py b20r "$(python3 $H/pick.py base-s shard-gen)" \
    "shard index-<gen> removed, then the next snapshot" --reregister
python3 $H/next_snapshot.py b21 index.latest "index.latest removed, then the next snapshot"
python3 $H/mount.py full_copy               # mounted, fully
python3 $H/mount.py shared_cache            # mounted, partially
python3 $H/mount_catalog.py                 # snapshot removed from the catalog
python3 $H/mount_catalog_sweep.py           # and then the blobs swept
python3 $H/mount_full_sweep.py              # same, fully mounted
python3 $H/mount_nodeloss.py                # fully mounted, node loses its copy

python3 $H/table.py > artifacts/blast-radius-table.tsv
echo "artifacts written to $D/artifacts"
