"""How much of one HTTP answer any client in this package will hold in memory.

Every client reads at most a cap plus one byte and refuses the answer when the
extra byte arrives, so a store, or anything in the path, cannot make the host
allocate without limit. A declared Content-Length over the cap is refused
before the first body byte is read. Each client turns `BodyTooLarge` into the
error it already raises for a read it will not retry.
"""

from __future__ import annotations

from typing import Optional

# The largest body either XML parser accepts. The biggest legitimate responses
# are a ListObjectsV2 page of 1000 keys and a DeleteObjects result for 1000
# keys. A key is at most 1024 bytes, and XML escaping can grow one by a factor
# of five (`&` becomes `&amp;`), so 1000 worst-case keys are about 5.1 MB, and
# the per-entry metadata (ETag, LastModified, Owner, error Code and Message)
# adds under 1 KB each, about 1 MB. 16 MiB is roughly 2.5 times that sum and
# stays far below the memory a hostile body can make expat spend.
MAX_XML_BODY_BYTES = 16 * 1024 * 1024

# The largest blob read: a root generation (`index-N`), a shard document, a
# snapshot document, or an object fetched by key for the reclaim identity
# check. The largest of those is a root generation. FACTS.md (the `index-N`
# fields) says it holds one entry per snapshot and, per index, the list of
# snapshots containing it. Each snapshot-in-index reference is a 22 character
# uuid plus JSON punctuation, about 30 bytes. A repository of 1000 snapshots
# over 1000 indices therefore writes about 30 MB, the "tens of MB" an
# operator of a large repository should expect. No measurement in this
# repository reaches that size: the largest generations captured on the
# test rigs are a few KB, so the 30 MB figure is derived from the format, not
# measured. 256 MiB is about eight times it. A larger cap would
# let the read-ahead pool, which holds several blobs at once, spend gigabytes
# on one hostile answer.
MAX_BLOB_BYTES = 256 * 1024 * 1024

# The largest Elasticsearch JSON answer. The biggest is `_snapshot/<repo>/_all`,
# which names every index of every snapshot (about 40 bytes of index name per
# snapshot-index pair, so about 40 MB for the 1000 by 1000 repository above)
# plus a few hundred bytes of metadata per snapshot. 128 MiB is about three
# times that. The figure is derived, not measured, for the same reason as
# MAX_BLOB_BYTES.
MAX_JSON_ANSWER_BYTES = 128 * 1024 * 1024


class BodyTooLarge(Exception):
    """The answer is larger than the cap its reader allows.

    Deliberately not a GenerationChainError. It never escapes a client: each
    client catches it and raises its own non-retried error.
    """


def read_capped(response, cap: int, what: str) -> bytes:
    """The whole body of `response`, or BodyTooLarge when it exceeds `cap`."""
    declared = _declared_length(response)
    if declared is not None and declared > cap:
        raise BodyTooLarge(
            f"{what} declares {declared} bytes, larger than the {cap} this "
            "tool reads; refused without reading it")
    body = response.read(cap + 1)
    if len(body) > cap:
        raise BodyTooLarge(
            f"{what} is larger than the {cap} bytes this tool reads; "
            "refused after reading the first "
            f"{cap + 1}")
    return body


def _declared_length(response) -> Optional[int]:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None
