"""What each shard directory holds, what of it is live, and what cannot be told.

This module answers one question per shard directory and refuses to answer at
all when it cannot answer completely. A shard it answers for carries three
facts: the blobs the store LISTS there, the blobs the CURRENT
BlobStoreIndexShardSnapshots references, and the file lists of the earlier eras.
A shard it will not answer for is dropped whole and named in the coverage
report, and contributes nothing anywhere.

THE LIVE SET COMES FROM ONE REQUIRED DOCUMENT. It is the shard document the
ANCHOR root generation names, `indices/<index>/<shard>/index-<gen>`, and nothing
else contributes to it. An earlier version widened it with every other document
it happened to read, which looked safer and was not: a protection that depends
on an OPTIONAL read disappears the moment a second failure removes that read,
and the manifest then grows. Anything protecting live data has to be REQUIRED,
so its absence drops the shard rather than quietly weakening the answer.

A FAILED ERA DOCUMENT IS LOCAL. It removes the attributions that document would
have carried and nothing else. An earlier version dropped the whole shard
instead, and that is where the derivation stopped being monotone: what the run
needed to read depended on what it had managed to read, so corrupting a root
generation removed the requirement to read a shard document, which re-admitted
the shard that document had been suppressing. Adding a failure grew the list.

ABSENCE IS NEVER POSITIVE EVIDENCE, and this module is where that rule has been
broken most often. An index the current generation does not list looks like an
empty live set and is not one; it is a question this run did not get an answer
to. A shard the catalog names no generation for states no opinion. A snapshot
document nobody could read verified nothing. Each of those drops the shard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import (Dict, FrozenSet, Iterable, List, Optional, Set, Tuple)

from ..errors import GenerationChainError, RunRefused, SourceReadError
from ..formats.shard_snapshots import parse_shard_snapshots, segment_stem
from ..formats.snapshot_document import (parse_snapshot_document,
                                         snapshot_document_key)
from ..model import ShardDocument, ShardLocation
from ..sources import hint
from ..sources import RepositorySource
from ..sources.budget import RESIDENT_BYTES_PER_OBJECT
from .chain import Chain
from .identity import (Doubt, check_directory, check_snapshot_names,
                       require_blob_names, writer_uuid_collisions,
                       WRITER_UUID_COLLISION)
from .keys import KeyIndex

# `indices/<index>/<shard>/snap-<uuid>.dat`, one snapshot's own shard document.
SHARD_SNAPSHOT_DOCUMENT = re.compile(r"^snap-(.+)\.dat$")

INDEX_IN_USE_BUT_UNLISTED = "index-not-listed-but-a-live-snapshot-is-here"
INDEX_REFERENCED_BUT_UNLISTED = "index-not-listed-but-a-live-lookup-names-it"
INDEX_RETIRED = "index-no-live-snapshot-references"
NO_SHARD_GENERATION = "catalog-names-no-generation-for-this-shard"
CURRENT_DOCUMENT_UNREADABLE = "current-shard-document-unreadable"
LIVE_SNAPSHOT_NOT_IN_FILE_LIST = "live-snapshot-here-is-absent-from-the-file-list"
EXTENT_UNREADABLE = "snapshot-extent-unreadable"
EXTENT_INDEX_NOT_TRAVERSED = "snapshot-declares-an-index-this-run-did-not-read"
EXTENT_SHARD_COUNT = "snapshot-declares-a-different-shard-count"
EXTENT_TOTAL_SHARDS = "snapshot-declares-a-different-total-shard-count"
EXTENT_SIZE = "snapshot-declares-a-different-size"
EXTENT_NOT_DECLARED = "snapshot-declares-no-shard-count-for-this-index"


@dataclass
class ShardHistory:
    """One shard directory this run read completely."""

    location: ShardLocation
    live_blobs: FrozenSet[str]
    present_blobs: FrozenSet[str]
    current: ShardDocument
    documents: Dict[int, ShardDocument] = field(default_factory=dict)
    unreadable: Dict[int, Doubt] = field(default_factory=dict)
    # The union of every document's writer uuids this shard has produced,
    # current generation and every era read so far. Kept even after
    # `documents` is cleared, because the writer-uuid collision check has to
    # run once against the whole run rather than once per batch: the set is
    # bounded by how many Lucene writers a shard has ever had, not by how
    # much history it carries (measured stable at nine values across three
    # generations of one shard, see docs/repository-layout-and-reachability.md),
    # so holding it for the whole run costs nothing like what holding the
    # documents themselves did.
    writer_uuids: FrozenSet[object] = frozenset()

    @property
    def collectable(self) -> FrozenSet[str]:
        """Elasticsearch's own answer for this shard, and the ONLY subtraction.

        From the blobstore package documentation: a delete collects "all segment
        blobs (identified by having the data blob prefix `__`) in the shard
        directory which are not referenced by the new BlobStoreIndexShardSnapshots"
        and deletes them. That is a shard-local set difference, and this is it.

        There is exactly one of these in the package on purpose. The retired
        design had two protections against naming live data, the subtraction and
        a take-back during classification, and they covered for each other so
        completely that removing EITHER alone changed no test's result. Measured
        on the retired tree: removing one, suite green; removing the other, suite
        green; removing both, six live blobs named. Redundancy is what made both
        of them unpinnable, so there is now one, and it cannot be vacuous.
        """
        return frozenset(self.present_blobs - self.live_blobs)


@dataclass
class CommitOracleTally:
    """How many (snapshot, commit) pairs issue #21's Lucene cross-check saw.

    Counted once per shard document this run actually decodes, at `_read`,
    the one place every parse funnels through: a document served from cache
    is not counted twice, because the cross-check did not run a second time
    for it either. `checked` is a pair the oracle compared against the file
    list. `skipped` is a pair it had no inline commit bytes for, so it fell
    back to the older presence-only gate without comparing anything.

    This exists because a run where the oracle fired on every entry and a
    run where it fired on none otherwise produce an identical manifest and
    an identical `Coverage`. `KeyIndex.unanswered` is the same shape for the
    same reason: folding a check that did not run into a check that ran and
    passed is the one measured place this tool's report has been wrong
    rather than conservative, and a guard closing a P0 data-loss path does
    not get a quieter version of that mistake.
    """

    checked: int = 0
    skipped: int = 0

    def record(self, document: ShardDocument) -> None:
        self.checked += document.commit_oracle_checked
        self.skipped += document.commit_oracle_skipped


@dataclass
class ShardSurvey:
    """Three answers per shard directory, and they are not the same answer.

    `histories` holds the shards this run read completely. `dropped` holds the
    shards whose evidence is in DOUBT, which is a fault worth an operator's
    attention. `retired` holds the shards of an index no live snapshot
    references any more, which is the ordinary result of dropping an index and
    is not a fault at all.

    The first version of this rewrite put the last two in one bucket, and the
    completeness tests caught it: a healthy repository reported a dropped shard
    for every index its users had deleted, which inflates the one number an
    operator reads to decide whether a run went well.
    """

    histories: Dict[ShardLocation, ShardHistory]
    dropped: Dict[str, Doubt]
    considered: int
    retired: Dict[str, Doubt] = field(default_factory=dict)
    commit_oracle_checked: int = 0
    commit_oracle_skipped: int = 0

    def collectable(self, directory: str) -> FrozenSet[str]:
        for location, history in self.histories.items():
            if location.directory == directory:
                return history.collectable
        return frozenset()


def survey_shards(source: RepositorySource, chain: Chain, keys: Iterable[str],
                  index: KeyIndex, notes: Optional[List[str]] = None,
                  groups: Optional[List[List[ShardLocation]]] = None,
                  on_group=None, on_collision=None) -> ShardSurvey:
    """Read every shard the chain names, completely, or drop it.

    Two passes, because the checks below need different amounts of the
    repository read before they can be believed.

    The first pass reads only each shard's CURRENT document, which is cheap:
    one object per directory, never multiplied by how many generations or
    snapshots that directory has lived through. `_check_declared_extent`
    needs every directory's current document before it can run, because a
    live snapshot's declared shard count is checked against shards spread
    across the whole repository, not against one batch of it. Running it
    here, before any batch exists, is what keeps a batched run's drops the
    same as an unbatched one's: a snapshot whose shards land in different
    batches must still be judged against all of them together.

    The second pass reads era documents, one group of `groups` at a time.
    This is the expensive read the memory this package holds is actually
    spent on: `ShardDocument.by_snapshot_name` is one frozenset of blob names
    per snapshot per generation per shard, and it is what multiplies with
    history rather than with repository size.

    `on_group(locations, histories)`, if given, runs right after a group's
    era documents are read, while `histories[location].documents` still
    holds them, so a caller can condemn that group's segments and extract
    whatever else it needs. Once it returns, this clears `.documents` for
    every location the callback saw before moving to the next group; a
    caller happy to hold every era document until the whole survey finishes,
    which is what calling this once and unbatched has always meant, leaves
    `on_group` out and gets exactly that. `groups` is None for every direct
    caller in this package's tests, which surveys every shard directory in
    one group; that is also the batched design's own one-batch case, so
    there is only one implementation of this pass to trust.

    The writer-uuid collision check runs ONCE, after every group has been
    read, against `ShardHistory.writer_uuids`, which every location keeps
    for the whole run regardless of `.documents` being cleared per group.
    It has to run this way rather than per group: two directories whose
    documents claim the same Lucene writer identity are a contradiction
    wherever in the run they are read, and a check that only compared
    directories inside one group would miss a pair split across two of
    them. `on_collision(locations)`, if given, runs once with whichever
    locations this removed, so a caller that condemned a group's segments
    before the whole run's writer uuids were known can take back
    condemnations that came from a directory since found untrustworthy.
    """
    keys = list(keys)
    notes = notes if notes is not None else []
    wanted = _shard_generation_ids(chain)
    present = _blobs_present(keys)
    owners = _owners(present)
    live_documents_here = _live_shard_documents(keys, set(chain.final.snapshots))
    live_indices = _live_index_uuids(chain)

    tally = CommitOracleTally()
    histories, dropped, retired = _survey_current(
        source, chain, wanted, present, owners, live_documents_here,
        live_indices, index, tally)
    _check_declared_extent(source, chain, histories, dropped, notes)

    for group in _shard_batches(sorted(histories, key=_location_order), groups):
        cache: Dict[str, Optional[ShardDocument]] = {}
        # The expensive reads of the whole run: one document per directory per
        # generation, and nothing ever removes a generation. Warmed per group
        # rather than for the run, because the group boundary is already where
        # this package bounds memory, and a hint for every directory at once
        # would hold a body for each.
        #
        # A hint and nothing more. The same keys are read in the same order and
        # `fetch` returns exactly what that key produced, so deleting this line
        # changes the manifest not at all and only makes the run wait for one
        # round trip at a time again.
        hint(source, [f"{location.directory}/index-{shard_generation}"
                      for location in group
                      for _, shard_generation in sorted(wanted[location].items())
                      if shard_generation is not None])
        for location in group:
            history = histories[location]
            _read_eras(source, chain, location, wanted[location], cache,
                      history.present_blobs, owners, index, history, tally)
        if on_group is not None:
            survivors = [location for location in group if location in histories]
            on_group(survivors, histories)
            for location in survivors:
                histories[location].documents = {}

    collided = _drop_global_writer_uuid_collisions(histories, dropped)
    if on_collision is not None and collided:
        on_collision(collided)

    return ShardSurvey(histories=histories, dropped=dropped,
                       considered=len(wanted) - len(retired), retired=retired,
                       commit_oracle_checked=tally.checked,
                       commit_oracle_skipped=tally.skipped)


def _location_order(location: ShardLocation) -> Tuple[str, int]:
    return (location.index_uuid, location.shard)


def _survey_current(
        source: RepositorySource, chain: Chain,
        wanted: Dict[ShardLocation, Dict[int, Optional[str]]],
        present: Dict[str, Set[str]], owners: Dict[str, Set[str]],
        live_documents_here: Dict[str, Set[str]], live_indices: Set[str],
        index: KeyIndex, tally: "CommitOracleTally"
) -> Tuple[Dict[ShardLocation, ShardHistory], Dict[str, Doubt], Dict[str, Doubt]]:
    """Decide which shard directories survive on their current document alone.

    One document per directory, so this never grows with how much history a
    shard carries, which is why it runs for every directory the chain names
    before any batching starts. `tally` is the same instance the batched era
    reads record into later: the count this reports is a whole-run total,
    not a per-batch one, so it has to outlive any one batch's cache.
    """
    cache: Dict[str, Optional[ShardDocument]] = {}
    histories: Dict[ShardLocation, ShardHistory] = {}
    dropped: Dict[str, Doubt] = {}
    retired: Dict[str, Doubt] = {}
    for location in sorted(wanted, key=_location_order):
        stems = frozenset(present.get(location.directory, set()))
        live, current, doubt = _current_live_set(
            source, chain, location, cache, stems, owners, index, live_indices,
            live_documents_here.get(location.directory, set()), tally)
        if doubt is not None:
            if doubt.code == INDEX_RETIRED:
                retired[location.directory] = doubt
            else:
                dropped[location.directory] = doubt
            continue
        histories[location] = ShardHistory(
            location=location, live_blobs=live, present_blobs=stems,
            current=current, writer_uuids=current.writer_uuids)
    return histories, dropped, retired


def _shard_batches(
        surviving: List[ShardLocation],
        groups: Optional[List[List[ShardLocation]]]
) -> List[List[ShardLocation]]:
    """The groups to read era documents in, one batch of directories at a time.

    `groups` is the caller's plan, filtered here to the directories that
    survived the current-document and declared-extent passes; either of
    those can drop a directory before any era document is read. Without a
    plan, everything that survived goes into one group, which is what every
    direct caller in this package's tests gets and is the batched design's
    own one-batch case.
    """
    if groups is None:
        return [surviving] if surviving else []
    survivors = set(surviving)
    return [[location for location in group if location in survivors]
            for group in groups]


def _read_eras(source: RepositorySource, chain: Chain, location: ShardLocation,
               per_generation: Dict[int, Optional[str]],
               cache: Dict[str, Optional[ShardDocument]],
               stems: FrozenSet[str], owners: Dict[str, Set[str]],
               index: KeyIndex, history: ShardHistory,
               tally: CommitOracleTally) -> None:
    """The file lists of the earlier eras, each tied to this directory or left out."""
    for generation, shard_generation in sorted(per_generation.items()):
        if shard_generation is None:
            continue
        where = f"index-{shard_generation}"
        document = _read(source, location, shard_generation, cache, tally)
        if document is None:
            history.unreadable[generation] = Doubt(
                CURRENT_DOCUMENT_UNREADABLE, f"{where} could not be read")
            continue
        doubt = check_directory(document, where, location.directory, stems,
                                owners, index)
        if doubt is None:
            doubt = check_snapshot_names(
                document, where,
                _names_covering(chain, generation, location.index_uuid))
        if doubt is not None:
            # An era document that cannot be tied to this directory and this
            # generation is a file list this run cannot attribute, so the era
            # contributes nothing rather than contributing another shard's files.
            history.unreadable[generation] = doubt
            continue
        history.documents[generation] = document
        history.writer_uuids = history.writer_uuids | document.writer_uuids


@dataclass(frozen=True)
class _WriterUuidWitness:
    """A stand-in for a ShardDocument that carries only its writer uuids.

    `identity.writer_uuid_collisions` reads nothing off what it is given
    except `.writer_uuids`, so this lets the check below run from
    `ShardHistory.writer_uuids`, the small whole-run summary that survives
    `.documents` being discarded, rather than from the documents themselves.
    """

    writer_uuids: FrozenSet[object]


def _drop_global_writer_uuid_collisions(
        histories: Dict[ShardLocation, ShardHistory],
        dropped: Dict[str, Doubt]) -> List[ShardLocation]:
    """A Lucene writer identity seen under two directories drops both.

    See `identity.writer_uuid_collisions` for what was measured and for the
    claim it refutes. The check can only ever REJECT: a matching writer uuid
    never blesses a document, which mirrors how Elasticsearch treats the field
    in `StoreFileMetadata.isSame`, where a mismatch returns false and a match
    falls through to the length, checksum and hash conjunction.

    Runs ONCE, against every surviving directory's whole-run `writer_uuids`
    summary, after every batch has been read. Not per batch: the set a
    directory has accumulated by the end of the run is what the collision
    is measured against, and two directories claiming the same writer
    identity are a contradiction wherever in the run each was read, not
    only when they happen to land in the same group. The set itself is
    bounded by how many Lucene writers a shard has ever had, not by how
    much history it carries: measured stable at nine values across three
    generations of one shard (docs/repository-layout-and-reachability.md),
    so keeping it for the whole run costs nothing like what keeping the
    documents themselves did, and this check does not have to be narrowed
    to afford batching. `check_directory` is necessary and not sufficient
    against a fetch returning another directory's document (a real document
    from a different shard whose blob set happens to be contained in the
    victim directory's still passes it); this is the one check that stands
    against exactly that case, so it runs at full strength.

    Returns the locations this removed. Segment condemnation happens per
    batch, before the whole run's writer uuids are known, so a caller that
    already condemned one of these locations' segments has to take that
    back; see `run_audit`'s `on_collision`.
    """
    by_directory = {location.directory: [_WriterUuidWitness(history.writer_uuids)]
                    for location, history in histories.items()}
    for directory, others in writer_uuid_collisions(by_directory).items():
        dropped[directory] = Doubt(
            WRITER_UUID_COLLISION,
            "documents read here claim Lucene writer identities that also "
            "appear under " + ", ".join(sorted(others))
            + ", so one of those reads returned the other directory's document")
    removed = [location for location in list(histories)
              if location.directory in dropped]
    for location in removed:
        del histories[location]
    return removed


def _current_live_set(source: RepositorySource, chain: Chain,
                      location: ShardLocation,
                      cache: Dict[str, Optional[ShardDocument]],
                      stems: FrozenSet[str], owners: Dict[str, Set[str]],
                      index: KeyIndex, live_indices: Set[str],
                      live_documents: Set[str], tally: CommitOracleTally
                      ) -> Tuple[FrozenSet[str], Optional[ShardDocument],
                                 Optional[Doubt]]:
    """What the ANCHOR generation still says lives in this shard.

    Every branch that returns a Doubt is a place where the retired code read an
    absence as an answer. An index missing from the catalog, a shard the catalog
    names no generation for, a document that would not read: none of those is
    evidence of an empty live set, and an empty live set condemns a whole
    directory.
    """
    entry = chain.final.index_by_uuid(location.index_uuid)
    if entry is None:
        if live_documents:
            # The store holds a live snapshot's own shard document here, so the
            # directory is in use. The catalog not listing the index and that
            # document lying in it cannot both be true.
            return frozenset(), None, Doubt(
                INDEX_IN_USE_BUT_UNLISTED,
                f"generation {chain.current_generation} does not list index "
                f"{location.index_uuid} and the store holds the shard document "
                f"of live snapshot(s) {', '.join(sorted(live_documents))} here")
        if location.index_uuid in live_indices:
            # The catalog contradicts itself: a live snapshot's lookup names
            # this index and the indices map does not hold it. One of the two
            # readings is wrong and there is no way to tell which.
            return frozenset(), None, Doubt(
                INDEX_REFERENCED_BUT_UNLISTED,
                f"a live snapshot references index {location.index_uuid}, "
                f"which generation {chain.current_generation} does not list")
        # No live snapshot references the index by either of the two
        # independent routes the catalog carries, and no live snapshot left a
        # document here. The index was dropped, which is ordinary, and this is
        # NOT a fault to report as one.
        #
        # The shard is still not surveyed. Its whole directory really is
        # garbage and Elasticsearch would remove it, but this run establishes
        # no live set here, and an empty live set is the input that condemns a
        # whole directory off one reading. So its blobs are reported as
        # unexplained and none of them is named. That costs real coverage, in
        # the leaking direction, which is the trade this package makes
        # everywhere.
        return frozenset(), None, Doubt(
            INDEX_RETIRED,
            f"no live snapshot references index {location.index_uuid}, so this "
            "run established no live set here and named none of its segments")

    shard_generation = entry.shard_generation(location.shard)
    if shard_generation is None:
        return frozenset(), None, Doubt(
            NO_SHARD_GENERATION,
            f"generation {chain.current_generation} names no shard generation "
            f"for shard {location.shard} of index {entry.name!r}")
    where = f"index-{shard_generation}"
    document = _read(source, location, shard_generation, cache, tally)
    if document is None:
        return frozenset(), None, Doubt(
            CURRENT_DOCUMENT_UNREADABLE,
            f"the current document {where} could not be read")

    # An index entry naming a snapshot the catalog's snapshots array does not
    # hold is refused by `parse_repository_data._cross_check`, on every
    # generation, before this record exists. This function once repeated that
    # check and the neuter sweep reported it unpinned, because nothing can tell
    # whether an unreachable branch is there. Verified rather than assumed: the
    # parser raises ShapeGateError on that document. The check lives where the
    # decision is, and `test_generation_chain_formats` pins it.
    expected = {chain.final.snapshots[uuid].name
                for uuid in entry.snapshot_uuids}
    doubt = check_snapshot_names(document, where, expected)
    if doubt is not None:
        return frozenset(), None, doubt

    unaccounted = live_documents - {
        uuid for uuid, snapshot in chain.final.snapshots.items()
        if snapshot.name in document.by_snapshot_name}
    if unaccounted:
        # A live snapshot's shard document sits here and the current file list
        # does not mention that snapshot, so the file list is either not this
        # shard's or not current.
        return frozenset(), None, Doubt(
            LIVE_SNAPSHOT_NOT_IN_FILE_LIST,
            "the store holds the shard document of live snapshot(s) "
            + ", ".join(sorted(unaccounted))
            + " here and the current file list does not name them")

    doubt = check_directory(document, where, location.directory, stems, owners,
                            index)
    if doubt is not None:
        return frozenset(), None, doubt
    return document.blob_names, document, None


def _blobs_present(keys: Iterable[str]) -> Dict[str, Set[str]]:
    """Which segment blobs the store lists in each shard directory.

    A blob name is a globally unique id that exists in exactly one directory, so
    this is the only thing available that differs between two shards of one
    index at one generation. `.part<K>` pieces fold into their stem, because a
    file longer than the part size has no object under its bare name.
    """
    out: Dict[str, Set[str]] = {}
    for key in keys:
        directory, _, name = key.rpartition("/")
        stem = segment_stem(name)
        if stem is not None:
            out.setdefault(directory, set()).add(stem)
    return out


def _owners(present: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """Blob stem to every shard directory the listing puts it in.

    In a healthy repository each of these sets holds exactly one directory. A
    stem in two directories is not a fact about the repository, it is a fact
    about a listing this run cannot use to tell those two directories apart.
    """
    out: Dict[str, Set[str]] = {}
    for directory, stems in present.items():
        for stem in stems:
            out.setdefault(stem, set()).add(directory)
    return out


def _live_shard_documents(keys: Iterable[str],
                          live_uuids: Set[str]) -> Dict[str, Set[str]]:
    """Shard directories holding the shard document of a LIVE snapshot.

    This repository leaks, so a `snap-<uuid>.dat` lying in a shard directory
    usually belongs to a snapshot that was deleted. When the uuid is one the
    ANCHOR catalog still holds, it is not leftovers: that snapshot covers this
    shard right now, and it is a second source for a fact the catalog also
    states.
    """
    out: Dict[str, Set[str]] = {}
    for key in keys:
        directory, _, name = key.rpartition("/")
        if not directory.startswith("indices/"):
            continue
        match = SHARD_SNAPSHOT_DOCUMENT.match(name)
        if match and match.group(1) in live_uuids:
            out.setdefault(directory, set()).add(match.group(1))
    return out


def _live_index_uuids(chain: Chain) -> Set[str]:
    """Every index a live snapshot references.

    RepositoryData lists these in two places, the `indices` map and each
    snapshot's `index_metadata_lookup`, and the parser refuses a catalog whose
    two halves disagree. So this set is complete or the run never got here.
    """
    live: Set[str] = set()
    for snapshot in chain.final.snapshots.values():
        live.update(snapshot.metadata_lookup)
    return live


def _check_declared_extent(source: RepositorySource, chain: Chain,
                           histories: Dict[ShardLocation, ShardHistory],
                           dropped: Dict[str, Doubt],
                           notes: List[str]) -> None:
    """Every live snapshot has to account for the extent it declares.

    `snap-<uuid>.dat` states which indices the snapshot holds, how many shards
    in total, how many shards each index has, and how many bytes each index came
    to. Elasticsearch writes that at snapshot time, in a different part of the
    code, into a different object. So a traversal that came up SHORT stops being
    invisible and becomes a contradiction between two independent statements.

    THE RULE. If this run's traversal does not account for what a snapshot says
    it contains, that snapshot's shards contribute nothing. Not a best effort
    over the part that added up.

    THE BOUNDARY. This is another object in the same bucket, so it does not
    defend against a tamper that adjusts the catalog and the snapshot document
    together. It defends against every failure this package has actually had: a
    short list, a missing entry, a silently dropped value, a partial read.
    """
    final = chain.final
    by_name = {entry.name: uuid for uuid, entry in final.indices.items()}
    # One snapshot document per snapshot in the final catalog, read back to
    # back, so warm them together.
    hint(source, [snapshot_document_key(uuid)
                  for uuid in sorted(final.snapshots)])
    for uuid, snapshot in sorted(final.snapshots.items()):
        touched = {i for i, e in final.indices.items() if uuid in e.snapshot_uuids}
        key = snapshot_document_key(uuid)
        try:
            extent = parse_snapshot_document(source.fetch(key), key)
        except (SourceReadError, GenerationChainError) as exc:
            # Unreadable, so this run verified nothing about that snapshot's
            # extent. Every index it is known to touch loses its shards rather
            # than being measured against a declaration nobody read.
            notes.append(f"{key} could not be read ({exc}), so the extent of "
                         f"snapshot {snapshot.name!r} was not verified")
            _drop_indices(histories, dropped, touched, Doubt(
                EXTENT_UNREADABLE,
                f"the extent of live snapshot {snapshot.name!r} could not be "
                "verified"))
            continue
        if not extent.is_complete:
            # A partial snapshot legitimately does not cover what it set out to,
            # so a shortfall says nothing about this run's reading.
            notes.append(
                f"snapshot {snapshot.name!r} declares "
                f"{extent.successful_shards} of {extent.total_shards} shards "
                "successful, so its extent was not used as a completeness check")
            continue
        _measure_against(extent, snapshot.name, touched, by_name, histories,
                         dropped)


def _measure_against(extent, snapshot_name: str, touched: Set[str],
                     by_name: Dict[str, str],
                     histories: Dict[ShardLocation, ShardHistory],
                     dropped: Dict[str, Doubt]) -> None:
    read_names = {entry_name for entry_name, uuid in by_name.items()
                  if uuid in touched}
    absent = set(extent.index_names) - read_names
    if absent:
        _drop_indices(histories, dropped, touched, Doubt(
            EXTENT_INDEX_NOT_TRAVERSED,
            f"snapshot {snapshot_name!r} declares indices "
            + ", ".join(sorted(absent))
            + " that this run did not traverse"))
        return

    total_read = 0
    for index_name in sorted(extent.index_names):
        index_uuid = by_name.get(index_name)
        if index_uuid is None:
            continue
        read = _shards_naming(histories, index_uuid, snapshot_name)
        total_read += len(read)
        declared = extent.by_index_name.get(index_name)
        if declared is None:
            # No shard count declared for this index, so there is nothing to
            # check the traversal against. An absent declaration is not a
            # statement that the traversal was complete.
            _drop_indices(histories, dropped, {index_uuid}, Doubt(
                EXTENT_NOT_DECLARED,
                f"snapshot {snapshot_name!r} declares no shard count for index "
                f"{index_name!r}, so this run cannot tell whether it read all "
                "of it"))
            continue
        if len(read) != declared.shard_count:
            _drop_indices(histories, dropped, {index_uuid}, Doubt(
                EXTENT_SHARD_COUNT,
                f"snapshot {snapshot_name!r} declares {declared.shard_count} "
                f"shard(s) for index {index_name!r} and this run read "
                f"{len(read)}"))
            continue
        if declared.size_in_bytes is not None:
            total = sum(h.current.length_by_snapshot_name.get(snapshot_name, 0)
                        for h in read)
            if total != declared.size_in_bytes:
                _drop_indices(histories, dropped, {index_uuid}, Doubt(
                    EXTENT_SIZE,
                    f"snapshot {snapshot_name!r} declares "
                    f"{declared.size_in_bytes} bytes for index {index_name!r} "
                    f"and the file lists this run read add up to {total}"))

    if extent.total_shards is not None and total_read != extent.total_shards:
        _drop_indices(histories, dropped, touched, Doubt(
            EXTENT_TOTAL_SHARDS,
            f"snapshot {snapshot_name!r} declares {extent.total_shards} shard(s) "
            f"in total and this run read {total_read}"))


def _shards_naming(histories: Dict[ShardLocation, ShardHistory],
                   index_uuid: str, snapshot_name: str) -> List[ShardHistory]:
    return [history for location, history in histories.items()
            if location.index_uuid == index_uuid
            and snapshot_name in history.current.by_snapshot_name]


def _drop_indices(histories: Dict[ShardLocation, ShardHistory],
                  dropped: Dict[str, Doubt], index_uuids: Set[str],
                  why: Doubt) -> None:
    for location in [l for l in list(histories) if l.index_uuid in index_uuids]:
        dropped[location.directory] = why
        histories.pop(location, None)


def _names_covering(chain: Chain, generation: int, index_uuid: str) -> Set[str]:
    """The snapshots one generation says cover ONE index, by name.

    Index-scoped rather than generation-scoped, and that distinction is not
    cosmetic. A shard document holds the file lists of the snapshots covering
    ITS index, so comparing it against every snapshot name in the catalog
    rejects the document of any index a snapshot did not cover.

    The first cut of this rewrite compared against the whole catalog and the
    neuter sweep found the consequence: in a repository where snapshots cover
    different indices, every era document of a partly-covered index was
    rejected, so no delete could be attributed there. It costs coverage rather
    than data, which is why the suite stayed green and why the sweep, not a
    test, is what surfaced it.
    """
    root = chain.generations.get(generation)
    if root is None:
        return set()
    entry = root.index_by_uuid(index_uuid)
    if entry is None:
        return set()
    return {root.snapshots[uuid].name for uuid in entry.snapshot_uuids
            if uuid in root.snapshots}


def _shard_generation_ids(
        chain: Chain) -> Dict[ShardLocation, Dict[int, Optional[str]]]:
    """Every (index, shard) the chain names, and its id in each generation."""
    out: Dict[ShardLocation, Dict[int, Optional[str]]] = {}
    for generation, root in sorted(chain.generations.items()):
        for index_uuid, entry in root.indices.items():
            for shard in range(len(entry.shard_generations)):
                location = ShardLocation(index_uuid=index_uuid, shard=shard)
                out.setdefault(location, {})[generation] = \
                    entry.shard_generation(shard)
    return out


def _read(source: RepositorySource, location: ShardLocation,
          shard_generation: str, cache: Dict[str, Optional[ShardDocument]],
          tally: CommitOracleTally) -> Optional[ShardDocument]:
    """One shard document, or None when this run may not use it.

    `require_blob_names` runs here rather than at a call site, so there is no
    path through this package that obtains a shard document naming nothing. A
    document with no blob names satisfies the per-directory subset test against
    every directory in the repository, which is how one read turns into a live
    set for the wrong shard.

    `tally` records here too, once per key actually parsed rather than once
    per call site, which is why it happens before `require_blob_names`: the
    Lucene commit cross-check already ran during `parse_shard_snapshots`, and
    that stands whether or not this document goes on to fail a different,
    unrelated check.
    """
    key = f"{location.directory}/index-{shard_generation}"
    if key in cache:
        return cache[key]
    try:
        document = parse_shard_snapshots(source.fetch(key), key)
        tally.record(document)
        require_blob_names(document, key)
    except (SourceReadError, GenerationChainError):
        document = None
    cache[key] = document
    return document


class ShardDirectoryTooLarge(RunRefused):
    """One shard directory alone does not fit the memory this run may use.

    A `RunRefused`, so it lands where every other refusal lands and produces
    a coverage record rather than a traceback. Batching sizes a GROUP of
    shard directories to the budget, so a repository too large to hold at
    once no longer refuses; a single directory too large to hold even alone
    is a real limit, not a batching decision, so that refusal stays.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)
        self.needs_a_bigger_host = True


def plan_shard_batches(
        chain: Chain, keys: Iterable[str], budget_bytes: Optional[int]
) -> List[List[ShardLocation]]:
    """Group every shard directory the chain names to fit a memory budget.

    Without a budget, every directory goes into one group, which is the
    batched design's own one-batch case and what `survey_shards` runs when a
    caller passes no `groups` at all.

    The estimate reuses `RESIDENT_BYTES_PER_OBJECT`, the same figure
    `sources.budget` measured against this package's actual cost driver: one
    parsed shard document per shard directory per generation. Applied here
    per directory, scaled by how many distinct shard-generation documents
    that directory will be read at, it estimates the same quantity at the
    grain batching now controls.
    """
    wanted = _shard_generation_ids(chain)
    locations = sorted(wanted, key=_location_order)
    if budget_bytes is None or budget_bytes <= 0:
        return [locations] if locations else []
    present = _blobs_present(list(keys))
    costs = {location: _estimated_directory_bytes(
                present.get(location.directory, ()), wanted[location])
             for location in locations}
    oversized = [location for location in locations
                if costs[location] > budget_bytes]
    if oversized:
        worst = max(oversized, key=lambda location: costs[location])
        raise ShardDirectoryTooLarge(
            f"{worst.directory} alone needs about "
            f"{costs[worst] // (1 << 20)} MB to read, and only "
            f"{budget_bytes // (1 << 20)} MB is available to this run. "
            "Nothing was read. Run it on a host with more memory, or raise "
            "the ceiling with --max-ram if this host really has more than "
            "it reports")
    groups: List[List[ShardLocation]] = []
    group: List[ShardLocation] = []
    group_cost = 0
    for location in locations:
        cost = costs[location]
        if group and group_cost + cost > budget_bytes:
            groups.append(group)
            group, group_cost = [], 0
        group.append(location)
        group_cost += cost
    if group:
        groups.append(group)
    return groups


def _estimated_directory_bytes(
        present_stems: Iterable[str],
        per_generation: Dict[int, Optional[str]]) -> int:
    generations = len({sgi for sgi in per_generation.values()
                       if sgi is not None})
    objects = len(list(present_stems))
    return objects * max(generations, 1) * RESIDENT_BYTES_PER_OBJECT
