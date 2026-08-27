"""What the derivation carries between its stages.

These are records rather than behaviour on purpose. The parsing modules build
them out of bytes, the derivation modules read them, and nothing in either
direction gets to reach back into a store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

# Elasticsearch writes this literal when a repository has never been assigned
# a uuid. It is not a uuid and it separates nothing.
UUID_NOT_ASSIGNED = "_na_"


@dataclass(frozen=True)
class SnapshotRef:
    """One entry of a root generation's `snapshots` array."""

    uuid: str
    name: str
    metadata_lookup: Mapping[str, str]

    @property
    def index_uuids(self) -> Tuple[str, ...]:
        return tuple(self.metadata_lookup)


@dataclass(frozen=True)
class IndexEntry:
    """One entry of a root generation's `indices` map."""

    name: str
    uuid: str
    snapshot_uuids: Tuple[str, ...]
    shard_generations: Tuple[Optional[str], ...]

    def shard_generation(self, shard: int) -> Optional[str]:
        """The generation id for one shard, or None when there is not one.

        Elasticsearch writes null for a shard it has no generation for, and a
        shard index past the end of the array is a shard this generation never
        knew about. Both answer "no opinion", which the caller turns into a
        dropped shard rather than an empty live set.
        """
        if shard < 0 or shard >= len(self.shard_generations):
            return None
        return self.shard_generations[shard]


@dataclass(frozen=True)
class RootGeneration:
    """A parsed `index-<N>`, which is a complete RepositoryData document."""

    generation: int
    repository_uuid: Optional[str]
    snapshots: Mapping[str, SnapshotRef]
    indices: Mapping[str, IndexEntry]
    index_metadata_identifiers: Mapping[str, str]

    def index_by_uuid(self, index_uuid: str) -> Optional[IndexEntry]:
        return self.indices.get(index_uuid)


@dataclass(frozen=True)
class ShardDocument:
    """A parsed shard `index-<gen>`, which is a BlobStoreIndexShardSnapshots."""

    blob_names: FrozenSet[str]
    by_snapshot_name: Mapping[str, FrozenSet[str]]
    # Lucene's IndexWriter identity, per file entry. Measured on the rig: the
    # sets are DISJOINT between two shards of one index and SHARED between two
    # generations of one shard, which is exactly the discriminator the
    # snapshot-name set cannot provide.
    writer_uuids: FrozenSet[object] = frozenset()
    # Summed `length` per snapshot, to check against the size the snapshot's
    # own document declares.
    length_by_snapshot_name: Mapping[str, int] = field(default_factory=dict)
    # How many (snapshot, commit) pairs in this document issue #21's Lucene
    # commit cross-check actually compared, and how many it had no inline
    # bytes for and had to skip. See `Coverage.commit_oracle_checked` for why
    # this is counted at all rather than folded into a single pass/fail.
    commit_oracle_checked: int = 0
    commit_oracle_skipped: int = 0


@dataclass(frozen=True)
class ShardLocation:
    index_uuid: str
    shard: int

    @property
    def directory(self) -> str:
        return f"indices/{self.index_uuid}/{self.shard}"

    def __str__(self) -> str:
        return self.directory


@dataclass(frozen=True)
class DeleteOperation:
    """One snapshot leaving the catalog between two adjacent generations."""

    snapshot_uuid: str
    snapshot_name: str
    from_generation: int
    to_generation: int

    def describes(self, what: str) -> str:
        return (f"{what} by deletion of snapshot {self.snapshot_name} "
                f"({self.snapshot_uuid}) between generation "
                f"{self.from_generation} and {self.to_generation}")


@dataclass(frozen=True)
class Condemnation:
    """One key, and the delete operation that should have removed it."""

    key: str
    category: str
    reason: str
    snapshot_uuid: str
    snapshot_name: str
    from_generation: int
    to_generation: int


@dataclass
class Coverage:
    """What fraction of the repository's history this run could explain.

    An operator reading a short manifest has to be able to tell "there is
    little to clean up" from "I could not see most of this repository", and
    those two look identical without these numbers.
    """

    repository_uuid: Optional[str] = None
    current_generation: Optional[int] = None
    # What `index.latest` said, and which input the anchor came from. Kept
    # apart from `current_generation` because a disagreement between the two is
    # the signature of a crash between writing index-N+1 and updating
    # index.latest, and an operator has to be able to see it rather than infer
    # it from a note.
    latest_generation: Optional[int] = None
    anchored_by: Optional[str] = None
    generations_present: Tuple[int, ...] = ()
    generations_usable: Tuple[int, ...] = ()
    generations_rejected: Dict[int, str] = field(default_factory=dict)
    generations_missing: Tuple[int, ...] = ()
    transitions_total: int = 0
    transitions_explained: int = 0
    transitions_mixed: int = 0
    operations_found: int = 0
    operations_attributed: int = 0
    shards_considered: int = 0
    shards_dropped: Dict[str, str] = field(default_factory=dict)
    # Shard directories of an index no live snapshot references. Ordinary, and
    # kept apart from `shards_dropped` because that number is the one an
    # operator reads to decide whether a run went well, and every index anyone
    # ever deleted would otherwise inflate it.
    shards_retired: Dict[str, str] = field(default_factory=dict)
    shards_partly_read: Dict[str, List[str]] = field(default_factory=dict)
    # Keys the store could neither confirm nor deny. Reported separately from
    # the denials because folding the two together is the one measured place
    # where this tool's report was wrong rather than conservative: with HEAD
    # failing 1 in 1000, about 31 of 30,938 keys left the manifest while
    # coverage still claimed 100%.
    existence_unanswered: Tuple[str, ...] = ()
    # (Snapshot, commit) pairs issue #21's Lucene commit cross-check compared
    # against the file list, versus pairs it had no inline commit bytes for
    # and had to defer to the older presence-only gate. Counted separately
    # for the same reason `existence_unanswered` is counted separately from a
    # denial: folding "the gate did not run" into "the gate ran and found
    # nothing" was the one measured place this tool's report was wrong
    # rather than conservative, and a guard closing a P0 data-loss path gets
    # the same treatment rather than a second, quieter version of that bug.
    commit_oracle_checked: int = 0
    commit_oracle_skipped: int = 0
    notes: List[str] = field(default_factory=list)
    refused: Optional[str] = None
    refusal_is_transient: bool = False
    # Separate from refusal_is_transient because an operator or a scheduled
    # job needs to tell "retry this" from "run this somewhere bigger"; those
    # call for opposite action, and conflating them wastes either a host or a
    # retry budget.
    refusal_needs_a_bigger_host: bool = False
    # None means nobody asked Elasticsearch. It NEVER means the
    # question was asked and went unanswered; that refuses the run.
    corroborated_by: Optional[str] = None

    @property
    def repository_uuid_is_unassigned(self) -> bool:
        return self.repository_uuid == UUID_NOT_ASSIGNED

    @property
    def transition_fraction(self) -> Optional[float]:
        if self.transitions_total <= 0:
            return None
        return self.transitions_explained / self.transitions_total

    @property
    def attribution_fraction(self) -> Optional[float]:
        if self.operations_found <= 0:
            return None
        return self.operations_attributed / self.operations_found

    @property
    def explained_fraction(self) -> Optional[float]:
        """The LESS flattering of the two, because a headline gets scanned.

        A run that read every generation and could not attribute one of the
        deletes it found has not explained 100% of anything, and an operator
        skimming the first number must not be told that it has.
        """
        both = [f for f in (self.transition_fraction, self.attribution_fraction)
                if f is not None]
        return min(both) if both else None


@dataclass
class AuditResult:
    condemned: List[Condemnation]
    coverage: Coverage
    classification: List["Placement"] = field(default_factory=list)

    @property
    def keys(self) -> List[str]:
        return sorted({c.key for c in self.condemned})
