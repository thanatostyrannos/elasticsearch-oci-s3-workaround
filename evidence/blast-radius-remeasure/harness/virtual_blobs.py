#!/usr/bin/env python3
"""The files named in the shard metadata that have no object behind them.

Pairs each blob name in a shard document with the Lucene file name that follows
it, then asks the bucket whether an object exists for it. The presence column
comes from the store, not from a rule about which files ought to be inlined.
The length column comes from the live index's own files on the node, so the
size-threshold explanation can be tested against real bytes.
"""
import json
import os
import re
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s3lib  # noqa: E402
import esapi  # noqa: E402

KUBECTL = ["kubectl", "--context", "rancher-desktop", "-n", "es-rig", "exec",
           "rig-es-default-0", "-c", "elasticsearch", "--"]
BLOB = re.compile(rb"v?__[A-Za-z0-9_-]{22}")
LUCENE = re.compile(rb"(?:_[0-9a-z]+(?:_[A-Za-z0-9_]+)?\.[a-z]{2,4}|segments_[0-9a-z]+)")


def inflate(b):
    i = b.find(b"DFL\x00")
    return zlib.decompressobj(-15).decompress(b[i + 4:]) if i >= 0 else b


def live_file_sizes(index):
    uuid = esapi.jcall("GET", f"/{index}/_settings")[index]["settings"]["index"]["uuid"]
    path = f"/usr/share/elasticsearch/data/indices/{uuid}/0/index"
    out = subprocess.run(KUBECTL + ["ls", "-l", path], capture_output=True, text=True).stdout
    sizes = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 9:
            sizes[p[-1]] = int(p[4])
    return sizes


bucket, prefix, index = sys.argv[1], sys.argv[2], sys.argv[3]
rows = s3lib.list_all(bucket, prefix)
present = {k for k, _, _ in rows}
sizes = {k: s for k, s, _ in rows}
live = live_file_sizes(index)

pairs = {}
for k, _, _ in rows:
    m = re.search(r"indices/([^/]+)/(\d+)/snap-[^/]+\.dat$", k)
    if not m:
        continue
    idx, shard = m.groups()
    raw = inflate(s3lib.get(bucket, k))
    for mm in BLOB.finditer(raw):
        nxt = LUCENE.search(raw, mm.end(), mm.end() + 60)
        phys = nxt.group(0).decode() if nxt else "?"
        pairs[f"{prefix}/indices/{idx}/{shard}/{mm.group(0).decode()}"] = (mm.group(0).decode(), phys)

print("blob_name_in_metadata\tlucene_file\tlength_on_the_live_node\tobject_in_bucket\tobject_bytes")
for key in sorted(pairs, key=lambda k: (pairs[k][1], pairs[k][0])):
    name, phys = pairs[key]
    print(f"{name}\t{phys}\t{live.get(phys, '-')}\t{'yes' if key in present else 'no'}\t{sizes.get(key, '-')}")
