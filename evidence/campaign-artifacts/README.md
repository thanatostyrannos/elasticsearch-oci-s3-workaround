# Campaign artifacts

Raw output from campaigns run against the live rig, the local test lab that
reproduces the fault (defined in [FACTS.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/FACTS.md#the-test-lab-henceforth-the-rig)). Kept as it came back. These are the
files the numbers in [campaign-data.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/campaign-data.md) and
`test-results.md` (removed with the retired sweepers; in git history before `9a149a8`) are read off. Nothing here is summarised
and nothing has been reformatted.

> [!WARNING]
> **These are transcripts of the RETIRED sweepers.** `s3_repo_sweeper.py`,
> `oci_repo_sweeper.py` and `es_log_driven_sweeper.py` decided what to delete by
> absence from a live set they computed themselves, so a failed read resolved
> toward deleting. A reviewer later drove one of them along its documented path
> to a real delete of a live segment blob, and they were removed.
>
> Read the commands here as history. Do not run them. The replacement,
> [`generation_chain`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/generation_chain/README.md), cannot delete at all:
> its HTTP layer allows GET and HEAD and nothing else.

The `dN` prefix is the day of the campaign the file came from.

| File | Lines | What it is |
|---|---|---|
| `campaign1-orphans.tsv` | 29 | The first campaign's orphan set. Key, size, created, last modified, last accessed, and the reason each was classified. The dates are what made "last accessed long before the snapshot was deleted" checkable rather than asserted. |
| `d1-sizing.txt` | 93 | The repository as it stood at the start: what the audit tool reported for size and object counts before anything was touched. The baseline every later delta is measured against. |
| `d1-classified.tsv` | 4 | Snapshots split into the two populations that share one repository: SLM backups against frozen-tier searchable-snapshot mounts. Carries policy, tier, which index mounts it, state and start time. Conflating these two is the mistake the classification exists to prevent. |
| `d3-snap1.txt` | 1 | The name of the filesystem-repository backup taken before the destructive steps, so there was a known-good restore point that did not live in the bucket under test. |
| `d3b-unlink-proof.txt` | 1 | File count in a shard directory after a delete. The proof that a delete through this path removed something rather than being acknowledged and ignored. |
| `d3c-standing.txt` | 1 | The standing backup still present after the day's work, confirming the restore point survived what was done around it. |
| `d7-mounted.txt` | 1 | The mounted searchable-snapshot index, its snapshot uuid, that it is a partial mount, and which index it backs. This is the linkage that lives only in cluster state: nothing in the bucket says a snapshot is load bearing for a mounted index, and nothing stops you deleting one that is. |
| `d8-log-manifest.tsv` | 54 | The log-driven condemnation set: keys the retired sweeper named from Elasticsearch's own delete logs, with sizes, dates, and when each key was first and last seen in the logs. |
| `d8-summary.txt` | 21 | The run that produced the manifest above. Opens with the tool warning that `--prefix '/'` means the whole bucket is being treated as the repository, which is exactly the shape of over-broad scope this project now refuses rather than warns about. |
| `d8b-applied.txt` | 1 | The result of applying that manifest: 53 deleted, 0 already gone. The one file here that records objects actually being removed. |
| `d9-catalog.txt` | 2 | The snapshot catalog after the deletes, showing the surviving snapshot still SUCCESS. Cheap, and the first thing to check when something has been removed underneath a repository. |
| `d9-residual-orphans.tsv` | 5 | What the audit still named after the sweep. A non-empty residual is the honest outcome: the sweep removed what it could attribute and left what it could not. |
| `d10-count.txt` | 1 | Document count after restore: 3500. |
| `d10-restore.txt` | 1 | The restore itself: 1 shard, 1 successful, 0 failed. Together with the count above, this is the end-to-end proof that what survived the campaign still restores, which is the only test of a snapshot repository that actually matters. |

## Reading these

The pairing that carries the most weight is `d10-restore.txt` with
`d10-count.txt`. A repository can list clean, report SUCCESS on every snapshot
and pass an integrity check while being unrestorable, and this project has
measured exactly that: a mounted index with all its data blobs deleted returned
HTTP 200 with `"failed": 0`. A restore that returns the documents is the only
check none of those failure modes survives.

`d9-residual-orphans.tsv` is worth reading next to `d8b-applied.txt`. Fifty-three
objects removed and a residual left behind is the correct shape for a tool that
condemns only what it can attribute. A sweep that reported zero residual would
mean it had accounted for everything in the bucket, which is a stronger claim
than any of this evidence supports.
