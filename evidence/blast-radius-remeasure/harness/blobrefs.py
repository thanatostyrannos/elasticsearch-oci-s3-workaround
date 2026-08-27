#!/usr/bin/env python3
"""Which snapshots name which data blob, read out of the shard-level
snap-<uuid>.dat documents themselves.

The documents are SMILE inside Elasticsearch's DFL container. Inflating gives a
byte stream in which a blob name is a printable token, so the reference map
comes out without a SMILE decoder and without importing anything from the tool
this campaign is evidence for.
"""
import os
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s3lib  # noqa: E402

# A blob name is 24 base64url characters after the prefix. Anchoring on the
# length stops the scan from swallowing the Lucene physical name that follows.
TOKEN = re.compile(rb"(v?__[A-Za-z0-9_-]{22})")


def inflate(blob):
    i = blob.find(b"DFL\x00")
    if i < 0:
        return blob
    return zlib.decompressobj(-15).decompress(blob[i + 4:])


def names_in(blob):
    return sorted({t.decode() for t in TOKEN.findall(inflate(blob))})


def main():
    bucket, prefix = sys.argv[1], sys.argv[2]
    rows = s3lib.list_all(bucket, prefix)
    sizes = {k: s for k, s, _ in rows}
    data = {k: s for k, s in sizes.items() if re.search(r"/\d+/__", k)}

    refs = {}
    for k in sorted(sizes):
        m = re.search(r"indices/([^/]+)/(\d+)/snap-([^/]+)\.dat$", k)
        if not m:
            continue
        idx_uuid, shard, snap_uuid = m.groups()
        for n in names_in(s3lib.get(bucket, k)):
            if n.startswith("v__"):
                continue
            refs.setdefault(f"{prefix}/indices/{idx_uuid}/{shard}/{n}", []).append(snap_uuid)

    print("data_blob\tbytes\tref_count\treferencing_snapshot_uuids")
    for k in sorted(data):
        r = sorted(set(refs.get(k, [])))
        print(f"{k}\t{data[k]}\t{len(r)}\t{','.join(r) if r else '-'}")


if __name__ == "__main__":
    main()
