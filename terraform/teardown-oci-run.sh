#!/usr/bin/env bash
# Take down everything an OCI test run created, in the order that works, and
# then check that nothing is left.
#
# A run is spin up, run, tear down. That only holds if teardown is one command
# nobody has to remember the steps of, and if it ends by checking rather than
# asserting. An enhanced OKE cluster costs $0.10 an hour whether or not
# anything runs on it, so a forgotten cluster is the expensive mistake this
# exists to prevent.
#
# In order:
#   1. Refuse while a rig process of yours is still running. A generator that
#      outlives teardown keeps writing into a repository that is being removed.
#   2. With arguments after --, run `snapshot_churn_rig.py teardown` with them,
#      which restores the cluster settings the rig changed and unregisters its
#      repository. Every rig option is available there, and so is every refusal
#      the rig makes on its own.
#   3. terraform destroy in oke-test-cluster/, then in oci-probe/, for each
#      module that has state.
#   4. Count what terraform still tracks, and list any OKE cluster still up in
#      the compartment.
#
# Safe to run twice. Every step tolerates its target already being gone.
#
# Usage:
#   terraform/teardown-oci-run.sh [--yes] [--compartment OCID] \
#       [-- <snapshot_churn_rig.py teardown arguments>]
#
# Example, for a rig started with --prefix leaktest:
#   terraform/teardown-oci-run.sh -- \
#       --es https://localhost:9200 --password-file ./espw --ca-cert ./ca.crt \
#       --prefix leaktest --state-file ./rig-state.json \
#       --repo-type s3 --bucket es-leak-test --base-path leaktest
#
# Exit status: 0 when terraform tracks nothing afterwards, 1 when something is
# left, 2 on a usage error or a refusal.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ASSUME_YES=""
COMPARTMENT=""
RIG_ARGS=()

usage() {
  sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=1; shift ;;
    --compartment)
      [ $# -ge 2 ] || { echo "--compartment needs an OCID" >&2; exit 2; }
      COMPARTMENT="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    --) shift; RIG_ARGS=("$@"); break ;;
    *) echo "unknown argument: $1 (rig options go after --)" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s ==\n' "$1"; }
indent() { while IFS= read -r line; do printf '%s%s\n' "$1" "$line"; done; }

say "checking for rig processes still running"
# Listed, never killed. Matching on the whole command line has more than once
# caught the shell running a teardown, because a wrapper's `bash -c "..."`
# string contains the same names. So this looks only at the program each
# process is running: the script an interpreter was handed, or the script
# itself. It skips this process and every ancestor of it.
running=$(python3 - "$$" <<'PY'
import os, sys
TOOLS = {"snapshot_churn_rig.py": "run", "reclaim_test_protocol.py": None,
         "run-test-cycle.sh": None}
uid = os.getuid()
skip, pid = set(), int(sys.argv[1])
while pid > 1:
    skip.add(pid)
    try:
        with open(f"/proc/{pid}/stat") as fh:
            pid = int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        break
if not os.path.isdir("/proc/self"):
    print("  no /proc here, so running rig processes cannot be checked;"
          " make sure the rig is stopped", file=sys.stderr)
    sys.exit()
for entry in os.listdir("/proc"):
    if not entry.isdigit() or int(entry) in skip:
        continue
    try:
        if os.stat(f"/proc/{entry}").st_uid != uid:
            continue
        with open(f"/proc/{entry}/cmdline", "rb") as fh:
            argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        continue
    if not argv:
        continue
    names = [os.path.basename(argv[0])]
    if len(argv) > 1 and names[0].startswith(("python", "bash", "sh")):
        names.append(os.path.basename(argv[1]))
    for i, name in enumerate(names):
        if name in TOOLS:
            need = TOOLS[name]
            if need is None or need in argv[i + 1:i + 2]:
                print(entry, " ".join(argv))
            break
PY
)
if [ -n "$running" ]; then
  indent "  " <<<"$running"
  echo "Stop these first (kill <pid>), then run this again. Nothing was done." >&2
  exit 2
fi
echo "  none"

modules=()
for module in oke-test-cluster oci-probe; do
  [ -f "$HERE/$module/terraform.tfstate" ] && modules+=("$module")
done

if [ -z "$ASSUME_YES" ]; then
  echo
  [ ${#RIG_ARGS[@]} -gt 0 ] \
    && echo "Will run: snapshot_churn_rig.py teardown ${RIG_ARGS[*]}"
  if [ ${#modules[@]} -gt 0 ]; then
    echo "Will terraform destroy: ${modules[*]}"
    echo "That removes the OKE cluster and its network, and the buckets, user"
    echo "and keys, for whichever of those modules has state."
  fi
  if [ ${#RIG_ARGS[@]} -eq 0 ] && [ ${#modules[@]} -eq 0 ]; then
    echo "No rig arguments and no terraform state: nothing to destroy."
  fi
  read -r -p "Type yes to continue: " reply
  [ "$reply" = "yes" ] || { echo "nothing done"; exit 2; }
fi

say "restoring the Elasticsearch cluster and unregistering the repository"
if [ ${#RIG_ARGS[@]} -gt 0 ]; then
  ( cd "$ROOT" && python3 snapshot_churn_rig.py teardown "${RIG_ARGS[@]}" )
  rc=$?
  [ "$rc" -eq 0 ] || echo "  rig teardown exited $rc; its output above says why"
else
  echo "  no rig arguments after --; skipped"
fi

if [ ${#modules[@]} -gt 0 ] && ! command -v terraform >/dev/null 2>&1; then
  echo "terraform is not on PATH, and these modules still have state: ${modules[*]}" >&2
  exit 2
fi

say "destroying the OKE cluster and its network"
if [[ " ${modules[*]:-} " == *" oke-test-cluster "* ]]; then
  ( cd "$HERE/oke-test-cluster" \
    && terraform destroy -input=false -auto-approve -no-color 2>&1 | tail -4 )
else
  echo "  no state; this module created nothing"
  echo "  A cluster built in the console is in no state file and is not"
  echo "  destroyed here: oci ce cluster delete --cluster-id <ocid> --force"
fi

say "destroying the buckets, user and keys"
if [[ " ${modules[*]:-} " == *" oci-probe "* ]]; then
  ( cd "$HERE/oci-probe" \
    && terraform destroy -input=false -auto-approve -no-color 2>&1 | tail -4 )
else
  echo "  no state; nothing to destroy"
fi

say "verifying"
# Asserting a teardown worked is not the same as checking. This checks.
LEFT=0
for module in oke-test-cluster oci-probe; do
  state="$HERE/$module/terraform.tfstate"
  [ -f "$state" ] || continue
  count=$(python3 -c '
import json, sys
try:
    print(len(json.load(open(sys.argv[1])).get("resources", [])))
except (OSError, ValueError):
    print(-1)' "$state")
  if [ "$count" -lt 0 ]; then
    echo "  $module: state file unreadable; inspect it by hand"
    LEFT=$((LEFT + 1))
  else
    echo "  $module: $count resource(s) left in state"
    LEFT=$((LEFT + count))
  fi
done

if command -v oci >/dev/null 2>&1; then
  if [ -z "$COMPARTMENT" ]; then
    COMPARTMENT=$(python3 -c '
import configparser, os
c = configparser.ConfigParser()
c.read(os.path.expanduser("~/.oci/config"))
print(c["DEFAULT"].get("tenancy", "") if "DEFAULT" in c else "")' 2>/dev/null)
  fi
  if [ -n "$COMPARTMENT" ]; then
    up=$(oci ce cluster list -c "$COMPARTMENT" --all 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin).get("data") or []
except ValueError:
    rows = []
for k in rows:
    if k.get("lifecycle-state") not in ("DELETED", "DELETING"):
        print(k.get("name"), k.get("lifecycle-state"))')
    if [ -n "$up" ]; then
      echo "  OKE clusters still up in the compartment, not all necessarily"
      echo "  from this run:"
      indent "    " <<<"$up"
    else
      echo "  no OKE cluster up in the compartment"
    fi
  fi
else
  echo "  oci CLI not found; OKE clusters outside terraform state not checked"
fi

if [ "$LEFT" -eq 0 ]; then
  echo "  terraform state is empty"
  say "done"
  exit 0
fi
echo "  WARNING: $LEFT resource(s) still tracked. Rerun, or inspect by hand."
exit 1
