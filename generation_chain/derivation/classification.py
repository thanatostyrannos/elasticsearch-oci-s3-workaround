"""A disposition for every key the store holds, and the manifest that implies.

Elasticsearch's delete removes more than the segments of the snapshot being
deleted. It also removes the superseded root generations and the superseded
shard generation documents, and this tool names NEITHER, because those are the
evidence its derivation reads. A tool that condemned its own inputs would work
exactly once. That silence is structural, so it is stated here rather than left
to be noticed: those keys are filed as evidence. If they simply went missing
from the manifest they would land in whichever bucket an operator uses for "the
chain cannot explain this", which is the bucket that is supposed to mean
something is wrong.

ONE FUNCTION PRODUCES BOTH VIEWS. `decide` returns the dispositions AND the
manifest AND the veto's effect on both. That is deliberate. The retired design
computed them separately and joined them with a statement in `run_audit`, and a
reviewer deleted that statement with the whole suite still green: it was the
only thing carrying the veto into the manifest and nothing pinned the join.
There is no longer a joining statement to delete, because removing the call
removes the manifest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from ..errors import RunRefused
from ..model import Condemnation
from .chain import Chain
from .garbage import live_metadata_blobs
from .keys import PART_SUFFIX
from .shards import ShardSurvey

ORPHANED = "orphaned"
PROTECTED = "protected"
LIVE = "live"
EVIDENCE = "evidence"
UNEXPLAINED = "unexplained"
OUTSIDE_MODEL = "outside-model"

ROOT_GENERATION = re.compile(r"^index-(0|[1-9][0-9]*)$")
ROOT_DOCUMENT = re.compile(r"^(snap|meta)-([^/]+)\.dat$")
INDEX_METADATA = re.compile(r"^indices/([^/]+)/meta-(.+)\.dat$")
SHARD_PATH = re.compile(r"^indices/([^/]+)/(0|[1-9][0-9]*)/(.+)$")
SHARD_GENERATION = re.compile(r"^index-(.+)$")
SHARD_DOCUMENT = re.compile(r"^snap-(.+)\.dat$")
SEGMENT = re.compile(r"^(__[A-Za-z0-9_\-]+)$")


@dataclass(frozen=True)
class Placement:
    """One key, where this run put it, and why."""

    key: str
    disposition: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    """The finished answer: every key placed, and the manifest that follows.

    A key that is both condemned and found live never reaches here, because
    `decide` refuses first. See `_refuse_a_contradiction`.
    """

    placements: List[Placement]
    manifest: List[Condemnation]


def decide(chain: Chain, survey: ShardSurvey, keys: Iterable[str],
           condemned: List[Condemnation], notes: List[str],
           veto=None) -> Verdict:
    """Sort every key, apply the veto, and say which keys stay condemned.

    `veto` is what Elasticsearch says must not be touched, when the caller asked
    and got an answer. It SUBTRACTS and only subtracts: it is never consulted
    while deciding what to condemn, so it cannot make the manifest longer.
    """
    orphans = {c.key: c for c in condemned}
    live_metadata = live_metadata_blobs(chain.final, [])
    current_shard_generation = _current_shard_generations(chain)
    live_blobs = {location.directory: history.live_blobs
                  for location, history in survey.histories.items()}
    surviving = set(chain.final.snapshots)
    protected = {key for key in orphans
                 if veto is not None and veto.covers(orphans[key])}

    placements: List[Placement] = []
    contradicted: List[str] = []
    for key in sorted(set(keys)):
        disposition, detail = _place(
            key, chain, surviving, current_shard_generation, live_blobs,
            live_metadata, survey)
        if disposition == LIVE and key in orphans:
            contradicted.append(key)
        elif key in protected:
            disposition = PROTECTED
            detail = f"Elasticsearch at {veto.endpoint} still references this"
        elif key in orphans:
            disposition, detail = ORPHANED, orphans[key].reason
        placements.append(Placement(key=key, disposition=disposition,
                                    detail=detail))

    if veto is not None:
        notes.append(
            f"Elasticsearch at {veto.endpoint} reported "
            f"{veto.snapshots_reported} snapshot(s) and "
            f"{len(veto.mounted_indices)} mounted searchable-snapshot "
            f"index(es); {len(protected)} key(s) left the manifest because it "
            "protects them")
    _refuse_a_contradiction(contradicted)
    kept = {p.key for p in placements if p.disposition == ORPHANED}
    return Verdict(placements=placements,
                   manifest=[c for c in condemned if c.key in kept])


def _refuse_a_contradiction(contradicted: List[str]) -> None:
    """A key both condemned and found live means the derivation is broken.

    The dispositions and the manifest are two readings of ONE live set, so this
    cannot happen unless a refactor has broken something. Shipping the rest of
    the manifest anyway would hand an operator a list produced by code that
    contradicts itself, so the run explains nothing and names the keys.

    IT RAISES RATHER THAN REPORTING, and the shape is the point. An earlier
    version returned the keys and left `run_audit` to write
    `if verdict.contradicted: return _refused(...)`. Nothing reaches that branch
    through `run_audit`, because there is only one subtraction in the package,
    so no test could pin the line and the neuter sweep found it unpinned. A
    reviewer deleted a statement of exactly that shape from the retired package
    with the whole suite green. Raising removes the line instead of adding a
    test for it: `run_audit` already turns RunRefused into a refusal for every
    other stage, so there is no wiring left to delete.
    """
    if contradicted:
        raise RunRefused(
            "the derivation contradicted itself: "
            + ", ".join(sorted(contradicted))
            + " were condemned and also found live. No manifest is produced "
            "from a derivation whose two readings of the live set disagree")


def _current_shard_generations(chain: Chain) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for index_uuid, entry in chain.final.indices.items():
        for shard, generation in enumerate(entry.shard_generations):
            out[f"indices/{index_uuid}/{shard}"] = generation
    return out


def _place(key: str, chain: Chain, surviving: Set[str],
           current_shard_generation: Dict[str, Optional[str]],
           live_blobs: Dict[str, FrozenSet[str]],
           live_metadata: Optional[Dict[str, Set[str]]],
           survey: ShardSurvey) -> Tuple[str, str]:
    if key == "index.latest":
        return LIVE, "the pointer to the current root generation"

    match = ROOT_GENERATION.match(key)
    if match:
        return _place_root_generation(int(match.group(1)), chain)

    match = ROOT_DOCUMENT.match(key)
    if match:
        return _place_root_document(match.group(1), match.group(2), surviving)

    match = INDEX_METADATA.match(key)
    if match:
        return _place_index_metadata(match.group(1), match.group(2),
                                     live_metadata)

    match = SHARD_PATH.match(key)
    if match:
        return _place_in_shard(
            f"indices/{match.group(1)}/{match.group(2)}", match.group(3),
            surviving, current_shard_generation, live_blobs, survey)

    return OUTSIDE_MODEL, "not an object this tool models"


def _place_root_generation(generation: int, chain: Chain) -> Tuple[str, str]:
    """Where one `index-<n>` sits against the generation this run used."""
    if generation == chain.current_generation:
        return LIVE, "the current root generation"
    if generation < chain.current_generation:
        return EVIDENCE, (
            "a superseded root generation; Elasticsearch's delete removes "
            "these and this tool will not, because the derivation reads "
            "them to learn what a delete removed")
    return UNEXPLAINED, (
        f"names a generation above {chain.current_generation}, which this "
        "run anchored on")


def _place_root_document(kind: str, uuid: str,
                         surviving: Set[str]) -> Tuple[str, str]:
    """Whether the snapshot a `snap-` or `meta-` document names is live."""
    what = "snapshot" if kind == "snap" else "global metadata"
    if uuid in surviving:
        return LIVE, f"the {what} document of a live snapshot"
    return UNEXPLAINED, (
        f"a {what} document for snapshot {uuid}, which no generation this "
        "run could read names")


def _place_index_metadata(index_uuid: str, blob_id: str,
                          live_metadata: Optional[Dict[str, Set[str]]]
                          ) -> Tuple[str, str]:
    """Whether a live snapshot still references one index metadata blob.

    A None live set is no answer rather than an empty one, so the blob is
    unexplained instead of being read as unreferenced.
    """
    if live_metadata is None:
        return UNEXPLAINED, (
            "index metadata, and this run established no live set for "
            "index metadata")
    if blob_id in live_metadata.get(index_uuid, set()):
        return LIVE, "index metadata a live snapshot references"
    return UNEXPLAINED, "index metadata no live snapshot references"


def _place_in_shard(directory: str, name: str, surviving: Set[str],
                    current_shard_generation: Dict[str, Optional[str]],
                    live_blobs: Dict[str, FrozenSet[str]],
                    survey: ShardSurvey) -> Tuple[str, str]:
    match = SHARD_GENERATION.match(name)
    if match:
        if current_shard_generation.get(directory) == match.group(1):
            return LIVE, "the current shard generation document"
        return EVIDENCE, (
            "a superseded shard generation document; Elasticsearch's delete "
            "removes these and this tool will not, because the derivation "
            "reads them for the file lists of earlier eras")

    match = SHARD_DOCUMENT.match(name)
    if match:
        if match.group(1) in surviving:
            return LIVE, "the shard document of a live snapshot"
        return UNEXPLAINED, (
            f"a shard document for snapshot {match.group(1)}, which no "
            "generation this run could read names")

    base = PART_SUFFIX.sub("", name)
    if SEGMENT.match(base):
        if directory in survey.dropped:
            return UNEXPLAINED, (
                f"in a shard this run dropped: {survey.dropped[directory]}")
        if directory in survey.retired:
            return UNEXPLAINED, str(survey.retired[directory])
        if base in live_blobs.get(directory, frozenset()):
            return LIVE, "a segment the current shard document names"
        if directory not in live_blobs:
            return UNEXPLAINED, "in a shard no generation of this chain names"
        return UNEXPLAINED, (
            "a segment Elasticsearch's own set difference would collect and "
            "no readable file list attributes to a delete this run observed")

    return OUTSIDE_MODEL, "not an object this tool models"
