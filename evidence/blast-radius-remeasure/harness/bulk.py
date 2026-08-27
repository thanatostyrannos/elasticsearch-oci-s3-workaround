#!/usr/bin/env python3
"""Generate a deterministic NDJSON bulk body. Deterministic so a re-run of the
campaign builds byte-comparable segments rather than merely similar ones."""
import sys
import random

index, start, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rnd = random.Random(20260825 + start)
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
         "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa"]
out = []
for i in range(start, start + count):
    body = " ".join(rnd.choice(WORDS) for _ in range(24))
    out.append('{"index":{"_index":"%s","_id":"%d"}}' % (index, i))
    out.append('{"seq":%d,"body":"%s","tag":"%s"}' % (i, body, rnd.choice(WORDS)))
sys.stdout.write("\n".join(out) + "\n")
