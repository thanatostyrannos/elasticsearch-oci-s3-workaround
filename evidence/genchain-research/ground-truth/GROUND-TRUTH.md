# QA-A ground truth, issue #4   (bucket i43a-gt, MinIO RELEASE.2025-01-18T00-31-37Z, ES 9.5.2)

Method: every delete was replayed on a byte-identical **filesystem mirror** of the same
repository state (same repo uuid, same snapshot uuids, same key names). The fs repository
performs real deletes, so `comm -23 <pre> <post>` is Elasticsearch's own deletion set by
name, with no `limit(10)` truncation and no reimplementation of anything.
Mirror equality with the S3 state was asserted before every replay.

## chain/  (index i43a-a, 1 shard)   49 keys frozen

gen | snapshots                          | operation
----|------------------------------------|-------------------------------
 0  | snap-1                             | create snap-1
 1  | snap-1 snap-2                      | create snap-2
 2  | snap-1 snap-2 snap-3               | create snap-3
 3  | snap-1 snap-2 snap-3 snap-4        | create snap-4
 4  | snap-1 snap-3 snap-4               | DELETE snap-2
 5  | snap-1 snap-3                      | DELETE snap-4
 6  | snap-1 snap-3 snap-5               | create snap-5 (after forcemerge to 1 segment)
 7  | snap-5                             | DELETE snap-1 AND snap-3  (one operation, two snapshots)

Oracle removal sets: gt/oracle-op-gen3to4-del-snap2.txt (11 keys, 0 data blobs)
                     gt/oracle-op-gen4to5-del-snap4.txt (18 keys, 2 data blobs)
                     gt/oracle-op-gen6to7-del-snap1-3.txt (40 keys, 14 data blobs)
Union = 40 keys.  49 - 40 = 9 live keys (gt/live-final.txt).  Exact partition, no residue.

KEY FACT: delete snap-2 removed ZERO data blobs. Every segment snap-2 named was also
named by snap-3 and snap-4. A tool that names snap-2's blobs proposes data loss.

## share/  (index i43a-b, 1 shard)   24 keys frozen: the deliberate shared-segment case

sh-a names 6 real blobs + 4 inline (v__).
sh-b names 10 real blobs + 6 inline; its 10 are a strict superset of sh-a's 6.
DELETE sh-b removed exactly 4 data blobs (gt/oracle-share-del-shb.txt).
gt/share-must-not-name.txt holds the 6 shared blobs. Naming any of them is data loss.
One of them is `___Lf8b9osS_O6IRE4o4y4Vw`, three leading underscores.

## gap/  (index i43a-c, forcemerge before every snapshot so nothing is shared)  36 keys

gen 0..6, snapshots g-1..g-4, deletes at 3->4 (g-1), 4->5 (g-2), 5->6 (g-3).
Each delete orphans exactly 2 data blobs. Union oracle 27 keys, 9 live.

## hole/  (a byte copy of gap/ with `index-4` removed)   35 keys

Simulates a repository where one delete succeeded. Operations 3->4 and 4->5 are
unexplainable from the chain; 5->6 is explainable. A tool must SAY so.

## abort/ (index i43a-d)  16 keys: aborted snapshot residue

ab-1 SUCCESS. ab-2 started, deleted while IN_PROGRESS (recorded state=2 in index-1).
ab-2's ROOT snap-/meta- blobs exist. ab-2's SHARD document was never written.
Its partial data blob `___09cbZfgS--ofvTy9pkWcw` is in the bucket and is named by nothing.
Elasticsearch's own `_snapshot/_cleanup` never reclaims it (proved: two cleanup passes on
the mirror removed only root blobs; the data blob and two stale shard generations remain).
So a generation-chain tool has NO evidence for this key and must not name it, while the
reachability sweeper condemns it correctly.

## realfix/  (tests/fixtures/real-es952-repo.tar.gz, unpacked)   50 keys

Chain: index-0 (v9-snap-1), index-1 (v9-snap-1 + v9-snap-2), index-2 (v9-snap-2).
One delete operation, 1->2, killing v9-snap-1. Three indices: v9-guards-idx,
.snapshot-blob-cache, .security-7.

The pre-delete state was reconstructed exactly (fixture minus index-2, index.latest
rewritten to 1, which is only a pointer), pushed to a filesystem repository, and the
delete replayed. Oracle: gt/oracle-realfix-del-snap1.txt, 18 keys, of which exactly
TWO data blobs, both in v9-guards-idx. The .security-7 shard holds 16 data blobs and
loses NONE of them: every one is shared with v9-snap-2.

Elasticsearch's own `_snapshot/<repo>/_cleanup` on this fixture removes 4 blobs only
(index-0, index-1, root meta- and snap- of v9-snap-1). It never touches shard-level
leftovers. `_cleanup` is therefore not a substitute oracle; only replaying the delete is.

### Caveat on the realfix oracle: shard `index-<gen>` names are randomised per write

Replaying a delete on a *reconstructed* pre-state is exact for data blobs, root blobs
and shard `snap-<uuid>.dat`, but Elasticsearch mints a fresh random uuid for each shard
`index-<gen>` it writes, so the replay's new generation blobs differ from the ones the
real (failed) operation left in the bucket. The three shard generations that the real
operation wrote and that `index-2` references are LIVE in the frozen fixture, not orphans.
gt/oracle-realfix-corrected.txt applies that correction: 15 keys.
The chain/, share/ and gap/ oracles do not need it, because each replay was mirrored from
the actual S3 state immediately before the corresponding delete.
