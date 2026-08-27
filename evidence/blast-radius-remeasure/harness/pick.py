#!/usr/bin/env python3
"""Name one object of a given kind in a base repository, so run_all.sh does not
carry uuids that change every time the campaign is rebuilt."""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s3lib  # noqa: E402

prefix, kind = sys.argv[1], sys.argv[2]
keys = [k[len(prefix) + 1:] for k, _, _ in s3lib.list_all("blastrm", prefix)]
gens = sorted(int(k.split("-")[1]) for k in keys if re.fullmatch(r"index-\d+", k))
current = json.loads(s3lib.get("blastrm", f"{prefix}/index-{gens[-1]}"))
# The index that was written once and never touched again is the one carrying
# the shared blobs, so it is the one every per-class delete targets. It is the
# index with the fewest data blobs in its shard directory.
def blobs(u):
    return sum(1 for k in keys if k.startswith(f"indices/{u}/0/__"))

chosen = min(current["indices"].values(), key=lambda v: (blobs(v["id"]), v["id"]))
uuid, gen = chosen["id"], chosen["shard_generations"][0]

pick = {
    "root-snap": next(k for k in sorted(keys) if k.startswith("snap-")),
    "root-meta": next(k for k in sorted(keys) if k.startswith("meta-")),
    "root-gen": f"index-{gens[-1]}",
    "shard-gen": f"indices/{uuid}/0/index-{gen}",
    "shard-snap": next(k for k in sorted(keys) if re.fullmatch(rf"indices/{uuid}/0/snap-.*\.dat", k)),
    "index-meta": next(k for k in sorted(keys) if re.fullmatch(rf"indices/{uuid}/meta-.*\.dat", k)),
}[kind]
print(pick)
