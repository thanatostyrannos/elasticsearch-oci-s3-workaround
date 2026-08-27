# Blast-radius re-measurement

Every number in `REPORT.md` came out of a script in `harness/` and left a file
in `artifacts/`. Re-run the campaign with:

```bash
./harness/run_all.sh
```

and remove everything it created with:

```bash
./harness/cleanup.sh
```

## What it does

`run_all.sh` builds four base repositories on MinIO through Elasticsearch, then
runs one experiment per class of object. Each experiment copies a base
repository byte for byte onto its own prefix, registers that copy as a separate
repository, removes one object from the store with an S3 `DELETE`, and then
records what an operator would see: the listing before and after, the raw
`_verify_integrity` response, the catalog, the restore of every snapshot, and
the document count of every restored index.

Nothing is simulated. The object leaves the bucket.

| Base | Shape |
|---|---|
| `base-s` | two indices, three snapshots, six index-snapshot pairs, one index written once and never touched |
| `base-p` | one index of 2,000 documents, one snapshot, nine objects |
| `base-g` | one snapshot taken with global cluster state |
| `base-ms` | one index, one snapshot, built to be mounted as a searchable snapshot |

## Environment

Elasticsearch 9.5.2 under ECK in namespace `es-rig` on the `rancher-desktop`
context, one node, 2 GB heap ceiling, a licence tier permitting searchable snapshots. Repositories are type
`s3` against MinIO pinned at `RELEASE.2025-01-18T00-31-37Z` in the same
namespace. Registration passes `?verify=false` because MinIO rejects the batch
delete repository verification uses.

## Hygiene

Everything this campaign creates carries a prefix it owns: bucket `blastrm`,
repositories `blast-*`, indices `blast-*`, restored indices `bxr*`, mounted
indices `bxms*`, MinIO port forward on 19045. `cleanup.sh` removes exactly
those. It touches nothing in `es-snapshots` and no repository or index another
agent on this rig created.
