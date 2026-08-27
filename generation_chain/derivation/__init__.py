"""The derivation: what is live, what is garbage, and what cannot be told.

THE ALGORITHM IS ELASTICSEARCH'S OWN, and this package computes exactly it. Its
blobstore package documentation says a delete collects "all segment blobs
(identified by having the data blob prefix `__`) in the shard directory which
are not referenced by the new BlobStoreIndexShardSnapshots", then deletes them.
That is a shard-local set difference. Nothing outside the shard directory takes
part. It lives in one place, `ShardHistory.collectable` in `shards.py`, and it
is the only subtraction here.

WHAT THIS TOOL NAMES IS A SUBSET OF THAT ANSWER, and the difference is worth
being straight about. Elasticsearch deletes on ABSENCE from the current file
list. This tool condemns on PRESENCE: it names a blob only when it can also
point at the delete operation that orphaned it. A blob it leaves out is not a
blob it calls live.

Read the modules in this order.

  `chain`      which root generations this run may believe, and which is
               current. Anchors on the highest generation the LISTING shows
               that carries this repository's uuid, which is the order
               Elasticsearch's own documentation gives.
  `keys`       the store's listing, and the store's second opinion about each
               entry, three-valued so "could not answer" is never recorded as
               "does not hold".
  `identity`   whether a read returned the object it asked for. Nothing inside
               a shard document names its own shard, so identity is built from
               outside the bytes.
  `shards`     the per-shard survey: the live set, the file lists of earlier
               eras, and the completeness check against each snapshot's own
               declared extent. A shard whose evidence is incomplete is dropped
               whole and contributes nothing.
  `garbage`    the set difference, and the attribution of each member of it to
               the delete operation that should have removed it.
  `classification` a disposition for every key the store holds, and the
               manifest that follows from it. One function returns both, so
               there is no joining statement a refactor can delete.
  `audit`      one run, in that order.

Nothing in here knows what a bucket is. Everything reaches the store through
the `RepositorySource` interface, which is why the same derivation runs over a
local mirror, an S3 compatibility endpoint and OCI native.
"""
