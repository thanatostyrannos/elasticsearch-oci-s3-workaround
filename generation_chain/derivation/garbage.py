"""What a delete operation should have removed and did not.

THE ALGORITHM IS ELASTICSEARCH'S OWN, and this module is where it is applied.
Its blobstore package documentation says a delete collects "all segment blobs
(identified by having the data blob prefix `__`) in the shard directory which
are not referenced by the new BlobStoreIndexShardSnapshots", then deletes them.
That set difference is `ShardHistory.collectable`, computed once per shard in
`shards.py`, and it is the only subtraction in this package.

WHAT THIS TOOL NAMES IS A SUBSET OF THAT. Elasticsearch deletes on ABSENCE from
the current file list. This tool condemns on PRESENCE: it names a blob only when
it can also point at the delete operation that orphaned it. So the manifest is
`collectable` intersected with what some observed delete accounted for, which is
smaller. The report says so rather than claiming parity, because the difference
matters to an operator: a blob this tool leaves out is not a blob it calls live.

THERE ARE TWO KINDS OF EDGE and they have different completeness conditions.
Keeping them apart matters, because every counterexample this package has had
lived in one or the other and they are not fixed by the same thing.

  SEGMENT EDGES go through a shard document. The live set for a shard is ONE
  OBJECT, at the generation the anchor catalog names, and it already holds every
  snapshot's file list for that shard unioned. Blobs are shard-scoped, so
  nothing outside that directory can add an edge. Complete by construction: the
  only ways to get it wrong are not reading that object, or reading something
  else in its place, which is what `identity.py` stands against.

  METADATA EDGES go through `index_metadata_lookup`, from a snapshot to the
  `indices/<index>/meta-<id>.dat` blob it uses. These are NOT complete by
  construction. They are assembled from two maps written in different parts of
  the catalog, and assembling anything from a partial input is how a live set
  comes up short. Three separate counterexamples came from exactly here: a
  lookup missing one index, a lookup value of the wrong type dropped by a
  comprehension, and a catalog with no lookups at all. So the condition is
  stated and absolute: an index whose lookup this run cannot resolve COMPLETELY
  contributes no condemnations for its metadata blobs. Nothing, not a best
  effort over what resolved.

Every claim here rests on three positive readings and never on an absence in
this tool's own bookkeeping:

  1. generation A named snapshot S and generation A+1 did not, so the delete of
     S is the operation that ran between them;
  2. the shard document of generation A names blob X as belonging to S;
  3. the anchor generation's document for that shard does not name X, the
     store's listing shows X, and the store confirms X on a second ask.

A key failing any of the three is left out, which is why an unreadable input can
only ever shorten this list.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Set

from ..model import (Condemnation, DeleteOperation, RootGeneration,
                     ShardLocation)
from .chain import Chain
from .keys import KeyIndex
from .shards import ShardSurvey

CATEGORY_SEGMENT = "segment blob"
CATEGORY_SHARD_SNAPSHOT = "shard snapshot document"
CATEGORY_ROOT_SNAPSHOT = "root snapshot document"
CATEGORY_GLOBAL_METADATA = "global metadata"
CATEGORY_INDEX_METADATA = "index metadata"


def delete_operations(chain: Chain) -> List[DeleteOperation]:
    """Every snapshot that left the catalog across an adjacent pair."""
    operations: List[DeleteOperation] = []
    for earlier, later in chain.adjacent_pairs:
        before = chain.generations[earlier]
        after = chain.generations[later]
        for uuid in sorted(set(before.snapshots) - set(after.snapshots)):
            snapshot = before.snapshots[uuid]
            operations.append(DeleteOperation(
                snapshot_uuid=uuid, snapshot_name=snapshot.name,
                from_generation=earlier, to_generation=later))
    return operations


def condemn(chain: Chain, survey: ShardSurvey, keys: KeyIndex,
            notes: List[str]) -> List[Condemnation]:
    """Every key some delete operation should have removed and did not.

    Expressed on top of the two pieces a batched caller needs apart:
    `condemn_repository_wide` for the three kinds that never depend on which
    shard directories were surveyed, and `condemn_segments` for the one kind
    that does. A single, unbatched call to `survey_shards` covering the whole
    repository is the batched design's one-batch case, so this function stays
    the reference the batched path is measured against.
    """
    unread_indices = unread_indices_of(survey)
    found = condemn_repository_wide(chain, keys, notes, unread_indices)
    operations = segment_eligible_operations(chain, notes)
    condemn_segments(survey, operations, keys, found)
    return sorted(found.values(), key=lambda c: c.key)


def unread_indices_of(survey: ShardSurvey) -> Set[str]:
    """Indices a dropped shard directory belongs to.

    An index whose evidence is incomplete in even one shard directory
    contributes no metadata condemnation, because the metadata edge is
    assembled rather than complete by construction. See
    `_condemn_index_metadata`.
    """
    return {directory.split("/")[1] for directory in survey.dropped
            if directory.count("/") >= 2}


def condemn_repository_wide(chain: Chain, keys: KeyIndex, notes: List[str],
                            unread_indices: Set[str]
                            ) -> Dict[str, Condemnation]:
    """Root snapshot, global metadata, shard-snapshot and index-metadata claims.

    None of these read a shard's file lists, so none of them depends on which
    shard directories a batched run has surveyed yet. A caller computes this
    exactly once regardless of batch size, and `unread_indices` has to be the
    union over every batch: an index dropped in batch 40 must still suppress
    its metadata condemnation even though this function ran before batch 40
    existed.
    """
    found: Dict[str, Condemnation] = {}
    live_metadata = live_metadata_blobs(chain.final, notes)
    for operation in delete_operations(chain):
        if operation.snapshot_uuid in chain.final.snapshots:
            continue
        _condemn_root_documents(operation, keys, found)
        _condemn_shard_documents(chain, operation, keys, found)
        _condemn_index_metadata(chain, operation, keys, live_metadata,
                                unread_indices, found)
    return found


def segment_eligible_operations(chain: Chain,
                                notes: List[str]) -> List[DeleteOperation]:
    """Delete operations `condemn_segments` may attribute a blob to.

    Filters out the one case a segment claim can never be made: the deleted
    snapshot's name is also carried by a snapshot that is still live, so a
    shard document's by-name file list cannot be resolved to the dead uuid
    without guessing. Computed once, so the note this appends is written once
    regardless of how many shard-directory batches a caller processes.
    """
    surviving_names = {s.name for s in chain.final.snapshots.values()}
    eligible = []
    for operation in delete_operations(chain):
        if operation.snapshot_uuid in chain.final.snapshots:
            continue
        if operation.snapshot_name in surviving_names:
            notes.append(
                f"snapshot {operation.snapshot_name!r} deleted between "
                f"generation {operation.from_generation} and "
                f"{operation.to_generation} shares its name with a snapshot "
                "that is still live, so no shard file list was attributed to it")
            continue
        eligible.append(operation)
    return eligible


def condemn_segments(survey: ShardSurvey, operations: List[DeleteOperation],
                     keys: KeyIndex, found: Dict[str, Condemnation]) -> None:
    """`_condemn_segments` for every eligible operation, against one survey.

    `survey` may cover the whole repository or one batch of shard
    directories; either way this only ever adds entries for the directories
    `survey.histories` actually holds, which is what makes calling it once
    per batch sound. Segment edges are complete by construction within a
    shard directory, so a batch never needs to see a directory outside itself
    to condemn correctly inside it.
    """
    for operation in operations:
        _condemn_segments(survey, operation, keys, found)


def _remember(found: Dict[str, Condemnation], candidate: Condemnation) -> None:
    """Keep the LAST operation that should have removed a key.

    A blob can be named by several snapshots deleted at different times, and the
    operation an operator wants named is the one after which nothing referenced
    it. Operations arrive in generation order, so the later replaces the earlier.
    """
    found[candidate.key] = candidate


def _condemn_segments(survey: ShardSurvey, operation: DeleteOperation,
                      keys: KeyIndex, found: Dict[str, Condemnation]) -> None:
    """The set difference, intersected with what this operation accounted for.

    `history.collectable` is Elasticsearch's own answer for the directory. This
    keeps the members of it that the deleted snapshot's own era file list names,
    so the manifest can say which operation orphaned each key.
    """
    for location, history in survey.histories.items():
        document = history.documents.get(operation.from_generation)
        if document is None:
            continue
        named = document.by_snapshot_name.get(operation.snapshot_name)
        if not named:
            continue
        for blob in sorted(named & history.collectable):
            for key in keys.objects_for(f"{location.directory}/{blob}"):
                _remember(found, Condemnation(
                    key=key, category=CATEGORY_SEGMENT,
                    reason=operation.describes("orphaned"),
                    snapshot_uuid=operation.snapshot_uuid,
                    snapshot_name=operation.snapshot_name,
                    from_generation=operation.from_generation,
                    to_generation=operation.to_generation))


def _condemn_root_documents(operation: DeleteOperation, keys: KeyIndex,
                            found: Dict[str, Condemnation]) -> None:
    """`snap-<uuid>.dat` and `meta-<uuid>.dat` carry the uuid in the name.

    These need no file list. The uuid was in one generation and not the next,
    and a blob whose name contains that uuid belonged to it.
    """
    for key, category in (
            (f"snap-{operation.snapshot_uuid}.dat", CATEGORY_ROOT_SNAPSHOT),
            (f"meta-{operation.snapshot_uuid}.dat", CATEGORY_GLOBAL_METADATA)):
        if key in keys:
            _remember(found, Condemnation(
                key=key, category=category,
                reason=operation.describes("left behind"),
                snapshot_uuid=operation.snapshot_uuid,
                snapshot_name=operation.snapshot_name,
                from_generation=operation.from_generation,
                to_generation=operation.to_generation))


def _condemn_shard_documents(chain: Chain, operation: DeleteOperation,
                             keys: KeyIndex,
                             found: Dict[str, Condemnation]) -> None:
    """`indices/<index>/<shard>/snap-<uuid>.dat`, one per shard the snapshot took.

    Named by uuid like the root documents, so this claim needs no file list, no
    live set and no readable shard document. The uuid was in one generation and
    not the next, and `condemn` has already established that the anchor catalog
    does not hold it.

    WHY THE DROPPED SET IS NOT CONSULTED HERE, when a dropped shard contributes
    nothing everywhere else. That rule governs the LIVE SET: a segment claim is
    only as good as the file list it was measured against, so a shard whose
    evidence is incomplete may not produce one. This claim uses no live set. It
    is chain-level evidence, exactly like the root `snap-<uuid>.dat` beside it,
    and a doubt about one directory's file lists says nothing about it.

    Gating it on shard evidence also broke monotonicity, which is how the
    reasoning above got checked rather than asserted. A fault that swapped the
    anchor for an older generation made an index the healthy anchor no longer
    lists surveyable again, and the manifest GREW by one of these keys against
    the healthy baseline. Found by the property search, not by review.
    """
    earlier = chain.generations[operation.from_generation]
    for index_uuid, entry in sorted(earlier.indices.items()):
        if operation.snapshot_uuid not in entry.snapshot_uuids:
            continue
        for shard in range(len(entry.shard_generations)):
            directory = ShardLocation(index_uuid=index_uuid,
                                      shard=shard).directory
            key = f"{directory}/snap-{operation.snapshot_uuid}.dat"
            if key in keys:
                _remember(found, Condemnation(
                    key=key, category=CATEGORY_SHARD_SNAPSHOT,
                    reason=operation.describes("left behind"),
                    snapshot_uuid=operation.snapshot_uuid,
                    snapshot_name=operation.snapshot_name,
                    from_generation=operation.from_generation,
                    to_generation=operation.to_generation))


def _condemn_index_metadata(chain: Chain, operation: DeleteOperation,
                            keys: KeyIndex,
                            live: Optional[Dict[str, Set[str]]],
                            unread_indices: Set[str],
                            found: Dict[str, Condemnation]) -> None:
    """`indices/<uuid>/meta-<id>.dat`, when the format names them by id.

    `live` is None when the live set for index metadata could not be
    established completely, and this run then makes no claim about index
    metadata at all.
    """
    if live is None:
        return
    earlier = chain.generations[operation.from_generation]
    identifiers = earlier.index_metadata_identifiers
    snapshot = earlier.snapshots.get(operation.snapshot_uuid)
    if snapshot is None:
        return
    for index_uuid, lookup in sorted(snapshot.metadata_lookup.items()):
        if index_uuid in unread_indices:
            continue
        blob_id = identifiers.get(lookup)
        if blob_id is None:
            continue
        # An index absent from `live` is one no live snapshot references. That
        # is a reading of a COMPLETE map rather than a default standing in for a
        # missing one: `live_metadata_blobs` returns None, and this function
        # returns early, unless every live snapshot's lookup resolved.
        if blob_id in live.get(index_uuid, set()):
            continue
        key = f"indices/{index_uuid}/meta-{blob_id}.dat"
        if key in keys:
            _remember(found, Condemnation(
                key=key, category=CATEGORY_INDEX_METADATA,
                reason=operation.describes("left behind"),
                snapshot_uuid=operation.snapshot_uuid,
                snapshot_name=operation.snapshot_name,
                from_generation=operation.from_generation,
                to_generation=operation.to_generation))


def live_metadata_blobs(final: RootGeneration,
                        notes: List[str]) -> Optional[Dict[str, Set[str]]]:
    """Index metadata blob ids the surviving snapshots still reference.

    Returns None when the live set cannot be established completely. A partial
    live set here would condemn metadata a snapshot this run failed to resolve
    is still using, which is the absence-shaped mistake this tool exists to
    avoid.
    """
    identifiers = final.index_metadata_identifiers
    live: Dict[str, Set[str]] = {}
    for snapshot in final.snapshots.values():
        for index_uuid, lookup in snapshot.metadata_lookup.items():
            blob_id = identifiers.get(lookup)
            if blob_id is None:
                notes.append(
                    f"snapshot {snapshot.name!r} references index metadata "
                    f"{lookup!r} that the current generation does not map, so "
                    "no index metadata blob was considered")
                return None
            live.setdefault(index_uuid, set()).add(blob_id)
    return live


def attribution_coverage(
        chain: Chain,
        era_snapshot_names: Dict[ShardLocation, Dict[int, FrozenSet[str]]]):
    """How many delete operations this run could follow all the way down.

    Counting generation TRANSITIONS answers "how much of the history did I see".
    It does not answer "how much of what I saw could I attribute", and the two
    come apart: a run can read every generation, find every delete, and still
    fail to attribute one because a shard it needed was dropped. A headline
    built on transitions alone reads 100% on that run, which is the more
    flattering of two true numbers.

    `era_snapshot_names` carries only the snapshot NAMES each shard's era
    document named, not the blobs, because that is all this completeness
    check reads. A batched caller can therefore afford to keep it for the
    whole run: it is bounded by how many shards, generations and snapshots
    the repository actually has, not by how many blobs are in it, which is
    the number this whole change exists to stop holding onto.
    """
    surviving = {s.name for s in chain.final.snapshots.values()}
    found = 0
    attributed = 0
    for operation in delete_operations(chain):
        if operation.snapshot_uuid in chain.final.snapshots:
            continue
        found += 1
        if operation.snapshot_name in surviving:
            continue
        earlier = chain.generations[operation.from_generation]
        complete = True
        for index_uuid, entry in earlier.indices.items():
            if operation.snapshot_uuid not in entry.snapshot_uuids:
                continue
            for shard in range(len(entry.shard_generations)):
                location = ShardLocation(index_uuid=index_uuid, shard=shard)
                names = era_snapshot_names.get(location, {}).get(
                    operation.from_generation)
                if names is None or operation.snapshot_name not in names:
                    complete = False
        if complete:
            attributed += 1
    return found, attributed


def era_snapshot_names_of(
        survey: ShardSurvey) -> Dict[ShardLocation, Dict[int, FrozenSet[str]]]:
    """The lightweight input `attribution_coverage` needs, from a full survey.

    A convenience for a caller holding one whole-repository `ShardSurvey`,
    such as `condemn`'s own callers before this change existed. A batched
    caller builds the same shape directly, one batch's histories at a time,
    and never needs a `ShardSurvey` covering more than one batch to do it.
    """
    return {location: {generation: frozenset(document.by_snapshot_name)
                       for generation, document in history.documents.items()}
            for location, history in survey.histories.items()}
