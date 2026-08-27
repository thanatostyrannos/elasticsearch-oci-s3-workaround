"""`index-<N>` at the repository root: a complete RepositoryData document.

Elasticsearch writes this as plain JSON. It names every live snapshot, every
index those snapshots reference, and the current shard generation for every
shard, which is everything the chain derivation needs to say what one delete
operation removed.

Every check here refuses rather than repairs. A generation this module will
not parse is a delete operation the run cannot explain, and the caller drops
it, which shrinks the manifest. A generation this module parsed wrongly would
be attributed anyway, so the shape gate is the last place to stop that.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..errors import (BlobFormatError, ShapeGateError,
                      UnsupportedRepository)
from ..model import IndexEntry, RootGeneration, SnapshotRef
from ..supported import require_supported_format

# A root generation key, and only at the repository root. The anchoring is not
# decoration: shard generations can be numeric, so `indices/<uuid>/0/index-3`
# matches this pattern too, and a scan that accepted it at any depth would
# read a shard file list as a repository catalog.
ROOT_GENERATION_KEY = re.compile(r"^index-(0|[1-9][0-9]*)$")


def root_generation_number(key: str) -> Optional[int]:
    """The generation a key names, or None when the key is not one.

    Rejects any key holding a slash before the pattern is even considered, so
    depth is checked structurally rather than by hoping the regex covers it.
    """
    if "/" in key:
        return None
    match = ROOT_GENERATION_KEY.match(key)
    return int(match.group(1)) if match else None


def parse_repository_data(data: bytes, generation: int) -> RootGeneration:
    """Decode one root generation blob and put it through the shape gate."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlobFormatError(
            f"generation {generation} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ShapeGateError(
            f"generation {generation} is a {type(document).__name__}, "
            "not a RepositoryData object")
    # The format precondition runs on the raw document, before the record the
    # derivation reads is built, so nothing downstream can be produced from a
    # shape the guards do not cover.
    require_supported_format(document, generation)
    return _build(document, generation)


def _build(document: Mapping[str, Any], generation: int) -> RootGeneration:
    snapshots = _snapshots(document, generation)
    indices = _indices(document, generation)
    _cross_check(snapshots, indices, generation)
    return RootGeneration(
        generation=generation,
        repository_uuid=_repository_uuid(document, generation),
        snapshots=snapshots,
        indices=indices,
        index_metadata_identifiers=_metadata_identifiers(document, generation),
    )


def _cross_check(snapshots: Mapping[str, SnapshotRef],
                 indices: Mapping[str, IndexEntry], generation: int) -> None:
    """The two halves of the catalog have to agree with each other.

    Elasticsearch writes the snapshots array and the indices map from one
    state, so every index a live snapshot references appears in the map. When
    they disagree, one of the two was decoded wrongly and there is no way to
    tell which, so the generation is refused. Reading on would build a live
    set out of half a document, and a live set that is too small is how this
    derivation would come to name a blob that is still in use.
    """
    for snapshot in snapshots.values():
        for index_uuid in snapshot.index_uuids:
            if index_uuid not in indices:
                raise ShapeGateError(
                    f"generation {generation} snapshot {snapshot.name!r} "
                    f"references index {index_uuid}, which its indices map "
                    "does not list")
    # And the other direction, which is the one that costs data. A snapshot
    # whose lookup is SHORT by one index still parses, and the live set built
    # from it is then short by one index metadata blob that the snapshot is
    # still using.
    for index_uuid, entry in indices.items():
        for uuid in entry.snapshot_uuids:
            snapshot = snapshots.get(uuid)
            if snapshot is None:
                raise ShapeGateError(
                    f"generation {generation} index {entry.name!r} names "
                    f"snapshot {uuid}, which its snapshots array does not "
                    "hold")
            if index_uuid not in snapshot.metadata_lookup:
                raise ShapeGateError(
                    f"generation {generation} index {entry.name!r} names "
                    f"snapshot {snapshot.name!r}, whose index_metadata_lookup "
                    "does not mention it")


def _repository_uuid(document: Mapping[str, Any], generation: int) -> Optional[str]:
    """The uuid the writer of this blob claims for the repository.

    A missing `uuid` is no opinion rather than evidence of a stranger, so it
    comes back as None and the caller declines to attribute the generation.
    Present-and-not-a-string is different: something wrote a field we do not
    understand under a name we rely on.
    """
    if "uuid" not in document:
        return None
    value = document["uuid"]
    if not isinstance(value, str) or not value:
        raise ShapeGateError(
            f"generation {generation} carries a non-string repository uuid")
    return value


def _snapshots(document: Mapping[str, Any],
               generation: int) -> Dict[str, SnapshotRef]:
    """The live catalog.

    A missing `snapshots` array is refused rather than read as an empty
    catalog. An empty catalog would say every snapshot in the previous
    generation had just been deleted, which is the single input that would
    make this tool name the most keys it possibly could.

    Requiring a LIST is also the second guard against reading a shard
    document as a catalog: a BlobStoreIndexShardSnapshots has a `snapshots`
    field too, and it is an object.
    """
    if "snapshots" not in document:
        raise ShapeGateError(
            f"generation {generation} has no snapshots array")
    raw = document["snapshots"]
    if not isinstance(raw, list):
        raise ShapeGateError(
            f"generation {generation} has a {type(raw).__name__} where the "
            "snapshots array belongs")
    out: Dict[str, SnapshotRef] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ShapeGateError(
                f"generation {generation} has a non-object snapshot entry")
        uuid = entry.get("uuid")
        name = entry.get("name")
        if not isinstance(uuid, str) or not uuid:
            raise ShapeGateError(
                f"generation {generation} has a snapshot with no uuid")
        if not isinstance(name, str) or not name:
            raise ShapeGateError(
                f"generation {generation} has snapshot {uuid} with no name")
        out[uuid] = SnapshotRef(uuid=uuid, name=name,
                                metadata_lookup=_lookup(entry, generation, uuid))
    return out


def _lookup(entry: Mapping[str, Any], generation: int,
            uuid: str) -> Mapping[str, str]:
    """A snapshot's index-uuid to metadata-identifier map, or no opinion.

    An ABSENT field contradicts the catalog's own `min_version`, which the
    precondition has already accepted, so this is a document disagreeing with
    its own declaration rather than an older format.

    A field that is present is read strictly. Dropping an entry whose value is
    not a string, which a comprehension does silently, produces a live set
    that is short by one index, and a live set that is short is exactly how
    this tool would come to name metadata a live snapshot still uses.
    """
    if "index_metadata_lookup" not in entry:
        raise UnsupportedRepository(
            f"generation {generation} snapshot {uuid} carries no "
            "index_metadata_lookup, which a catalog declaring a supported "
            "min_version always writes; the declaration and the document "
            "disagree")
    lookup = entry["index_metadata_lookup"]
    if not isinstance(lookup, dict):
        raise ShapeGateError(
            f"generation {generation} snapshot {uuid} has a "
            f"{type(lookup).__name__} index_metadata_lookup")
    for key, value in lookup.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ShapeGateError(
                f"generation {generation} snapshot {uuid} has an "
                "index_metadata_lookup entry that is not a pair of strings")
    return dict(lookup)


def _indices(document: Mapping[str, Any],
             generation: int) -> Dict[str, IndexEntry]:
    """Index uuid to its entry, including this generation's shard generations."""
    if "indices" not in document:
        raise ShapeGateError(f"generation {generation} has no indices map")
    raw = document["indices"]
    if not isinstance(raw, dict):
        raise ShapeGateError(
            f"generation {generation} has a {type(raw).__name__} where the "
            "indices map belongs")
    out: Dict[str, IndexEntry] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ShapeGateError(
                f"generation {generation} has a malformed indices entry")
        index_uuid = entry.get("id")
        if not isinstance(index_uuid, str) or not index_uuid:
            raise ShapeGateError(
                f"generation {generation} index {name!r} has no id")
        out[index_uuid] = IndexEntry(
            name=name,
            uuid=index_uuid,
            snapshot_uuids=_snapshot_uuids(entry, generation, name),
            shard_generations=_shard_generations(entry, generation, name),
        )
    return out


def _snapshot_uuids(entry: Mapping[str, Any], generation: int,
                    index_name: str) -> Tuple[str, ...]:
    """The snapshots this generation says reference one index.

    Read strictly for the same reason as the metadata lookup: this list is one
    half of the pair that establishes which snapshots are still using an
    index, and a quietly shortened half produces a quietly shortened live set.
    """
    if "snapshots" not in entry:
        raise ShapeGateError(
            f"generation {generation} index {index_name!r} has no snapshots "
            "list. An absent list is a claim this catalog did not make, and "
            "reading it as an empty one says no live snapshot uses the index")
    raw = entry["snapshots"]
    if not isinstance(raw, list):
        raise ShapeGateError(
            f"generation {generation} index {index_name!r} has a "
            f"{type(raw).__name__} where its snapshots list belongs")
    for value in raw:
        if not isinstance(value, str) or not value:
            raise ShapeGateError(
                f"generation {generation} index {index_name!r} lists a "
                "snapshot that is not a uuid")
    return tuple(raw)


def _shard_generations(entry: Mapping[str, Any], generation: int,
                       index_name: str) -> Tuple[Optional[str], ...]:
    """Per-shard generation ids, with "no opinion" preserved as None.

    A repository written before 7.6 has no shard_generations at all. That is a
    real repository rather than a broken one, so it yields an empty tuple,
    every shard reads as "no generation named", and the caller drops those
    shards instead of guessing an id.
    """
    raw = entry.get("shard_generations")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ShapeGateError(
            f"generation {generation} index {index_name!r} has a "
            f"{type(raw).__name__} where shard_generations belongs")
    out: List[Optional[str]] = []
    for value in raw:
        if value is None:
            out.append(None)
        elif isinstance(value, str) and value:
            out.append(value)
        else:
            raise ShapeGateError(
                f"generation {generation} index {index_name!r} has a shard "
                "generation that is neither a string nor null")
    return tuple(out)


def _metadata_identifiers(document: Mapping[str, Any],
                          generation: int) -> Mapping[str, str]:
    """The lookup-value to metadata-blob-id map, when the format carries one.

    A catalog that declared a supported `min_version` and does not carry this
    map contradicts itself, which is refused rather than worked around.
    """
    raw = document.get("index_metadata_identifiers")
    if raw is None:
        raise UnsupportedRepository(
            f"generation {generation} carries no index_metadata_identifiers "
            "map, which a catalog declaring a supported min_version always "
            "writes; the declaration and the document disagree")
    if not isinstance(raw, dict):
        raise ShapeGateError(
            f"generation {generation} has a {type(raw).__name__} where "
            "index_metadata_identifiers belongs")
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ShapeGateError(
                f"generation {generation} has an index_metadata_identifiers "
                "entry that is not a pair of strings")
    return dict(raw)
