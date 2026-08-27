"""`snap-<uuid>.dat`: what one snapshot says it contains.

This is the repository's own bookkeeping, and an earlier version of this
package never read it: the derivation only ever condemned these blobs by name.
A whole layer of Elasticsearch's redundancy went unused.

The document DECLARES THE SNAPSHOT'S EXTENT: which indices it holds, how many
shards each index has, how many shards succeeded, and how many bytes each
index came to. Decoded from two independently built 9.5.2 repositories:

    v9-snap-2  indices=['.snapshot-blob-cache', 'v9-guards-idx', '.security-7']
               total_shards=3 successful_shards=3
               index_details['.security-7'] = shard_count 1, size_in_bytes 108323
    gcw-s1     indices=['gcw-two'] total_shards=2 successful_shards=2
               index_details['gcw-two'] = shard_count 2, size_in_bytes 89753

`size_in_bytes` is the TOTAL for that snapshot in that index rather than an
increment. Established by measurement, not by reading a name: summing the
`length` of that snapshot's files across the CURRENT shard documents matched
the declared figure exactly on all five index and snapshot pairs available,
across both repositories, including one whose two snapshots share most of
their segments.

WHAT THIS BUYS. It turns a live set that came up SHORT from something invisible
into a contradiction. A catalog that omits an index, a `shard_generations`
array that is one entry short, a file list a decoder truncated: each of them
now disagrees with a declaration written at snapshot time by a different part
of Elasticsearch into a different object.

WHAT IT DOES NOT BUY, and the boundary is worth stating where it is
implemented. This is another object in the same bucket. It does not defend
against a tamper that adjusts the catalog and the snapshot document
consistently, so issue #21 is exactly as open as it was. What it defends
against is every failure this package has actually had: a short list, a
missing entry, a silently dropped value, a partial read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from ..errors import ShapeGateError
from .codec import unwrap


@dataclass(frozen=True)
class IndexExtent:
    shard_count: int
    size_in_bytes: Optional[int]


@dataclass(frozen=True)
class SnapshotExtent:
    """One snapshot's own statement of how large a traversal for it should be."""

    uuid: str
    name: str
    index_names: Tuple[str, ...]
    total_shards: Optional[int]
    successful_shards: Optional[int]
    by_index_name: Mapping[str, IndexExtent]

    @property
    def is_complete(self) -> bool:
        """Whether Elasticsearch itself says every shard of this snapshot took.

        A partial snapshot declares fewer successful shards than total, and
        its file lists legitimately do not cover its declared extent, so the
        caller must not read that gap as a short read.
        """
        return (self.total_shards is not None
                and self.successful_shards == self.total_shards)


def snapshot_document_key(uuid: str) -> str:
    return f"snap-{uuid}.dat"


def parse_snapshot_document(data: bytes, where: str) -> SnapshotExtent:
    """Decode one `snap-<uuid>.dat` and read the extent it declares."""
    document = unwrap(data)
    if not isinstance(document, dict):
        raise ShapeGateError(
            f"{where} decoded to a {type(document).__name__}, not a snapshot "
            "document")
    # Real 9.5.2 nests everything under a `snapshot` key. Accepting both
    # shapes costs nothing and the nesting is not something to depend on.
    body = document.get("snapshot", document)
    if not isinstance(body, dict):
        raise ShapeGateError(f"{where} has a malformed snapshot object")
    uuid, name = body.get("uuid"), body.get("name")
    if not isinstance(uuid, str) or not uuid:
        raise ShapeGateError(f"{where} declares no snapshot uuid")
    if not isinstance(name, str) or not name:
        raise ShapeGateError(f"{where} declares no snapshot name")
    indices = body.get("indices")
    if not isinstance(indices, list) or not all(
            isinstance(i, str) for i in indices):
        raise ShapeGateError(
            f"{where} declares no usable indices list, so it states no extent")
    return SnapshotExtent(
        uuid=uuid, name=name, index_names=tuple(indices),
        total_shards=_count(body.get("total_shards")),
        successful_shards=_count(body.get("successful_shards")),
        by_index_name=_details(body.get("index_details"), where))


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and value >= 0 else None


def _details(raw: Any, where: str) -> Dict[str, IndexExtent]:
    """Per-index extent, or nothing when the document does not carry it.

    An absent `index_details` map is no opinion about per-index size, not a
    claim that every index is empty, so it yields an empty mapping and the
    caller checks only what was actually declared.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, IndexExtent] = {}
    for name, detail in raw.items():
        if not isinstance(name, str) or not isinstance(detail, dict):
            raise ShapeGateError(f"{where} has a malformed index_details entry")
        shard_count = _count(detail.get("shard_count"))
        if shard_count is None:
            continue
        out[name] = IndexExtent(shard_count=shard_count,
                                size_in_bytes=_count(detail.get("size_in_bytes")))
    return out
