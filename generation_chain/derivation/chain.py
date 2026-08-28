"""Which root generations this run is allowed to believe, and which is current.

ANCHOR ON THE HIGHEST, THE WAY ELASTICSEARCH DOES. Its own package
documentation states the order: "First, find the most recent RepositoryData by
getting a list of all index-N blobs through listing all blobs with prefix
'index-' under the repository root and then selecting the one with the highest
value for N", and only "if listing fails: read the highest value of N from the
index.latest blob". Listing is primary. `index.latest` is the fallback.

The retired version of this module had that backwards and it cost live data. A
repository left by an ordinary crash between writing `index-N+1` and updating
`index.latest` was confirmed on the rig to make the tool name TWO LIVE KEYS. No
store misbehaved and nothing was tampered with. Anchoring low means measuring
today's blobs against yesterday's live set, so every segment the newer
generation added looks like garbage.

WHAT `index.latest` IS STILL FOR. It is the one statement in the repository
about WHICH repository this is that does not depend on guessing. Reading it
gives a generation whose document declares the repository uuid, and the anchor
is then the highest listed generation carrying that same uuid. So `index.latest`
establishes identity and the listing establishes currency, which is each input
doing the thing it is actually evidence for.

A DISAGREEMENT IS REPORTED, NOT ABORTED OVER. An earlier design treated "a root
generation newer than index.latest exists" as a safety guard and aborted. That
is a wrong anchor dressed up as a safety property, and keeping it would teach
the next reader to reproduce the defect. The higher generation is the current
one; the run says so and carries on.

WHAT DOES REFUSE. A generation ABOVE the anchor that this run cannot read at
all. Unreadable means its uuid is unknown, so it might be ours, and if it is
ours it is the current one. Anchoring below a generation that might be current
is precisely the defect above, so the run explains nothing instead. That rule
is not the abort it replaces: it fires on an unreadable blob rather than on a
readable one, and it never prefers the lower generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..errors import (GenerationChainError, RunRefused, SourceReadError,
                      UnsupportedRepository)
from ..formats.latest import INDEX_LATEST_KEY, parse_index_latest
from ..formats.repository_data import (parse_repository_data,
                                       root_generation_number)
from ..model import RootGeneration
from ..sources import RepositorySource, hint

BY_LISTING = "listing"
BY_INDEX_LATEST = "index.latest"


@dataclass
class Chain:
    """The generations this run believes, and everything it could not use."""

    current_generation: int
    repository_uuid: str
    generations: Dict[int, RootGeneration]
    present: Tuple[int, ...]
    # What `index.latest` said, kept beside the anchor so a report and a test
    # can both see a disagreement rather than have to infer it.
    latest_generation: Optional[int] = None
    anchored_by: str = BY_INDEX_LATEST
    rejected: Dict[int, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def anchor_disagrees_with_index_latest(self) -> bool:
        return (self.latest_generation is not None
                and self.latest_generation != self.current_generation)

    @property
    def usable(self) -> Tuple[int, ...]:
        return tuple(sorted(self.generations))

    @property
    def missing(self) -> Tuple[int, ...]:
        """Generations of this repository's history the run cannot see."""
        return tuple(n for n in range(0, self.current_generation + 1)
                     if n not in self.generations)

    @property
    def transitions_total(self) -> int:
        """Every generation step this repository has taken since generation 0.

        Generation 0 is the first catalog Elasticsearch writes, so a repository
        at generation N has taken N steps and each could have carried a delete.
        """
        return self.current_generation

    @property
    def adjacent_pairs(self) -> List[Tuple[int, int]]:
        """Steps this run can explain: both ends read, and nothing in between.

        Only adjacent generations are compared. Across a gap the difference
        between two catalogs is the sum of several operations the run never
        saw, and crediting that difference to one imagined operation would name
        keys on the strength of history nobody read.

        A step that both ADDS and REMOVES snapshots is left out. Elasticsearch
        writes a root generation for one operation: a snapshot finishes, or a
        delete completes. A step that appears to do both is not one operation,
        so there is no single delete to attribute anything to.

        This is also the only structural defence against a store that answers a
        generation key with a DIFFERENT generation of the same repository.
        Nothing inside a RepositoryData names its own generation number, so that
        swap is invisible to any check on the bytes. What it cannot hide is that
        it makes the step into that generation add back snapshots the chain had
        already moved past. Leaving the step out costs whatever that step
        carried, which is a shorter manifest and never a longer one.
        """
        return [pair for pair, mixed in self._steps() if not mixed]

    @property
    def mixed_transitions(self) -> List[Tuple[int, int]]:
        """Steps that add and remove at once, so this run interprets neither."""
        return [pair for pair, mixed in self._steps() if mixed]

    def _steps(self) -> List[Tuple[Tuple[int, int], bool]]:
        out: List[Tuple[Tuple[int, int], bool]] = []
        for n in range(self.current_generation):
            if n not in self.generations or n + 1 not in self.generations:
                continue
            before = set(self.generations[n].snapshots)
            after = set(self.generations[n + 1].snapshots)
            out.append(((n, n + 1),
                        bool(after - before) and bool(before - after)))
        return out

    @property
    def final(self) -> RootGeneration:
        return self.generations[self.current_generation]


def load_chain(source: RepositorySource, keys: List[str]) -> Chain:
    """Read every root generation blob and decide which ones count."""
    present = sorted({n for n in (root_generation_number(k) for k in keys)
                      if n is not None})
    latest = _index_latest(source)
    identity = _read_generation(source, latest)
    repository_uuid = identity.repository_uuid
    if repository_uuid is None:
        # A generation with no uuid field states no opinion about which
        # repository it belongs to. With the identity generation stating none,
        # no other generation blob in the bucket can be tied to this
        # repository, so the run explains nothing rather than assuming a match.
        raise RunRefused(
            f"generation {latest} carries no repository uuid, so no other "
            "generation blob can be attributed to this repository")

    notes: List[str] = []
    rejected: Dict[int, str] = {}
    current, anchor, anchored_by = _highest_ours(
        source, present, latest, identity, repository_uuid, rejected, notes)

    if not anchor.snapshots:
        # A catalog with no live snapshots leaves no live set to measure any
        # file list against, so every blob in the repository becomes
        # condemnable off this one document. That is the largest manifest this
        # tool could produce and the state where one misread costs most, so it
        # produces none. The reachability sweeper already handles a repository
        # whose last snapshot is gone.
        raise RunRefused(
            f"generation {current} names no live snapshots, so there is no "
            "live set to measure file lists against")

    below_anchor = [n for n in present if n < current and n not in rejected]
    # Every generation below the anchor gets read, in this order. Saying so
    # lets the transport overlap the round trips; it changes nothing about
    # which blobs are read or what happens when one of them fails.
    hint(source, [f"index-{n}" for n in below_anchor])
    generations: Dict[int, RootGeneration] = {current: anchor}
    generations.update(
        _read_below_anchor(source, below_anchor, repository_uuid, rejected))

    if latest not in present:
        notes.append(
            f"{INDEX_LATEST_KEY} names generation {latest} and the listing "
            "does not show it; that generation was read by key")
    return Chain(current_generation=current, repository_uuid=repository_uuid,
                 generations=generations, present=tuple(present),
                 latest_generation=latest, anchored_by=anchored_by,
                 rejected=rejected, notes=notes)


def _read_below_anchor(source: RepositorySource, numbers: List[int],
                       repository_uuid: str,
                       rejected: Dict[int, str]) -> Dict[int, RootGeneration]:
    """The generations under the anchor this run can read and can claim.

    Anything else is recorded in `rejected` and contributes nothing, which is
    always the safe direction: a generation left out is a delete operation
    this run does not interpret, so the manifest gets shorter.
    """
    out: Dict[int, RootGeneration] = {}
    for number in numbers:
        try:
            parsed = parse_repository_data(
                source.fetch(f"index-{number}"), number)
        except UnsupportedRepository as exc:
            # An older generation below the format floor is dropped rather than
            # taking the run down with it. Evidence this tool cannot read is
            # evidence it does not use: the manifest gets shorter and the
            # coverage report says how much shorter. The anchor is the one
            # generation that cannot be dropped, because everything is measured
            # against it.
            rejected[number] = str(exc)
        except (SourceReadError, GenerationChainError) as exc:
            rejected[number] = str(exc)
        else:
            refusal = _why_not_ours(parsed, repository_uuid)
            if refusal is None:
                out[number] = parsed
            else:
                rejected[number] = refusal
    return out


def _why_not_ours(parsed: RootGeneration,
                  repository_uuid: str) -> Optional[str]:
    """Why this generation is not attributed to our repository, or None.

    No uuid at all is no opinion rather than evidence of a match, so it is
    turned away on the same footing as a uuid naming somebody else.
    """
    if parsed.repository_uuid is None:
        return ("carries no repository uuid, which is no opinion rather "
                "than evidence, so it is not attributed to this repository")
    if parsed.repository_uuid != repository_uuid:
        return (f"belongs to repository {parsed.repository_uuid}, not "
                f"{repository_uuid}")
    return None


def _highest_ours(source: RepositorySource, present: List[int], latest: int,
                  identity: RootGeneration, repository_uuid: str,
                  rejected: Dict[int, str], notes: List[str]):
    """The highest listed generation carrying our uuid, walking down from the top.

    Elasticsearch takes the highest. This takes the highest that is OURS,
    because a bucket can hold a co-tenant's generation blobs and their
    numbering says nothing about ours. A generation above the anchor that this
    run cannot read leaves its ownership unknown, and an unknown generation
    above the anchor might be the current one, so the run refuses rather than
    quietly anchoring lower.
    """
    for number in sorted((n for n in present if n > latest), reverse=True):
        key = f"index-{number}"
        try:
            parsed = parse_repository_data(source.fetch(key), number)
        except SourceReadError as exc:
            raise RunRefused(
                f"{key} is listed above the generation {INDEX_LATEST_KEY} "
                f"names and could not be read ({exc}), so this run cannot tell "
                "whether it is this repository's current generation",
                transient=True) from exc
        except GenerationChainError as exc:
            raise RunRefused(
                f"{key} is listed above the generation {INDEX_LATEST_KEY} "
                f"names and could not be read ({exc}), so this run cannot tell "
                "whether it is this repository's current generation") from exc
        if parsed.repository_uuid != repository_uuid:
            rejected[number] = (
                f"belongs to repository {parsed.repository_uuid}, not "
                f"{repository_uuid}")
            continue
        notes.append(
            f"the listing holds generation {number} and {INDEX_LATEST_KEY} "
            f"names {latest}; Elasticsearch anchors on the highest generation "
            f"it can list, so this run used {number}. A repository left by a "
            f"crash between writing index-{number} and updating "
            f"{INDEX_LATEST_KEY} looks exactly like this")
        return number, parsed, BY_LISTING
    return latest, identity, BY_INDEX_LATEST


def _index_latest(source: RepositorySource) -> int:
    try:
        return parse_index_latest(source.fetch(INDEX_LATEST_KEY))
    except SourceReadError as exc:
        raise RunRefused(f"cannot read {INDEX_LATEST_KEY}: {exc}",
                         transient=True) from exc
    except GenerationChainError as exc:
        raise RunRefused(f"cannot read {INDEX_LATEST_KEY}: {exc}") from exc


def _read_generation(source: RepositorySource,
                     generation: int) -> RootGeneration:
    try:
        return parse_repository_data(
            source.fetch(f"index-{generation}"), generation)
    except UnsupportedRepository as exc:
        raise RunRefused(str(exc)) from exc
    except SourceReadError as exc:
        raise RunRefused(
            f"cannot read generation index-{generation}: {exc}",
            transient=True) from exc
    except GenerationChainError as exc:
        raise RunRefused(
            f"cannot read generation index-{generation}: {exc}") from exc
