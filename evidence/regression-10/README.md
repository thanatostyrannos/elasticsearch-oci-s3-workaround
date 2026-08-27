# Ten cycles after a week of changes, 2026-08-26

A regression run. The tool had gained batch reclaiming, size reporting,
per-disposition sizing and a reworked report since the last campaign, and the
question was whether any of that broke the thing it exists to do.

Ten audit-then-delete cycles against a live MinIO repository, with the load
generator writing and SLM snapshotting every 30 seconds throughout. Nothing was
paused.

## Result

**31,534 objects deleted. Zero failed. Zero unconfirmed. In every cycle.**

| cycle | manifest | deleted | failed | unconfirmed | objects before → after |
|---|---|---|---|---|---|
| 1 | 30,851 | 30,849 | 0 | 0 | 193,022 → 164,235 |
| 2 | 2 | 0 | 0 | 0 | 164,770 → 164,804 |
| 3 | 2 | 0 | 0 | 0 | 165,237 → 165,237 |
| 4 | 2 | 0 | 0 | 0 | 165,796 → 166,297 |
| 5 | 2 | 0 | 0 | 0 | 166,820 → 166,820 |
| 6 | 96 | 94 | 0 | 0 | 167,353 → 167,287 |
| 7 | 159 | 157 | 0 | 0 | 168,050 → 168,322 |
| 8 | 2 | 0 | 0 | 0 | 168,801 → 168,801 |
| 9 | 216 | 214 | 0 | 0 | 169,446 → 169,696 |
| 10 | 222 | 220 | 0 | 0 | 170,251 → 170,089 |

A manifest of 2 is a header and a completion marker, so those cycles condemned
nothing. That is the correct answer rather than a failure, and the reason is
below.

## Nothing was broken

The restore check, which is the only one that survives the others passing:

```
integrity   http=200  anomalies=0
restore     3 of 3 shards, 109,000 documents
            INTACT
```

## Why most cycles found nothing

Two reasons, both the tool refusing rather than failing.

**Retention had not expired anything yet.** SLM keeps snapshots for five
minutes, and cycles ran about forty seconds apart. A cycle that runs between
expiries has no delete to attribute anything to, so it condemns nothing. Cycle
1 found 30,849 because the repository had been accumulating since the previous
campaign.

**The shard directories were mid-write.** At a 30 second snapshot cadence with
a generator running, some snapshot is nearly always in flight, so a shard
directory declares more shards than the run has read. The completeness guard
drops that directory whole rather than computing a set difference over a
partial view. Across all ten cycles **not one segment blob was condemned**, and
every object deleted was metadata.

That is the safety property working. An incomplete view produces a smaller
manifest, never a wrong one.

## What each file is

| File | What it holds |
|---|---|
| `cycles.tsv` | One row per cycle: manifest size, deleted, failed, unconfirmed, object count before and after, reclaimable, exit code |
| `derive-N.txt` | The audit report for cycle N: coverage, dispositions with sizes, reclaimable |
| `dry-N.txt` | The dry run: what would be sent, and the approval it would need |
| `exec-N.txt` | The execute pass: deleted, failed, unconfirmed |
| `intact-N.txt` | The restore check, run every third cycle |

## What the run also found

Three defects in the checker rather than in the tool, all fixed in the same
change that produced this evidence.

A cluster-wide RED gate failed on ten unassigned shards belonging to two
indices left by an unrelated campaign months earlier. It now names what is
unhealthy and fails only if those indices appear in the repository under test.

`IN_PROGRESS` was treated as an unexpected state, which on a live cluster is
always true, so this check had never once completed against a moving
repository. `PARTIAL` was accepted when it means shards failed.
