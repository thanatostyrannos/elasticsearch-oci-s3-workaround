#!/usr/bin/env python3
"""Run one delete-class experiment end to end and write every artifact behind
every number it produces.

Each experiment starts from a byte-identical clone of a base repository, so the
only difference between two experiments is which object was removed. Nothing
here simulates a delete: the object goes out of the store with an S3 DELETE.
"""
import argparse
import fnmatch
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import esapi  # noqa: E402
import s3lib  # noqa: E402

ART = os.path.join(ROOT, "artifacts")
BUCKET = "blastrm"
GLOBAL_STATE = [False]


def write_listing(path, rows):
    with open(path, "w") as fh:
        fh.write("key\tbytes\tetag\n")
        for k, s, e in rows:
            fh.write(f"{k}\t{s}\t{e}\n")


def restore_all(repo, expid, snapshots, out, global_state=False):
    """Restore every snapshot the catalog still lists, under renamed indices, and
    record what an operator would see: the restore response, the index health,
    and the document count."""
    results = []
    for sn in snapshots:
        tag = f"bxr{expid}-{sn.split('-')[-1]}"
        body = {
            "indices": "*",
            "include_global_state": global_state,
            "rename_pattern": "blast-(.+)",
            "rename_replacement": tag + "-$1",
            "include_aliases": False,
        }
        t0 = time.time()
        try:
            resp = esapi.jcall(
                "POST", f"/_snapshot/{repo}/{sn}/_restore?wait_for_completion=true",
                body, timeout=900,
            )
        except Exception as exc:  # a restore that never returns is itself a result
            resp = {"_error": repr(exc)}
        elapsed = round(time.time() - t0, 1)
        time.sleep(3)
        cat = esapi.jcall("GET", f"/_cat/indices/{tag}-*?format=json&h=index,health,status,docs.count,store.size")
        counts = {}
        for row in (cat if isinstance(cat, list) else []):
            c = esapi.jcall("GET", f"/{row['index']}/_count")
            counts[row["index"]] = c
        results.append({
            "snapshot": sn, "restore_response": resp, "elapsed_s": elapsed,
            "cat_indices": cat, "counts": counts,
        })
        # action.destructive_requires_name defaults to true on 9.x, so a
        # wildcard delete is refused and every restored index would pile up on
        # the node until it fell over. Name them.
        names = [c.get("index") for c in (cat if isinstance(cat, list) else []) if c.get("index")]
        if names:
            esapi.call("DELETE", "/" + ",".join(names))
        time.sleep(1)
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--delete", action="append", default=[])
    ap.add_argument("--delete-glob", action="append", default=[])
    ap.add_argument("--no-restore", action="store_true")
    ap.add_argument("--restore-snapshots", default="",
                    help="comma separated names to restore when the catalog can no longer list them")
    ap.add_argument("--keep", action="store_true", help="leave the clone and repo in place")
    ap.add_argument("--reregister", action="store_true",
                    help="drop and re-add the repository after the delete. Elasticsearch caches "
                         "RepositoryData in memory, so a root-level delete is invisible to a node "
                         "that already read it. Re-registering forces the fresh read a restarted "
                         "node would do.")
    ap.add_argument("--global-state", action="store_true",
                    help="restore with include_global_state true")
    args = ap.parse_args()

    GLOBAL_STATE[0] = args.global_state
    expid = args.id
    prefix = f"exp-{expid}"
    repo = f"blast-{expid}"

    esapi.call("DELETE", f"/_snapshot/{repo}")
    s3lib.purge(BUCKET, prefix)
    n = s3lib.clone_prefix(BUCKET, args.base, prefix)
    reg = esapi.jcall("PUT", f"/_snapshot/{repo}?verify=false", {
        "type": "s3",
        "settings": {"bucket": BUCKET, "client": "default", "base_path": prefix},
    })

    before = s3lib.list_all(BUCKET, prefix)
    write_listing(os.path.join(ART, f"{expid}-before.tsv"), before)
    total_objs = len(before)
    total_bytes = sum(s for _, s, _ in before)

    sizes = {k[len(prefix) + 1:]: s for k, s, _ in before}
    targets = list(args.delete)
    for g in args.delete_glob:
        targets += [k for k in sizes if fnmatch.fnmatch(k, g)]
    targets = sorted(set(targets))
    if not targets and (args.delete or args.delete_glob):
        raise SystemExit(f"no key matched for {expid}")

    verify_pre = esapi.jcall("POST", f"/_snapshot/{repo}/_verify_integrity", timeout=900)
    with open(os.path.join(ART, f"{expid}-verify-pre.json"), "w") as fh:
        json.dump(verify_pre, fh, indent=2)

    deleted = []
    for rel in targets:
        st, body = s3lib.delete(BUCKET, f"{prefix}/{rel}")
        deleted.append((rel, sizes.get(rel, -1), st))
    del_bytes = sum(b for _, b, _ in deleted)
    with open(os.path.join(ART, f"{expid}-deleted.tsv"), "w") as fh:
        fh.write("key_relative_to_base_path\tbytes\thttp_status\n")
        for rel, b, st in deleted:
            fh.write(f"{rel}\t{b}\t{st}\n")

    if args.reregister:
        esapi.call("DELETE", f"/_snapshot/{repo}")
        time.sleep(2)
        esapi.jcall("PUT", f"/_snapshot/{repo}?verify=false", {
            "type": "s3",
            "settings": {"bucket": BUCKET, "client": "default", "base_path": prefix},
        })
        time.sleep(2)

    after = s3lib.list_all(BUCKET, prefix)
    write_listing(os.path.join(ART, f"{expid}-after.tsv"), after)

    verify = esapi.jcall("POST", f"/_snapshot/{repo}/_verify_integrity", timeout=900)
    with open(os.path.join(ART, f"{expid}-verify.json"), "w") as fh:
        json.dump(verify, fh, indent=2)

    catalog = esapi.jcall("GET", f"/_snapshot/{repo}/_all")
    with open(os.path.join(ART, f"{expid}-catalog.json"), "w") as fh:
        json.dump(catalog, fh, indent=2)

    snaps = [s["snapshot"] for s in catalog.get("snapshots", [])] if isinstance(catalog, dict) else []
    if args.restore_snapshots:
        snaps = [x for x in args.restore_snapshots.split(",") if x]
    restores = []
    if not args.no_restore and snaps:
        restores = restore_all(repo, expid, snaps, os.path.join(ART, f"{expid}-restores.json"),
                               global_state=args.global_state)

    res = verify.get("results", {}) if isinstance(verify, dict) else {}
    anomalies = [e for e in (verify.get("log") or []) if "anomaly" in e]
    with open(os.path.join(ART, f"{expid}-anomalies.json"), "w") as fh:
        json.dump(anomalies, fh, indent=2)

    per_snap = {}
    pairs = set()
    for a in anomalies:
        sn = (a.get("snapshot") or {}).get("snapshot", "?")
        per_snap[sn] = per_snap.get(sn, 0) + 1
        ix = (a.get("index") or {}).get("name")
        if ix:
            pairs.add((ix, sn))

    summary = {
        "id": expid, "note": args.note, "base": args.base, "repo": repo,
        "clone_objects": n, "register": reg,
        "objects_before": total_objs, "bytes_before": total_bytes,
        "deleted_objects": len(deleted), "deleted_bytes": del_bytes,
        "share_of_objects_pct": round(100.0 * len(deleted) / total_objs, 3) if total_objs else None,
        "share_of_bytes_pct": round(100.0 * del_bytes / total_bytes, 3) if total_bytes else None,
        "objects_after": len(after),
        "verify_pre_status": (verify_pre.get("results") or {}).get("status"),
        "verify_pre_total_anomalies": (verify_pre.get("results") or {}).get("total_anomalies"),
        "verify_pre_result": (verify_pre.get("results") or {}).get("result"),
        "verify_status": res.get("status"),
        "total_anomalies": res.get("total_anomalies"),
        "result": res.get("result"),
        "anomaly_classes": sorted({a.get("anomaly") for a in anomalies}),
        "anomalies_per_snapshot": per_snap,
        "damaged_index_snapshot_pairs": sorted("/".join(p) for p in pairs),
        "damaged_pair_count": len(pairs),
        "catalog_snapshots": snaps,
        "catalog_states": {s["snapshot"]: s.get("state") for s in catalog.get("snapshots", [])} if isinstance(catalog, dict) else {},
        "restores": [
            {
                "snapshot": r["snapshot"],
                "shards": (r["restore_response"].get("snapshot") or {}).get("shards")
                          if isinstance(r["restore_response"], dict) else None,
                "indices": [c.get("index") for c in (r["cat_indices"] if isinstance(r["cat_indices"], list) else [])],
                "health": {c.get("index"): c.get("health") for c in (r["cat_indices"] if isinstance(r["cat_indices"], list) else [])},
                "docs": {k: (v.get("count") if isinstance(v, dict) else None) for k, v in r["counts"].items()},
                "count_errors": {k: (v.get("error", {}).get("type") if isinstance(v, dict) and "error" in v else None) for k, v in r["counts"].items()},
            } for r in restores
        ],
    }
    with open(os.path.join(ART, f"{expid}-summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps(summary, indent=2))

    if not args.keep:
        esapi.call("DELETE", f"/_snapshot/{repo}")
        s3lib.purge(BUCKET, prefix)


if __name__ == "__main__":
    main()
