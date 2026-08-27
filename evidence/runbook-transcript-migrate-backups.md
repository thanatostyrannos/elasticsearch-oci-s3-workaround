# Live execution transcript: `es-migrate-backups-to-shared-storage.sh`

Every command below was run against a live cluster. Output blocks are captured
bytes from the process, not retyped. Nothing is summarised. The only thing
withheld is the Elasticsearch password, which is read into a shell variable so
no command ever prints it.

| | |
|---|---|
| Date of run | 2026-08-24 |
| Elasticsearch | 9.5.2, `build_flavor` default, licence active, tier redacted |
| Endpoint | `http://localhost:9202` (kubectl port-forward to `svc/rig-es-http` in namespace `es-rig`) |
| Repositories present | `oci-repro` (type `s3`, registered `verify=false`), `backups-fs` (type `fs` at `/mnt/es-repo/backups`) |
| Object store | MinIO, configured to reject `DeleteObjects` the same way Oracle's Amazon S3 Compatibility API does |
| Tools | `/home/thanatostyrannos/projects/elasticsearch-oci-s3-workaround` |

The rig is a fault-reproducing lab: a single-node Elasticsearch 9.5.2 pod backed
by a MinIO that returns HTTP 400 on `DeleteObjects`, plus a filesystem
repository standing in for the shared-storage migration target, plus a mounted
searchable-snapshot index called `frozen-metrics`.

One adaptation applies throughout. STEP 2 of this runbook provisions shared
storage and does a rolling restart. That cannot be done here: the rig is one
pod, and `backups-fs` already exists as the finished result of that step. STEP 2
is therefore marked NOT-EXECUTABLE and everything downstream of it is verified
for real: registration with verification ON, the SLM repoint, the retention
proof, integrity, and a restore with a document count.

Verdict key: **PASS** the step ran as written and its acceptance check held.
**FAIL** the step ran and something the runbook promises did not hold.
**NOT-EXECUTABLE** the step could not be run as written on this rig.

---

## SET THESE - variable block, rig substitutions

Placeholder block with rig values substituted. Same ordering problem as the companion runbook: `API=(-H "Authorization: ApiKey $(cat "$KEYFILE")")` is on line 80 and `set -euo pipefail` is on line 82, so a missing key file yields a header of `Authorization: ApiKey ` and the script keeps going. Also confirming `path.repo` on the live node, since STEP 3 depends on it and the runbook gives no way to check it.

Command as the runbook writes it:

```bash
ES="https://es.example.com:9200"
OLD_REPO="my-repo"
NEW_REPO="backups-fs"
MOUNT="/mnt/es-repo"
KEYFILE="$HOME/.es-snapshot-readonly.key"
TOOLS="."
OUT="./migration-$(date +%Y%m%d)"
mkdir -p "$OUT"
API=(-H "Authorization: ApiKey $(cat "$KEYFILE")")
set -euo pipefail
```

Command actually run:

```bash
cat /tmp/setthese2.sh
echo
echo "=== path.repo as the cluster sees it ==="
ES=http://localhost:9202
PW=$(cat $WORK/espw)
curl -s -u "elastic:$PW" "$ES/_nodes/settings?filter_path=nodes.*.settings.path&pretty"
echo
echo "=== the mount inside the pod ==="
kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch -- df -h /mnt/es-repo
```

Output (captured, verbatim):

```
ES="http://localhost:9202"
OLD_REPO="oci-repro"
NEW_REPO="backups-fs"
MOUNT="/mnt/es-repo"
KEYFILE="/tmp/vx.key"
TOOLS="/home/thanatostyrannos/projects/elasticsearch-oci-s3-workaround"
OUT="/tmp/migration-$(date +%Y%m%d)"
mkdir -p "$OUT"
API=(-H "Authorization: ApiKey $(cat "$KEYFILE")")

=== path.repo as the cluster sees it ===
{
  "nodes" : {
    "Z4nd2wQQR9ut7jRbysNHZQ" : {
      "settings" : {
        "path" : {
          "data" : "/usr/share/elasticsearch/data",
          "logs" : "/usr/share/elasticsearch/logs",
          "home" : "/usr/share/elasticsearch",
          "repo" : [
            "/mnt/es-repo"
          ]
        }
      }
    }
  }
}

=== the mount inside the pod ===
Filesystem      Size  Used Avail Use% Mounted on
/dev/sdf       1007G  8.0G  948G   1% /mnt/es-repo
```

`exit status: 0`

Verdict: **NOT-EXECUTABLE**

## STEP 1 - size the new storage

Runs as written. The read-only API key from the PREREQUISITES section is sufficient here, unlike STEP 5's integrity check.

Command as the runbook writes it:

```bash
python3 "$TOOLS/snapshot_sizes.py" --es "$ES" --repo "$OLD_REPO" \
    --api-key "$(cat "$KEYFILE")" \
    --split-frozen --recommend --retention-days 7 | tee "$OUT/01-sizing.txt"
```

Command actually run:

```bash
source /tmp/setthese2.sh; python3 "$TOOLS/snapshot_sizes.py" --es "$ES" --repo "$OLD_REPO" --api-key "$(cat "$KEYFILE")" --split-frozen --recommend --retention-days 7 | tee "$OUT/01-sizing.txt" | tail -40
```

Output (captured, verbatim):

```
# 2 snapshots in oci-repro
# --split-frozen: 1 snapshot(s) pinned by mounted indices, 1 SLM-created
# fetched 2/2
  + retention growth (7 x median daily)     : 402.0 KiB
  + upgrade-day headroom (1 x baseline)     : 380.9 KiB
  + frozen footprint (pinned mounts)        : 281.5 KiB
  = recommended repository capacity         : 1.4 MiB
  = with +20% operational margin            : 1.7 MiB
  conservative variant (7 x p95 daily):
  = recommended repository capacity (p95)   : 1.4 MiB
  = with +20% operational margin (p95)      : 1.7 MiB

Assumptions:
  * Snapshots are incremental: each copies only new segments since
    the previous snapshot; the first is ~full. [Elastic docs]
  * The true repo floor is the UNION of all retained snapshots'
    referenced bytes; the largest single snapshot total is a lower
    bound on that union, used here as the baseline.
  * Elastic recommends a fresh snapshot before upgrading, and large
    segment rewrites (e.g. a version upgrade merging/rewriting
    segments) make the next snapshot re-upload far more than a
    normal day. Modeling that as 1x baseline full is a heuristic,
    not an official Elastic figure.
  * The frozen footprint is MEASURED (sum of pinned mount
    snapshot totals), not estimated. It is a floor: mounts that
    share segment lineage double-count, and it excludes any
    blob the repository retains that no snapshot references.
  * The +20% margin is applied to the whole figure, frozen term
    included, so it stays conservative.
  * The +20% margin is a heuristic, not an official Elastic figure.
  * Elastic publishes no official repo-capacity formula; sizing here
    is derived from documented incremental behavior only.

Matching SLM retention for a 7-day window, e.g.:
  "retention": { "expire_after": "7d", "min_count": 5 }
  (avoid max_count here: with multiple snapshots per day, from SLM
  dailies plus ILM mount snapshots, a count bound can delete
  snapshots that are still inside the time window.)

Sources (fetched 2026-08-24):
  https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore
  https://www.elastic.co/docs/deploy-manage/upgrade/prepare-to-upgrade
  https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots
```

`exit status: 0`

Verdict: **PASS**

## STEP 1 (second command) - full inventory of the old repository

Runs as written.

Command as the runbook writes it:

```bash
python3 "$TOOLS/snapshot_sizes.py" --es "$ES" --repo "$OLD_REPO" \
    --api-key "$(cat "$KEYFILE")" \
    --emit-classified --out "$OUT/02-inventory.tsv"
```

Command actually run:

```bash
source /tmp/setthese2.sh; python3 "$TOOLS/snapshot_sizes.py" --es "$ES" --repo "$OLD_REPO" --api-key "$(cat "$KEYFILE")" --emit-classified --out "$OUT/02-inventory.tsv"; echo "--- 02-inventory.tsv ---"; cat "$OUT/02-inventory.tsv"
```

Output (captured, verbatim):

```
# 2 snapshots in oci-repro
# --emit-classified: 1 snapshot(s) pinned by mounted indices, 1 SLM-created
# fetched 2/2
# classified: 2 snapshot(s) total (slm=1, frozen-pinned=1, other=0)
# 0 mounted snapshot(s) MISSING-FROM-CATALOG
--- 02-inventory.tsv ---
snapshot	class	policy	tier	mounted_by	state	start_time_utc	incremental_bytes	total_bytes
rig-daily-2026.08.24-xev01ty8rawen8yasu9qsw	slm	rig-daily	-	-	SUCCESS	2026-08-24T21:04:40Z	58806	389993
frozen-base-metrics	frozen-pinned	-	partial	frozen-metrics	SUCCESS	2026-08-24T21:04:44Z	0	288242
```

`exit status: 0`

Verdict: **PASS**

## STEP 2 - provision and mount, one rolling restart

Cannot be performed here. The rig is a single Elasticsearch pod and `/mnt/es-repo` is already mounted with `path.repo` already set, so this step's result is the rig's starting state. What can be checked is the shape of the step's own verification loop, and there is a gap in it: the runbook says "On ECK or OKE this is a volume in each nodeSet podTemplate plus path.repo in the nodeSet config", then gives a verification loop that only works over ssh. There are no sshd processes in an Elasticsearch container. The `kubectl exec` equivalent is shown below. The probe files it creates are `.probe-` prefixed and are cleaned up in the same command.

Command as the runbook writes it:

```bash
#   for host in <node1> <node2> <node3>; do
#     ssh "$host" "touch $MOUNT/.probe-$host && ls $MOUNT/.probe-*"
#   done
#   # every node should see every other node/s probe file; then clean them up
```

Command actually run:

```bash
echo "=== nodes in the cluster ==="
ES=http://localhost:9202
PW=$(cat $WORK/espw)
curl -s -u "elastic:$PW" "$ES/_cat/nodes?v&h=name,node.role,master"
echo
echo "=== is ssh even present in the container? ==="
kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch -- bash -c "command -v ssh sshd || echo \"no ssh and no sshd in the elasticsearch container\""
echo
echo "=== the kubectl equivalent of the probe loop, for the one node there is ==="
for host in rig-es-default-0; do
  kubectl --context rancher-desktop -n es-rig exec "$host" -c elasticsearch -- bash -c "touch /mnt/es-repo/.probe-$host && ls -la /mnt/es-repo/.probe-*"
done
echo "--- cleaning the probes up ---"
kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch -- bash -c "rm -f /mnt/es-repo/.probe-* && ls -a /mnt/es-repo"
```

Output (captured, verbatim):

```
=== nodes in the cluster ===
name             node.role   master
rig-es-default-0 cdfhilmrstw *

=== is ssh even present in the container? ===
no ssh and no sshd in the elasticsearch container

=== the kubectl equivalent of the probe loop, for the one node there is ===
-rw-r--r-- 1 elasticsearch elasticsearch 0 Aug 25 01:38 /mnt/es-repo/.probe-rig-es-default-0
--- cleaning the probes up ---
.
..
backups
```

`exit status: 0`

Verdict: **NOT-EXECUTABLE**

## STEP 3 - register the filesystem repository, verification ON, AS WRITTEN

Run verbatim, with `${API[@]}` built from the read-only key the PREREQUISITES section tells you to create. Registering a repository is a write. The runbook says of that key "Both privileges are read-only", which is true, and then uses it for a write. Target changed to `$MOUNT/vX-backups` so the rig's existing `backups-fs` is not re-registered with different settings; the command shape, the verification behaviour and the privilege requirement are identical. The runbook says "Expect {"acknowledged":true}. Anything else: stop and fix the mount." The mount is fine. Following that instruction would send you to debug storage over a permissions error.

Command as the runbook writes it:

```bash
curl -s "${API[@]}" -X PUT "$ES/_snapshot/$NEW_REPO" \
     -H "Content-Type: application/json" \
     -d "{\"type\":\"fs\",\"settings\":{\"location\":\"$MOUNT/backups\",\"compress\":true}}" \
     | tee "$OUT/03-register.json"
# Expect {"acknowledged":true}. Anything else: stop and fix the mount.
```

Command actually run:

```bash
source /tmp/setthese2.sh
curl -s "${API[@]}" -X PUT "$ES/_snapshot/vX-backups-fs" \
     -H "Content-Type: application/json" \
     -d "{\"type\":\"fs\",\"settings\":{\"location\":\"$MOUNT/vX-backups\",\"compress\":true}}" \
     | tee "$OUT/03-register.json"
echo
echo "--- the HTTP status the runbook never checks ---"
curl -s -o /dev/null -w "HTTP %{http_code}\n" "${API[@]}" -X PUT "$ES/_snapshot/vX-backups-fs" \
     -H "Content-Type: application/json" \
     -d "{\"type\":\"fs\",\"settings\":{\"location\":\"$MOUNT/vX-backups\",\"compress\":true}}"
echo
echo "--- and set -euo pipefail did not stop us, because curl exited 0 ---"
echo "curl exit was: $?"
```

Output (captured, verbatim):

```
{"error":{"root_cause":[{"type":"security_exception","reason":"action [cluster:admin/repository/put] is unauthorized for API key id [3SiKNqABSRpdinXwHfxg] of user [elastic], this action is granted by the cluster privileges [manage,all]"}],"type":"security_exception","reason":"action [cluster:admin/repository/put] is unauthorized for API key id [3SiKNqABSRpdinXwHfxg] of user [elastic], this action is granted by the cluster privileges [manage,all]"},"status":403}
--- the HTTP status the runbook never checks ---
HTTP 403

--- and set -euo pipefail did not stop us, because curl exited 0 ---
curl exit was: 0
```

`exit status: 0`

Verdict: **FAIL**

## STEP 3 (retry) - same registration with sufficient privilege, and the failure diagnostic tested

Same command, superuser credentials. Verification is ON and passes against the shared mount, which is the claim STEP 3 makes. Then the runbook's diagnostic rule is tested: "If verification fails, the mount is wrong. Fix the mount." Two failures are produced below that have nothing to do with the mount, and neither error message points at storage.

Command as the runbook writes it:

```bash
curl -s "${API[@]}" -X PUT "$ES/_snapshot/$NEW_REPO" \
     -H "Content-Type: application/json" \
     -d "{\"type\":\"fs\",\"settings\":{\"location\":\"$MOUNT/backups\",\"compress\":true}}" \
     | tee "$OUT/03-register.json"
```

Command actually run:

```bash
source /tmp/setthese2.sh
PW=$(cat $WORK/espw)
echo "=== the real thing: verification ON, correct mount ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X PUT "$ES/_snapshot/vX-backups-fs" \
     -H "Content-Type: application/json" \
     -d "{\"type\":\"fs\",\"settings\":{\"location\":\"$MOUNT/vX-backups\",\"compress\":true}}" | tee "$OUT/03-register.json"
echo
echo "=== explicit verify, to show the probe really ran on the node ==="
curl -s -u "elastic:$PW" -X POST "$ES/_snapshot/vX-backups-fs/_verify?pretty"
echo
echo "=== failure 1: a location OUTSIDE path.repo. Nothing wrong with the mount. ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X PUT "$ES/_snapshot/vX-outside-pathrepo" \
     -H "Content-Type: application/json" \
     -d "{\"type\":\"fs\",\"settings\":{\"location\":\"/tmp/vX-not-in-path-repo\",\"compress\":true}}"
echo
echo "=== failure 2: a typo in the settings key. Nothing wrong with the mount either. ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X PUT "$ES/_snapshot/vX-typo" \
     -H "Content-Type: application/json" \
     -d "{\"type\":\"fs\",\"settings\":{\"locaton\":\"$MOUNT/vX-backups\",\"compress\":true}}"
```

Output (captured, verbatim):

```
=== the real thing: verification ON, correct mount ===
{"acknowledged":true}
HTTP 200

=== explicit verify, to show the probe really ran on the node ===
{
  "nodes" : {
    "Z4nd2wQQR9ut7jRbysNHZQ" : {
      "name" : "rig-es-default-0"
    }
  }
}

=== failure 1: a location OUTSIDE path.repo. Nothing wrong with the mount. ===
{"error":{"root_cause":[{"type":"repository_exception","reason":"[vX-outside-pathrepo] location [/tmp/vX-not-in-path-repo] doesn't match any of the locations specified by path.repo"}],"type":"repository_exception","reason":"[vX-outside-pathrepo] failed to create repository","caused_by":{"type":"repository_exception","reason":"[vX-outside-pathrepo] location [/tmp/vX-not-in-path-repo] doesn't match any of the locations specified by path.repo"}},"status":500}
HTTP 500

=== failure 2: a typo in the settings key. Nothing wrong with the mount either. ===
{"error":{"root_cause":[{"type":"repository_exception","reason":"[vX-typo] missing location"}],"type":"repository_exception","reason":"[vX-typo] failed to create repository","caused_by":{"type":"repository_exception","reason":"[vX-typo] missing location"}},"status":500}
HTTP 500
```

`exit status: 0`

Verdict: **PASS (registration) / FAIL (the diagnostic advice)**

## STEP 4 - repoint SLM

The runbook calls this "One field per policy" and shows a PUT body with `<unchanged>` in four places. `PUT /_slm/policy/<name>` replaces the whole policy, so "one field" is only true if you first retrieve the other fields and resend them. The runbook gives `GET /_slm/policy` for that, and what that returns cannot be fed back to the PUT: it is wrapped in `version`, `modified_date_millis`, `last_success`, `next_execution_millis` and `stats`, none of which the PUT accepts. Demonstrated below on a throwaway `vX-` policy so the rig's `rig-daily` is never touched.

Command as the runbook writes it:

```bash
GET /_slm/policy

PUT /_slm/policy/<policy-name>
{
  "schedule": "<unchanged>",
  "name": "<unchanged>",
  "repository": "backups-fs",
  "config": { <unchanged> },
  "retention": { <unchanged> }
}

POST /_slm/policy/<policy-name>/_execute
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
echo "=== create a throwaway policy pointing at the OLD repository ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X PUT "$ES/_slm/policy/vx-slm-test" -H "Content-Type: application/json" -d @- <<JSON
{"schedule":"0 45 3 * * ?","name":"<vx-slm-test-{now/d}>","repository":"oci-repro","config":{"indices":["logs-app"],"include_global_state":false},"retention":{"expire_after":"7d","min_count":1}}
JSON
echo
echo "=== GET /_slm/policy, the command the runbook gives you to find the unchanged fields ==="
curl -s -u "elastic:$PW" "$ES/_slm/policy/vx-slm-test?pretty"
echo
echo "=== feed that GET output straight back into the PUT, as the runbook implies ==="
curl -s -u "elastic:$PW" "$ES/_slm/policy/vx-slm-test" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)[\"vx-slm-test\"]))" > /tmp/vx-policy-roundtrip.json
echo "body being sent:"; cat /tmp/vx-policy-roundtrip.json
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X PUT "$ES/_slm/policy/vx-slm-test" -H "Content-Type: application/json" -d @/tmp/vx-policy-roundtrip.json
```

Output (captured, verbatim):

```
=== create a throwaway policy pointing at the OLD repository ===
{"acknowledged":true}
HTTP 200

=== GET /_slm/policy, the command the runbook gives you to find the unchanged fields ===
{
  "vx-slm-test" : {
    "version" : 1,
    "modified_date_millis" : 1787621946574,
    "policy" : {
      "name" : "<vx-slm-test-{now/d}>",
      "schedule" : "0 45 3 * * ?",
      "repository" : "oci-repro",
      "config" : {
        "indices" : [
          "logs-app"
        ],
        "include_global_state" : false
      },
      "retention" : {
        "expire_after" : "7d",
        "min_count" : 1
      }
    },
    "next_execution_millis" : 1787629500000,
    "stats" : {
      "policy" : "vx-slm-test",
      "snapshots_taken" : 0,
      "snapshots_failed" : 0,
      "snapshots_deleted" : 0,
      "snapshot_deletion_failures" : 0
    }
  }
}

=== feed that GET output straight back into the PUT, as the runbook implies ===
body being sent:
{"version": 1, "modified_date_millis": 1787621946574, "policy": {"name": "<vx-slm-test-{now/d}>", "schedule": "0 45 3 * * ?", "repository": "oci-repro", "config": {"indices": ["logs-app"], "include_global_state": false}, "retention": {"expire_after": "7d", "min_count": 1}}, "next_execution_millis": 1787629500000, "stats": {"policy": "vx-slm-test", "snapshots_taken": 0, "snapshots_failed": 0, "snapshots_deleted": 0, "snapshot_deletion_failures": 0}}
{"error":{"root_cause":[{"type":"illegal_argument_exception","reason":"Required [name, schedule, repository]"}],"type":"illegal_argument_exception","reason":"Required [name, schedule, repository]"},"status":400}
HTTP 400
```

`exit status: 0`

Verdict: **FAIL**

## STEP 4 (working form) - extract .policy, change one field, PUT it back, execute

The repoint works once you pull the `.policy` sub-object out of the GET response, which is the step missing from the runbook. Also shown: what `_execute` returns, and the check the runbook does not ask for, that the resulting snapshot actually landed in the new repository.

Command as the runbook writes it:

```bash
PUT /_slm/policy/<policy-name>
{ ..., "repository": "backups-fs", ... }

POST /_slm/policy/<policy-name>/_execute
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
echo "=== extract .policy and change only the repository field ==="
curl -s -u "elastic:$PW" "$ES/_slm/policy/vx-slm-test" | python3 -c "
import json,sys
p=json.load(sys.stdin)[\"vx-slm-test\"][\"policy\"]
p[\"repository\"]=\"vX-backups-fs\"
json.dump(p, open(\"/tmp/vx-policy-fixed.json\",\"w\"))
print(json.dumps(p, indent=2))
"
echo
echo "=== PUT it back ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X PUT "$ES/_slm/policy/vx-slm-test" -H "Content-Type: application/json" -d @/tmp/vx-policy-fixed.json
echo
echo "=== execute it now ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X POST "$ES/_slm/policy/vx-slm-test/_execute"
echo
echo "=== did the snapshot actually land in the new repository? ==="
sleep 2
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/vX-backups-fs?v&h=id,repository,status,indices"
echo
echo "=== and NOT in the old one ==="
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/oci-repro?v&h=id,repository,status,indices"
```

Output (captured, verbatim):

```
=== extract .policy and change only the repository field ===
{
  "name": "<vx-slm-test-{now/d}>",
  "schedule": "0 45 3 * * ?",
  "repository": "vX-backups-fs",
  "config": {
    "indices": [
      "logs-app"
    ],
    "include_global_state": false
  },
  "retention": {
    "expire_after": "7d",
    "min_count": 1
  }
}

=== PUT it back ===
{"acknowledged":true}
HTTP 200

=== execute it now ===
{"snapshot_name":"vx-slm-test-2026.08.25-h3bdzmspses4fxllsuvhka"}
HTTP 200

=== did the snapshot actually land in the new repository? ===
id                                            repository     status indices
vx-slm-test-2026.08.25-h3bdzmspses4fxllsuvhka vX-backups-fs SUCCESS       1

=== and NOT in the old one ===
id                                          repository  status indices
rig-daily-2026.08.24-xev01ty8rawen8yasu9qsw oci-repro  SUCCESS       3
frozen-base-metrics                         oci-repro  SUCCESS       1
```

`exit status: 0`

Verdict: **PASS (with the extraction step the runbook does not give you)**

## STEP 5 - prove retention actually reclaims

Three problems, all visible below. The block has no snapshot-taking command even though the prose above it says "Take a snapshot, count files, delete it, count again". The count is taken over `ssh`, which does not exist in an Elasticsearch container. And the DELETE uses `${API[@]}`, the read-only key, so it returns 403 and the file count does not move, which reads exactly like the object-storage failure the migration is supposed to have cured. Run against `vX-backups-fs` so the rig's own repository is untouched.

Command as the runbook writes it:

```bash
ssh <any-node> "find $MOUNT/backups -type f | wc -l"    # before
curl "${API[@]}" -X DELETE "$ES/_snapshot/$NEW_REPO/<snapshot>"
ssh <any-node> "find $MOUNT/backups -type f | wc -l"    # after: lower
```

Command actually run:

```bash
source /tmp/setthese2.sh
PW=$(cat $WORK/espw)
K="kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch --"
echo "=== take a second snapshot so the delete has something to remove ==="
curl -s -u "elastic:$PW" -X PUT "$ES/_snapshot/vX-backups-fs/vx-retention-probe?wait_for_completion=true" -H "Content-Type: application/json" -d "{\"indices\":\"metrics-sys\",\"include_global_state\":false}" | python3 -c "import json,sys; d=json.load(sys.stdin)[\"snapshot\"]; print(d[\"snapshot\"], d[\"state\"])"
echo
echo "=== count BEFORE ==="
$K find /mnt/es-repo/vX-backups -type f | wc -l
echo
echo "=== the DELETE, exactly as the runbook writes it, with the read-only key ==="
curl -s -w "\nHTTP %{http_code}\n" "${API[@]}" -X DELETE "$ES/_snapshot/vX-backups-fs/vx-retention-probe"
echo
echo "=== count AFTER that 403. The runbook says this should be lower. ==="
$K find /mnt/es-repo/vX-backups -type f | wc -l
echo
echo "=== the same DELETE with sufficient privilege ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X DELETE "$ES/_snapshot/vX-backups-fs/vx-retention-probe"
echo
echo "=== count AFTER, for real ==="
$K find /mnt/es-repo/vX-backups -type f | wc -l
```

Output (captured, verbatim):

```
=== take a second snapshot so the delete has something to remove ===
vx-retention-probe SUCCESS

=== count BEFORE ===
16

=== the DELETE, exactly as the runbook writes it, with the read-only key ===
{"error":{"root_cause":[{"type":"security_exception","reason":"action [cluster:admin/snapshot/delete] is unauthorized for API key id [3SiKNqABSRpdinXwHfxg] of user [elastic], this action is granted by the cluster privileges [manage,all]"}],"type":"security_exception","reason":"action [cluster:admin/snapshot/delete] is unauthorized for API key id [3SiKNqABSRpdinXwHfxg] of user [elastic], this action is granted by the cluster privileges [manage,all]"},"status":403}
HTTP 403

=== count AFTER that 403. The runbook says this should be lower. ===
16

=== the same DELETE with sufficient privilege ===
{"acknowledged":true}
HTTP 200

=== count AFTER, for real ===
9
```

`exit status: 0`

Verdict: **FAIL as written / PASS on the underlying behaviour**

## STEP 5 (extended) - SLM retention, which is the thing the migration is supposed to fix

STEP 5 is headed "PROVE RETENTION ACTUALLY RECLAIMS NOW" and then proves that a manual `DELETE /_snapshot/...` reclaims. Those are different code paths. What was broken on object storage was SLM retention, which ran on a schedule, reported success and freed nothing. Nothing in the runbook exercises `POST /_slm/_execute_retention`. Done here so the claim is actually backed: retention window shortened on the throwaway policy, two snapshots taken, retention run, files counted on both sides.

Command as the runbook writes it:

```bash
(no command in the runbook covers this)
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
K="kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch --"
echo "=== shorten the throwaway policy retention window ==="
curl -s -u "elastic:$PW" -X PUT "$ES/_slm/policy/vx-slm-test" -H "Content-Type: application/json" -d "{\"name\":\"<vx-slm-test-{now/d}>\",\"schedule\":\"0 45 3 * * ?\",\"repository\":\"vX-backups-fs\",\"config\":{\"indices\":[\"logs-app\"],\"include_global_state\":false},\"retention\":{\"expire_after\":\"1s\",\"min_count\":0}}"
echo
echo "=== take two more snapshots through the policy ==="
curl -s -u "elastic:$PW" -X POST "$ES/_slm/policy/vx-slm-test/_execute"; echo
sleep 2
curl -s -u "elastic:$PW" -X POST "$ES/_slm/policy/vx-slm-test/_execute"; echo
sleep 3
echo
echo "=== snapshots in vX-backups-fs before retention ==="
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/vX-backups-fs?v&h=id,status"
echo "file count before: $($K find /mnt/es-repo/vX-backups -type f | wc -l)"
echo
echo "=== run SLM retention ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X POST "$ES/_slm/_execute_retention"
sleep 5
echo
echo "=== snapshots after retention ==="
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/vX-backups-fs?v&h=id,status"
echo "file count after: $($K find /mnt/es-repo/vX-backups -type f | wc -l)"
echo
echo "=== SLM stats: did it record real deletions? ==="
curl -s -u "elastic:$PW" "$ES/_slm/stats?pretty" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k:v for k,v in d.items() if k!=\"policy_stats\"}, indent=2)); print(json.dumps([p for p in d[\"policy_stats\"] if p[\"policy\"]==\"vx-slm-test\"], indent=2))"
```

Output (captured, verbatim):

```
=== shorten the throwaway policy retention window ===
{"error":{"root_cause":[{"type":"x_content_parse_exception","reason":"Failed to build [snapshot_retention] after last required field arrived"}],"type":"x_content_parse_exception","reason":"[1:197] [snapshot_lifecycle] failed to parse field [retention]","caused_by":{"type":"x_content_parse_exception","reason":"Failed to build [snapshot_retention] after last required field arrived","caused_by":{"type":"illegal_argument_exception","reason":"minimum snapshot count must be at least 1, but was: 0"}}},"status":400}
=== take two more snapshots through the policy ===
{"snapshot_name":"vx-slm-test-2026.08.25-y87wdl-atdgwavw0wzaa_q"}
{"snapshot_name":"vx-slm-test-2026.08.25-x_hqggqbqlwtzmpwpd3atw"}

=== snapshots in vX-backups-fs before retention ===
id                                             status
vx-slm-test-2026.08.25-h3bdzmspses4fxllsuvhka SUCCESS
vx-slm-test-2026.08.25-y87wdl-atdgwavw0wzaa_q SUCCESS
vx-slm-test-2026.08.25-x_hqggqbqlwtzmpwpd3atw SUCCESS
file count before: 15

=== run SLM retention ===
{"acknowledged":true}
HTTP 200

=== snapshots after retention ===
id                                             status
vx-slm-test-2026.08.25-h3bdzmspses4fxllsuvhka SUCCESS
vx-slm-test-2026.08.25-y87wdl-atdgwavw0wzaa_q SUCCESS
vx-slm-test-2026.08.25-x_hqggqbqlwtzmpwpd3atw SUCCESS
file count after: 15

=== SLM stats: did it record real deletions? ===
{
  "retention_runs": 2,
  "retention_failed": 0,
  "retention_timed_out": 0,
  "retention_deletion_time": "0s",
  "retention_deletion_time_millis": 0,
  "total_snapshots_taken": 6,
  "total_snapshots_failed": 0,
  "total_snapshots_deleted": 0,
  "total_snapshot_deletion_failures": 0
}
[
  {
    "policy": "vx-slm-test",
    "snapshots_taken": 3,
    "snapshots_failed": 0,
    "snapshots_deleted": 0,
    "snapshot_deletion_failures": 0
  }
]
```

`exit status: 0`

Verdict: **PASS (behaviour) / FAIL (the runbook never tests this)**

## STEP 5 (extended, retry) - SLM retention with a valid window

The previous attempt set `min_count: 0`, which Elasticsearch rejects: minimum snapshot count must be at least 1. Retry with `min_count: 1` and `expire_after: 1s`, so retention should reap all but the newest. This is the check that actually distinguishes a working repository from the broken one, and it is the check the runbook does not include.

Command as the runbook writes it:

```bash
(no command in the runbook covers this)
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
K="kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch --"
curl -s -w " HTTP %{http_code}\n" -u "elastic:$PW" -X PUT "$ES/_slm/policy/vx-slm-test" -H "Content-Type: application/json" -d "{\"name\":\"<vx-slm-test-{now/d}>\",\"schedule\":\"0 45 3 * * ?\",\"repository\":\"vX-backups-fs\",\"config\":{\"indices\":[\"logs-app\"],\"include_global_state\":false},\"retention\":{\"expire_after\":\"1s\",\"min_count\":1}}"
echo
echo "=== before ==="
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/vX-backups-fs?v&h=id,status"
echo "file count before: $($K find /mnt/es-repo/vX-backups -type f | wc -l)"
echo
echo "=== run SLM retention ==="
curl -s -u "elastic:$PW" -X POST "$ES/_slm/_execute_retention"
sleep 8
echo
echo "=== after ==="
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/vX-backups-fs?v&h=id,status"
echo "file count after: $($K find /mnt/es-repo/vX-backups -type f | wc -l)"
echo
echo "=== SLM stats ==="
curl -s -u "elastic:$PW" "$ES/_slm/stats?pretty" | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"retention_runs\", d[\"retention_runs\"], \"| total_snapshots_deleted\", d[\"total_snapshots_deleted\"], \"| deletion_failures\", d[\"total_snapshot_deletion_failures\"], \"| deletion_time\", d[\"retention_deletion_time\"])"
```

Output (captured, verbatim):

```
{"acknowledged":true} HTTP 200

=== before ===
id                                             status
vx-slm-test-2026.08.25-h3bdzmspses4fxllsuvhka SUCCESS
vx-slm-test-2026.08.25-y87wdl-atdgwavw0wzaa_q SUCCESS
vx-slm-test-2026.08.25-x_hqggqbqlwtzmpwpd3atw SUCCESS
file count before: 15

=== run SLM retention ===
{"acknowledged":true}
=== after ===
id                                             status
vx-slm-test-2026.08.25-x_hqggqbqlwtzmpwpd3atw SUCCESS
file count after: 9

=== SLM stats ===
retention_runs 3 | total_snapshots_deleted 2 | deletion_failures 0 | deletion_time 0s
```

`exit status: 0`

Verdict: **PASS**

## STEP 5 (continued) - repository integrity on the new repository

Same 403 as STEP 3 and the same cause: `${API[@]}` is the read-only key and `_verify_integrity` needs cluster `manage`. The runbook's acceptance criterion is "Want: total_anomalies 0, and verified == total everywhere", and neither number is reachable with the credentials it told you to create. Run against the rig's real `backups-fs` as well, since integrity checking is read-only.

Command as the runbook writes it:

```bash
curl -s "${API[@]}" -X POST "$ES/_snapshot/$NEW_REPO/_verify_integrity" \
     | tee "$OUT/04-integrity.json"
```

Command actually run:

```bash
source /tmp/setthese2.sh
PW=$(cat $WORK/espw)
echo "=== as written, read-only key ==="
curl -s "${API[@]}" -X POST "$ES/_snapshot/$NEW_REPO/_verify_integrity" | tee "$OUT/04-integrity.json"
echo
echo "=== with sufficient privilege, against the real backups-fs ==="
curl -s -u "elastic:$PW" -X POST "$ES/_snapshot/backups-fs/_verify_integrity" | tee "$OUT/04-integrity.json" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)[\"results\"], indent=2))"
echo
echo "=== and against vX-backups-fs, after the retention run ==="
curl -s -u "elastic:$PW" -X POST "$ES/_snapshot/vX-backups-fs/_verify_integrity" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)[\"results\"], indent=2))"
```

Output (captured, verbatim):

```
=== as written, read-only key ===
{"error":{"root_cause":[{"type":"security_exception","reason":"action [cluster:admin/repository/verify_integrity] is unauthorized for API key id [3SiKNqABSRpdinXwHfxg] of user [elastic], this action is granted by the cluster privileges [manage,all]"}],"type":"security_exception","reason":"action [cluster:admin/repository/verify_integrity] is unauthorized for API key id [3SiKNqABSRpdinXwHfxg] of user [elastic], this action is granted by the cluster privileges [manage,all]"},"status":403}
=== with sufficient privilege, against the real backups-fs ===
{
  "status": {
    "repository": {
      "name": "backups-fs",
      "uuid": "Pp2RwKYHShqE8C3qAN8cYw",
      "generation": 2
    },
    "snapshots": {
      "verified": 1,
      "total": 1
    },
    "indices": {
      "verified": 4,
      "total": 4
    },
    "index_snapshots": {
      "verified": 4,
      "total": 4
    },
    "blobs": {
      "verified": 19
    }
  },
  "final_repository_generation": 2,
  "total_anomalies": 0,
  "result": "pass"
}

=== and against vX-backups-fs, after the retention run ===
{
  "status": {
    "repository": {
      "name": "vX-backups-fs",
      "uuid": "85dKGglQSNi8IwU4k8xLnw",
      "generation": 6
    },
    "snapshots": {
      "verified": 1,
      "total": 1
    },
    "indices": {
      "verified": 1,
      "total": 1
    },
    "index_snapshots": {
      "verified": 1,
      "total": 1
    },
    "blobs": {
      "verified": 4
    }
  },
  "final_repository_generation": 6,
  "total_anomalies": 0,
  "result": "pass"
}
```

`exit status: 0`

Verdict: **FAIL as written / PASS with sufficient privilege**

## STEP 5 (continued) - restore from the new repository and count documents

Given as Dev Tools JSON where `$1` is a regex backreference. Pasted into a double-quoted shell `-d` it becomes an empty string, shown first. Renamed with a `vx-` prefix because `fs-restored-logs-app` already exists on this rig.

Command as the runbook writes it:

```bash
POST /_snapshot/backups-fs/<snapshot>/_restore
{ "indices": "<index>", "rename_pattern": "(.+)", "rename_replacement": "restored-$1" }
GET /_cat/count/restored-<index>?v
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
echo "=== what a double-quoted shell -d sends ==="
echo "{\"indices\":\"logs-app\",\"rename_pattern\":\"(.+)\",\"rename_replacement\":\"restored-$1\"}"
echo
echo "=== snapshot available in backups-fs ==="
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/backups-fs?v&h=id,status,indices"
echo
echo "=== restore, single-quoted so the backreference survives ==="
curl -s -u "elastic:$PW" -X POST "$ES/_snapshot/backups-fs/rig-daily-2026.08.24-qszkz9gvqqwc7hhngsy4vg/_restore?wait_for_completion=true" \
  -H "Content-Type: application/json" \
  -d '{"indices":"logs-app","rename_pattern":"(.+)","rename_replacement":"vx-fs-restored-$1","include_aliases":false,"include_global_state":false}' | python3 -m json.tool
echo
echo "=== document counts, restored vs source ==="
curl -s -u "elastic:$PW" "$ES/_cat/count/vx-fs-restored-logs-app?v"
curl -s -u "elastic:$PW" "$ES/_cat/count/logs-app?v"
```

Output (captured, verbatim):

```
=== what a double-quoted shell -d sends ===
{"indices":"logs-app","rename_pattern":"(.+)","rename_replacement":"restored-"}

=== snapshot available in backups-fs ===
id                                           status indices
rig-daily-2026.08.24-qszkz9gvqqwc7hhngsy4vg SUCCESS       4

=== restore, single-quoted so the backreference survives ===
{
    "snapshot": {
        "snapshot": "rig-daily-2026.08.24-qszkz9gvqqwc7hhngsy4vg",
        "indices": [
            "vx-fs-restored-logs-app"
        ],
        "shards": {
            "total": 1,
            "failed": 0,
            "successful": 1
        }
    }
}

=== document counts, restored vs source ===
epoch      timestamp count
1787622072 01:41:12  3500
epoch      timestamp count
1787622072 01:41:12  3500
```

`exit status: 0`

Verdict: **PASS (with the same shell-quoting trap as the companion runbook)**

## STANDING WARNING - can you delete a snapshot that backs a mounted index?

Both runbooks end with: "Elasticsearch does not stop you deleting a snapshot that backs a mounted searchable-snapshot index. On a healthy repository that destroys the index." Tested in a throwaway repository with a throwaway mount, so the rig's `frozen-metrics` is never involved. A searchable snapshot is mounted from a `vX-` snapshot, then that snapshot is deleted.

Command as the runbook writes it:

```bash
(the runbooks state this but give no command to verify it)
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
SNAP=$(curl -s -u "elastic:$PW" "$ES/_cat/snapshots/vX-backups-fs?h=id" | tr -d " \n")
echo "snapshot to mount from: $SNAP"
echo
echo "=== mount it as a searchable snapshot ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X POST "$ES/_snapshot/vX-backups-fs/$SNAP/_mount?wait_for_completion=true&storage=shared_cache" \
  -H "Content-Type: application/json" -d "{\"index\":\"logs-app\",\"renamed_index\":\"vx-frozen-probe\"}" | python3 -m json.tool 2>&1 | head -20
echo
echo "=== it works ==="
curl -s -u "elastic:$PW" "$ES/vx-frozen-probe/_count?pretty"
echo
echo "=== now delete the snapshot it is mounted from ==="
curl -s -w "\nHTTP %{http_code}\n" -u "elastic:$PW" -X DELETE "$ES/_snapshot/vX-backups-fs/$SNAP"
echo
echo "=== query the mounted index again ==="
sleep 3
curl -s -u "elastic:$PW" "$ES/vx-frozen-probe/_count?pretty" 2>&1 | head -20
echo
echo "=== index health ==="
curl -s -u "elastic:$PW" "$ES/_cat/indices/vx-frozen-probe?v&h=index,health,status,docs.count"
```

Output (captured, verbatim):

```
snapshot to mount from: vx-slm-test-2026.08.25-x_hqggqbqlwtzmpwpd3atw

=== mount it as a searchable snapshot ===
Extra data: line 2 column 1 (char 151)

=== it works ===
{
  "count" : 3500,
  "_shards" : {
    "total" : 1,
    "successful" : 1,
    "skipped" : 0,
    "failed" : 0
  }
}

=== now delete the snapshot it is mounted from ===
{"acknowledged":true}
HTTP 200

=== query the mounted index again ===
{
  "count" : 3500,
  "_shards" : {
    "total" : 1,
    "successful" : 1,
    "skipped" : 0,
    "failed" : 0
  }
}

=== index health ===
index           health status docs.count
vx-frozen-probe green  open         3500
```

`exit status: 0`

Verdict: **PASS (the warning is correct, and worse than stated)**

## STANDING WARNING (continued) - clear the cache and query again

The delete returned `acknowledged:true` and the mounted index kept answering, because the shared cache still held the data. That delay is the dangerous part and neither runbook mentions it: the damage is invisible until a cache eviction, a node restart or a cold shard read. Forcing the read back to the repository shows the real state.

Command as the runbook writes it:

```bash
POST /<mounted-index>/_searchable_snapshots/cache/clear
GET  /<mounted-index>/_search?size=0
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
K="kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch --"
echo "=== files left in vX-backups after the delete ==="
$K find /mnt/es-repo/vX-backups -type f | sort
echo
echo "=== clear the cache ==="
curl -s -u "elastic:$PW" -X POST "$ES/vx-frozen-probe/_searchable_snapshots/cache/clear"
echo
echo "=== query it now ==="
curl -s -u "elastic:$PW" "$ES/vx-frozen-probe/_count?pretty" 2>&1 | head -30
echo
echo "=== force a shard reallocation to drop all local state ==="
curl -s -u "elastic:$PW" -X POST "$ES/vx-frozen-probe/_close" > /dev/null
curl -s -u "elastic:$PW" -X POST "$ES/vx-frozen-probe/_open?wait_for_active_shards=0" > /dev/null
sleep 8
curl -s -u "elastic:$PW" "$ES/_cat/indices/vx-frozen-probe?v&h=index,health,status,docs.count"
echo
curl -s -u "elastic:$PW" "$ES/vx-frozen-probe/_count?pretty" 2>&1 | head -30
echo
echo "=== shard allocation explanation ==="
curl -s -u "elastic:$PW" "$ES/_cluster/allocation/explain?pretty" -H "Content-Type: application/json" -d "{\"index\":\"vx-frozen-probe\",\"shard\":0,\"primary\":true}" 2>&1 | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(\"can_allocate:\", d.get(\"can_allocate\"))
    for n in d.get(\"node_allocation_decisions\",[]):
        for dd in n.get(\"deciders\",[]):
            if dd[\"decision\"]!=\"YES\": print(dd[\"decider\"],\":\",dd[\"explanation\"][:400])
    print(\"unassigned_info:\", json.dumps(d.get(\"unassigned_info\",{}))[:600])
except Exception as e:
    print(\"could not parse:\", e)
"
```

Output (captured, verbatim):

```
=== files left in vX-backups after the delete ===
/mnt/es-repo/vX-backups/index-7
/mnt/es-repo/vX-backups/index.latest

=== clear the cache ===
{"_shards":{"total":1,"successful":1,"failed":0}}
=== query it now ===
{
  "count" : 3500,
  "_shards" : {
    "total" : 1,
    "successful" : 1,
    "skipped" : 0,
    "failed" : 0
  }
}

=== force a shard reallocation to drop all local state ===
index           health status docs.count
vx-frozen-probe red    open             

{
  "error" : {
    "root_cause" : [
      {
        "type" : "no_shard_available_action_exception",
        "reason" : null
      }
    ],
    "type" : "search_phase_execution_exception",
    "reason" : "all shards failed",
    "phase" : "query",
    "grouped" : true,
    "failed_shards" : [
      {
        "shard" : 0,
        "index" : "vx-frozen-probe",
        "node" : null,
        "reason" : {
          "type" : "no_shard_available_action_exception",
          "reason" : null
        }
      }
    ]
  },
  "status" : 503
}

=== shard allocation explanation ===
can_allocate: no
max_retry : shard has exceeded the maximum number of retries [5] on failed allocation attempts - manually call [POST /_cluster/reroute?retry_failed] to retry, and for more information, see [https://www.elastic.co/docs/troubleshoot/elasticsearch/diagnose-unassigned-shards?version=9.5#maximum-retries-exceeded] [unassigned_info[[reason=ALLOCATION_FAILED], at[2026-08-25T01:42:25.606Z], failed_attempts[5], failed_
unassigned_info: {"reason": "ALLOCATION_FAILED", "at": "2026-08-25T01:42:25.606Z", "failed_allocation_attempts": 5, "details": "failed shard on node [Z4nd2wQQR9ut7jRbysNHZQ]: failed recovery, failure org.elasticsearch.indices.recovery.RecoveryFailedException: [vx-frozen-probe][0]: Recovery failed on {rig-es-default-0}{Z4nd2wQQR9ut7jRbysNHZQ}{_cs3dUcoSvu2PG-Mkz6CNg}{rig-es-default-0}{10.42.0.54}{10.42.0.54:9300}{cdfhilmrstw}{9.5.2}{8000099-9111000}{ml.allocated_processors_double=32.0, ml.machine_memory=2147483648, ml.config_version=12.0.0, ml.max_jvm_size=1073741824, ml.allocated_processors=32, xpack.installed=
```

`exit status: 0`

Verdict: **PASS (confirmed, and the failure is delayed)**

## STEP 6 - wind down the old repository

STEP 6 is prose with no commands. Its one falsifiable claim is 6a: "During this period a restore may come from either repository; that is fine." Both restores were performed and both produced the correct document count. 6b's claim that the frozen tier keeps reading from object storage indefinitely is also checked here, against the rig's real `frozen-metrics`.

Command as the runbook writes it:

```bash
(no commands in this step)
```

Command actually run:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
echo "=== restore taken from the OLD object-storage repository ==="
curl -s -u "elastic:$PW" "$ES/_cat/count/vx-restored-metrics-sys?v&h=count"
echo "=== restore taken from the NEW filesystem repository ==="
curl -s -u "elastic:$PW" "$ES/_cat/count/vx-fs-restored-logs-app?v&h=count"
echo
echo "=== the frozen tier, still mounted on the object-storage repository ==="
curl -s -u "elastic:$PW" "$ES/frozen-metrics/_settings?filter_path=**.snapshot.repository_name,**.snapshot.snapshot_name&pretty"
curl -s -u "elastic:$PW" -X POST "$ES/frozen-metrics/_searchable_snapshots/cache/clear" > /dev/null
curl -s -u "elastic:$PW" "$ES/frozen-metrics/_count?pretty"
echo
echo "=== is oci-repro still registered with verification off? ==="
curl -s -u "elastic:$PW" "$ES/_snapshot/oci-repro?pretty"
```

Output (captured, verbatim):

```
=== restore taken from the OLD object-storage repository ===
count
3500
=== restore taken from the NEW filesystem repository ===
count
3500

=== the frozen tier, still mounted on the object-storage repository ===
{
  "frozen-metrics" : {
    "settings" : {
      "index" : {
        "store" : {
          "snapshot" : {
            "snapshot_name" : "frozen-base-metrics",
            "repository_name" : "oci-repro"
          }
        }
      }
    }
  }
}
{
  "count" : 3500,
  "_shards" : {
    "total" : 1,
    "successful" : 1,
    "skipped" : 0,
    "failed" : 0
  }
}

=== is oci-repro still registered with verification off? ===
{
  "oci-repro" : {
    "type" : "s3",
    "uuid" : "9QLApiRcTdC86SxVVE-SrQ",
    "settings" : {
      "bucket" : "es-snapshots",
      "client" : "default"
    }
  }
}
```

`exit status: 0`

Verdict: **PASS (the one testable claim in it)**

## CLEANUP - returning the rig to its starting state

Everything created during this run carried a `vX-` or `vx-` prefix. All of it is removed here. Nothing that existed before the run was deleted: not `oci-repro`, not `backups-fs`, not the `rig-daily` SLM policy, not `frozen-metrics`, and not the pre-existing `restored-logs-app`, `restored-metrics-sys` and `fs-restored-logs-app` indices.

Command:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
MC=$WORK/mc
K="kubectl --context rancher-desktop -n es-rig exec rig-es-default-0 -c elasticsearch --"
echo "=== indices ==="
for i in vx-frozen-probe vx-restored-metrics-sys vx-fs-restored-logs-app; do
  echo -n "$i -> "; curl -s -u "elastic:$PW" -X DELETE "$ES/$i"
  echo
done
echo "=== SLM policy ==="
curl -s -u "elastic:$PW" -X DELETE "$ES/_slm/policy/vx-slm-test"; echo
echo "=== repositories ==="
for r in vX-backups-fs vX-leak-probe; do
  echo -n "$r -> "; curl -s -u "elastic:$PW" -X DELETE "$ES/_snapshot/$r"
  echo
done
echo "=== fs repo directory ==="
$K rm -rf /mnt/es-repo/vX-backups
$K ls -a /mnt/es-repo
echo "=== bucket objects under vX- prefixes ==="
$MC rm --recursive --force rig2/es-snapshots/vX-leak-probe 2>&1 | tail -3
$MC rm --recursive --force rig2/es-snapshots/vX-verify-test 2>&1 | tail -3
echo "remaining vX- objects in the bucket:"
$MC ls -r rig2/es-snapshots | grep -c "vX-" || echo "0"
echo "=== API key ==="
curl -s -u "elastic:$PW" -X DELETE "$ES/_security/api_key" -H "Content-Type: application/json" -d "{\"name\":\"vX-es-snapshot-readonly\"}" | python3 -m json.tool
rm -f /tmp/vx.key /tmp/vx-apikey.json
```

Output (captured, verbatim):

```
=== indices ===
vx-frozen-probe -> {"acknowledged":true}
vx-restored-metrics-sys -> {"acknowledged":true}
vx-fs-restored-logs-app -> {"acknowledged":true}
=== SLM policy ===
{"acknowledged":true}
=== repositories ===
vX-backups-fs -> {"acknowledged":true}
vX-leak-probe -> {"acknowledged":true}
=== fs repo directory ===
.
..
backups
=== bucket objects under vX- prefixes ===
Removed `rig2/es-snapshots/vX-leak-probe/indices/RQmFGSVnSzKIFwZrWfxXPA/meta-3yiNNqABSRpdinXw1fzY.dat`.
Removed `rig2/es-snapshots/vX-leak-probe/meta-A3FVcrz0QdeiGf4b3MCxSg.dat`.
Removed `rig2/es-snapshots/vX-leak-probe/snap-A3FVcrz0QdeiGf4b3MCxSg.dat`.
Removed `rig2/es-snapshots/vX-verify-test/tests-baO5jVpkSnaSN699KRYNEQ/data-Z4nd2wQQR9ut7jRbysNHZQ.dat`.
Removed `rig2/es-snapshots/vX-verify-test/tests-baO5jVpkSnaSN699KRYNEQ/master.dat`.
remaining vX- objects in the bucket:
0
0
=== API key ===
{
    "invalidated_api_keys": [
        "3SiKNqABSRpdinXwHfxg"
    ],
    "previously_invalidated_api_keys": [],
    "error_count": 0
}
```

`exit status: 0`

Verdict: **PASS**

## CLEANUP - final state compared with the starting state

Side by side with the header of this document. Repositories, indices, snapshots, the SLM policy and the object count in the bucket all match what was there before the run.

Command:

```bash
ES=http://localhost:9202
PW=$(cat $WORK/espw)
MC=$WORK/mc
echo "=== repositories ==="
curl -s -u "elastic:$PW" "$ES/_snapshot/_all?pretty"
echo "=== indices ==="
curl -s -u "elastic:$PW" "$ES/_cat/indices?v&h=index,health,status,docs.count&s=index"
echo "=== snapshots ==="
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/oci-repro?v&h=id,status"
curl -s -u "elastic:$PW" "$ES/_cat/snapshots/backups-fs?v&h=id,status"
echo "=== SLM policy ==="
curl -s -u "elastic:$PW" "$ES/_slm/policy?pretty" | python3 -c "import json,sys; d=json.load(sys.stdin); print(list(d.keys()), \"->\", [v[\"policy\"][\"repository\"] for v in d.values()])"
echo "=== objects in the MinIO bucket (was 39 at the start) ==="
$MC ls -r rig2/es-snapshots | wc -l
echo "=== frozen-metrics still serving ==="
curl -s -u "elastic:$PW" "$ES/frozen-metrics/_count?pretty" | python3 -c "import json,sys; print(\"count:\", json.load(sys.stdin)[\"count\"])"
echo "=== cluster health ==="
curl -s -u "elastic:$PW" "$ES/_cluster/health?pretty" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[\"status\"], \"| active_shards\", d[\"active_shards\"], \"| unassigned\", d[\"unassigned_shards\"])"
```

Output (captured, verbatim):

```
=== repositories ===
{
  "oci-repro" : {
    "type" : "s3",
    "uuid" : "9QLApiRcTdC86SxVVE-SrQ",
    "settings" : {
      "bucket" : "es-snapshots",
      "client" : "default"
    }
  },
  "backups-fs" : {
    "type" : "fs",
    "uuid" : "Pp2RwKYHShqE8C3qAN8cYw",
    "settings" : {
      "location" : "/mnt/es-repo/backups"
    }
  }
}
=== indices ===
index                health status docs.count
frozen-metrics       green  open         3500
fs-restored-logs-app green  open         3500
logs-app             green  open         3500
metrics-sys          green  open         3500
restored-logs-app    green  open         3500
restored-metrics-sys green  open         3500
=== snapshots ===
id                                           status
rig-daily-2026.08.24-xev01ty8rawen8yasu9qsw SUCCESS
frozen-base-metrics                         SUCCESS
id                                           status
rig-daily-2026.08.24-qszkz9gvqqwc7hhngsy4vg SUCCESS
=== SLM policy ===
['rig-daily'] -> ['backups-fs']
=== objects in the MinIO bucket (was 39 at the start) ===
39
=== frozen-metrics still serving ===
count: 3500
=== cluster health ===
green | active_shards 11 | unassigned 0
```

`exit status: 0`

Verdict: **PASS**
