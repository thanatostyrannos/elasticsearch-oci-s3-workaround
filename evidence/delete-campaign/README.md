# Delete campaign, 2026-08-26

Twelve reclaim cycles run back to back by `generation_chain` against a live,
moving repository. The load generator kept writing, ILM kept rolling and SLM
kept snapshotting throughout. Nothing was paused, because orphans created
during a run are caught by the next one.

This is evidence of the **current** tool. The retired sweepers produced none of
it.

The summary table and the interpretation are in
[FACTS.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/FACTS.md#campaign-results-2026-08-26). This directory is the
raw output those numbers are read off.

## What each file is

| File | What it holds |
|---|---|
| `cycles.tsv` | One row per cycle: timestamp, manifest size, keys deleted, failed, absent, object count before and after, exit code. The `deleted` column reads 0 for the first twelve rows, which is a defect in the loop's scoreboard, not the tool. Take deleted counts from `exec-N.txt`. |
| `dry-N.txt` | The dry run for cycle N. Reports the manifest path, its key count and its sha256. Sends nothing. |
| `exec-N.txt` | The execute pass for cycle N. Carries the authoritative `deleted`, `failed` and `unconfirmed` counts. |
| `intact-N.txt` | The did-we-break-Elasticsearch check after cycle N: cluster health, snapshot states, `_verify_integrity`, then a real restore that counts documents. |

## Reading it

**The manifest count is one higher than the delete count** in every cycle. The
manifest file carries a header row and the count is a line count. Nothing is
being skipped; `dry-N.txt` reports the true key count.

**`unconfirmed` is the column that matters most.** Every cycle re-reads the keys
it deleted and counts the ones the store will not confirm gone. It is zero
everywhere.

**Cycle 12's object count rose**, from 459,624 to 466,715, and it is the only
cycle that did. The arithmetic, derived from `cycles.tsv`:

    cycle   window    deleted   created during   net       created/min
    4       2.5m      2474      76               -2398     30
    9       5.9m      5564      970              -4594     165
    11      8.9m      8794      1437             -7357     162
    12      19.3m     11538     18629            +7091     964

Two things combined. The rig was manufacturing objects at about 964 a minute
during that cycle, against a previous worst of 214, because SLM was snapshotting
every fifteen seconds with the load generator writing and ILM rolling
throughout. And the cycles were lengthening, from 2.5 minutes at cycle 4 to 19.3
at cycle 12, which is simply more time for the generator to run. The lengthening
is consistent with root generations accumulating and making each audit pass
costlier, which is issue #9.

Neither is the delete path being slow. Across all twelve cycles the tool removed
146,800 objects while the rig kept writing underneath it.

**`intact-12.txt` reads FAIL and did not test anything.** The checker treated an
`IN_PROGRESS` snapshot as an unexpected state and stopped, and on a live rig with
SLM running there is nearly always one. That was a defect in the checker and it
is fixed: in-flight snapshots are now excluded from the restore candidates
rather than failing the run. The capture is left as it came back. No restore was attempted, so cycle 12
has no restore result in either direction. It is kept because a check that did
not run should not be mistaken for a check that passed.

The three checks that did run, at cycles 3, 6 and 9, each restored a real
data-stream index: 24 of 24 shards, zero integrity anomalies, and 246,000,
122,000 and 184,082 documents respectively.
