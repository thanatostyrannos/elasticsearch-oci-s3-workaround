# generation_chain

Reads an Elasticsearch snapshot repository and names the objects a delete
operation should have removed and did not. It reads, and it never deletes: the
HTTP layer sets `ALLOWED_METHODS = frozenset({"GET", "HEAD"})` behind an assert,
so DELETE is unreachable rather than merely unused. What comes out is a manifest
a person reads before anything is removed.

Standard library only. No third-party dependency, at all.

## Why this exists

Elasticsearch 8.19.17 and later, and 9.5.0 and later, send
`x-amz-checksum-crc32` on the batch `DeleteObjects` call and expose no setting
to send `Content-MD5` instead. Stores that require the latter reject the
request. Elasticsearch reports the deletion as successful anyway, so the
repository accumulates objects that nothing references and nothing will ever
collect.

Measured on the rig, a local test lab reproducing the fault with Elasticsearch
9.5.2 against a pinned MinIO (see [FACTS.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/FACTS.md#the-test-lab-henceforth-the-rig)): 109 snapshot deletions reported successful, not one object
reclaimed, and Elasticsearch's own `_snapshot/<repo>/_cleanup` endpoint
returning `{"deleted_bytes":0,"deleted_blobs":0}` against a bucket where the
great majority of objects were referenced by nothing. Cleanup cannot help,
because a delete that "succeeded" has already been written out of the metadata:
the blobs are not unreferenced objects it can find, they are objects it believes
are gone.

So recovering the space needs something outside Elasticsearch deciding which
objects are safe to remove. That is this.

## The safety condition

The believed set of references must be a SUPERSET of the true set.

An extra reference means an object is kept that could have been removed, which
costs storage. A missing reference means a live object is named, someone acts on
the name, and data is destroyed with no recovery path. Those outcomes are not
comparable, so every uncertainty here resolves toward more references and a
shorter manifest.

That single asymmetry explains most of the design. A read that fails drops the
shard rather than assuming what it would have said. A document that cannot be
tied to where it was found is refused rather than trusted. A repository whose
catalog names no live snapshots is refused outright rather than treated as one
where everything is collectable.

## How it decides

The algorithm is Elasticsearch's own, quoted from its `blobstore`
package documentation: a delete collects "all segment blobs (identified by
having the data blob prefix `__`) in the shard directory which are not
referenced by the new BlobStoreIndexShardSnapshots", then deletes them. That set
difference is the only subtraction in the package.

One difference matters and it is deliberate. Elasticsearch condemns on ABSENCE
from the current file list. This condemns on PRESENCE in a deleted snapshot's
file list. So what it names is a subset of what Elasticsearch itself would
collect, and a failure to read makes the manifest shorter rather than longer.
The retired sweepers had that the other way round, which is why they were
retired.

The gates that establish completeness and identity are described in
[`docs/repository-layout-and-reachability.md`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/repository-layout-and-reachability.md),
which is the design document. If the code and that document disagree, the
document is what was agreed and the code is a bug.

## Layout

| Path | What lives there |
|---|---|
| `cli.py` | flags, exit codes, and the refusal messages an operator reads |
| `credentials.py` | secrets from a file or the standard locations, never from argv |
| `supported.py` | the format floor: which repositories this is entitled to parse |
| `model.py` | the parsed shapes and `Coverage`, which is what a run could not establish |
| `corroboration.py` | the optional Elasticsearch veto. It protects, it never condemns |
| `formats/` | decoders for the on-disk blobs, written from the format rather than imported |
| `derivation/` | the chain, the shard survey, identity, and the subtraction itself |
| `sources/` | transports (`s3`, `oci`, `local`), request signing, read-ahead, memory budget |
| `reporting/` | the manifest and the coverage report |
| `reclaim/` | reads an approved manifest and deletes exactly what it names. The only directory here with a delete path; kept out of every stage above it |

`derivation/audit.py` is the one place the stages are joined, and its `run_audit`
is the whole entry point. `reclaim/cli.py` is the other entry point, a separate
command for a separate step: it runs after a manifest from this tool has been
reviewed and approved, never before.

## Running it

```
python3 -m generation_chain \
  --transport s3 --endpoint https://<store> --region <region> \
  --bucket <bucket> --prefix <base_path> \
  --credentials creds.json \
  --elasticsearch https://<cluster>:9200 --es-repository <repo> \
  --manifest orphans.tsv
```

`--credentials` takes a PATH and never a value, because a secret in argv is
visible in `ps` to every user on the host. A credentials file other users can
read is refused rather than used.

`--self-test` proves the signing and the framing offline, with no store and no
cluster.

### Give Elasticsearch the chance to veto

`--elasticsearch` and `--es-repository` are optional and should not be. Two facts
live in cluster state and appear nowhere in the bucket, and both cost data when
missed. The first is which snapshots have searchable-snapshot indices mounted on
them: nothing stops you deleting a snapshot that backs a mounted index, and SLM
has no mount awareness, so a retention policy reaps one on schedule and the
mounted index fails at its next restart with nothing connecting the failure to
the sweep.

The veto only ever REMOVES keys from the manifest. It cannot add one, and it is
not a check on the derivation: it protects by snapshot identity, so it cannot
catch a key attributed to the wrong snapshot.

A run that asked and got no answer refuses. It never proceeds with an empty veto
standing in for a failed call.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | the run completed and wrote a manifest |
| 2 | refused for a settled reason. Retrying changes nothing |
| 3 | the invocation or a credential is wrong |
| 4 | the store or the cluster did not answer. A retry is reasonable |
| 5 | larger than this host can hold. Narrow it, or use a bigger host |

A scheduled job should distinguish 2 from 4. Retrying a settled refusal burns
the backoff to reach the same answer.

## Reading the output

The manifest is tab separated: the key, why it was named, its category, the
snapshot whose deletion orphaned it, and the generations it sat between. Every
row is checkable on its own, which is the point: a reviewer should be able to
audit a row rather than trust a count.

Read the coverage report as well as the manifest. A short manifest means either
"there is little to clean up" or "I could not see most of this repository", and
those look identical without the coverage numbers. `existence_unanswered` names
keys the store could neither confirm nor deny, reported separately from the
denials because folding them together is the one measured place where this
tool's report was wrong rather than merely conservative.

**A key absent from the manifest is not evidence that the key is live.**

## Auditing a repository that is being written

Safe, and worth saying because the instinct is to stop the writer first.

Under churn, shard documents get replaced between the listing and the fetch, and
every gate that notices reacts by dropping the shard. A moving repository yields
fewer names, never different ones. Measured against a rig snapshotting every
fifteen seconds, coverage fell to 0 percent and the manifest was correspondingly
short.

Nothing is lost. A blob orphaned while a run was reading is still orphaned when
the next run reads. Missed orphans get picked up later; a wrongly named live key
would not be recoverable at all.

So pausing SLM buys completeness, not correctness. Worth doing when you want one
long list, worth skipping when you would rather run often.

## What it cannot see

It reads the repository. Anything true only in cluster state is invisible to it
unless `--elasticsearch` is passed, and the veto's own limits are stated above.

Corroboration is common-mode with what it corroborates: Elasticsearch reads the
same object store this reads, so a change that moves what both of them see is
invisible to both. The Lucene commit point is used as an independent oracle
against exactly that, and its limit is stated where it is implemented.

None of this has been run against a real OCI bucket. No OCI endpoint is
reachable from this project's lab, which is a standing limit on every claim
here, not a gap in any one part of it.
