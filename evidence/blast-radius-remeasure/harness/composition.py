#!/usr/bin/env python3
"""Object count and byte share by kind, which is the table behind the claim that
a byte percentage tells an operator almost nothing."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s3lib  # noqa: E402

prefix = sys.argv[1] if len(sys.argv) > 1 else "base-s"
rows = s3lib.list_all("blastrm", prefix)
tot_o, tot_b = len(rows), sum(s for _, s, _ in rows)


def kind(k):
    r = k[len(prefix) + 1:]
    if r == "index.latest":
        return "index.latest"
    if re.fullmatch(r"index-\d+", r):
        return "root index-N"
    if r.startswith("snap-"):
        return "root snap-<uuid>.dat"
    if r.startswith("meta-"):
        return "root meta-<uuid>.dat"
    if re.search(r"/\d+/__", r):
        return "__ data blob"
    if re.search(r"/\d+/index-", r):
        return "shard index-<gen>"
    if re.search(r"/\d+/snap-", r):
        return "shard snap-<uuid>.dat"
    if re.search(r"indices/[^/]+/meta-", r):
        return "index meta-<id>.dat"
    return "other"


agg = {}
for k, s, _ in rows:
    a = agg.setdefault(kind(k), [0, 0])
    a[0] += 1
    a[1] += s
print("kind\tobjects\tshare_of_objects_pct\tbytes\tshare_of_bytes_pct")
for k, (n, b) in sorted(agg.items(), key=lambda x: -x[1][1]):
    print(f"{k}\t{n}\t{round(100 * n / tot_o, 2)}\t{b}\t{round(100 * b / tot_b, 2)}")
print(f"TOTAL\t{tot_o}\t100.0\t{tot_b}\t100.0")
d = agg["__ data blob"]
print(f"__ data blobs\t{d[0]}\t{round(100 * d[0] / tot_o, 2)}\t{d[1]}\t{round(100 * d[1] / tot_b, 2)}")
print(f"everything else\t{tot_o - d[0]}\t{round(100 * (tot_o - d[0]) / tot_o, 2)}"
      f"\t{tot_b - d[1]}\t{round(100 * (tot_b - d[1]) / tot_b, 2)}")
