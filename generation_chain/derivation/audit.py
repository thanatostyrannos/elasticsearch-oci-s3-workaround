"""One run: read a repository through one source, emit what it can explain.

The order is short enough to state. Anchor the chain on the highest generation
that is ours, survey the shards and drop every one whose evidence is
incomplete, take Elasticsearch's own shard-local set difference, keep the part
of it some observed delete accounts for, and record everything none of that
could see. Every stage that fails records itself in coverage and contributes
nothing to the manifest.

WHY THE MANIFEST HAS ONE PRODUCER. `decide` returns the dispositions and the
manifest together, and `run_audit` has no statement that joins them and no
statement that filters one by the other. A reviewer deleted exactly such a
statement from the retired version with the whole suite green, which silently
unwired the Elasticsearch veto. A wiring that cannot be expressed as a
deletable line cannot be deleted.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional

from ..corroboration import Veto
from ..errors import RunRefused, SourceReadError
from ..model import AuditResult, Condemnation, Coverage, ShardLocation
from ..sources import RepositorySource, hint, prepared
from .chain import load_chain
from .classification import decide
from .garbage import (CATEGORY_SEGMENT, attribution_coverage,
                      condemn_repository_wide, condemn_segments,
                      segment_eligible_operations, unread_indices_of)
from .keys import KeyIndex
from .shards import ShardHistory, ShardSurvey, plan_shard_batches, survey_shards


def run_audit(source: RepositorySource, veto: Optional[Veto] = None,
             budget_bytes: Optional[int] = None,
             progress=None) -> AuditResult:
    """Derive the delete operations a repository's generation chain records.

    `veto` is what Elasticsearch says must not be touched, when the caller asked
    and got an answer. It is None when nobody asked. It is never an empty veto
    standing in for a call that failed, because a failed call refuses the run
    before it gets here.

    `budget_bytes` sizes how many shard directories `survey_shards` reads era
    documents for at once, and it never changes what the run finds. Without
    it, every shard directory the chain names is read in one group, which is
    also what happens when the whole repository fits comfortably inside
    whatever `budget_bytes` names: a batch boundary only decides when this
    process discards a parsed document, never which delete operation gets
    credited with a key.

    A REPOSITORY BEING WRITTEN WHILE THIS RUNS IS SAFE TO AUDIT, and that is
    worth stating because the instinct is to stop the writer first.

    Under churn, shard documents get replaced between the listing and the fetch.
    Every gate that notices reacts by DROPPING the shard, so a moving repository
    yields fewer names, never different ones. Measured on a rig taking a
    snapshot every fifteen seconds, coverage fell to 0 percent and the manifest
    was correspondingly short. The failure mode is a useless answer, not a
    dangerous one.

    Nothing is lost by that. A blob orphaned while this run was reading is still
    orphaned when the next run reads, because a delete that stranded it does not
    un-strand it. Missed orphans are picked up later; a wrongly named live key
    would not be recoverable at all. That asymmetry is why the run is allowed to
    come up short and is never allowed to guess.

    So pausing SLM before an audit buys completeness, not correctness. It is
    worth doing when you want one long list, and worth skipping when you would
    rather run often against a repository that never stops.
    """
    # A run against a real repository can read for twenty minutes and say
    # nothing, which leaves an operator unable to tell working from stuck.
    # `progress` is how it says something. It is
    # optional and defaults to silence, so nothing that embeds this changes
    # behaviour, and it never affects what the run finds.
    say = progress or (lambda message: None)

    notes: List[str] = []
    source = prepared(source)
    try:
        keys = source.list_keys()
    except RunRefused as exc:
        return _refused_by(exc, notes)
    except SourceReadError as exc:
        return _refused(f"cannot list the repository: {exc}", notes, True)
    say(f"listed {len(keys):,} objects")
    try:
        chain = load_chain(source, keys)
    except RunRefused as exc:
        return _refused_by(exc, notes)

    say(f"read the generation chain: {len(chain.generations):,} generation(s) "
        f"believed, current {chain.current_generation}")
    index = KeyIndex(keys, source)
    try:
        groups = plan_shard_batches(chain, keys, budget_bytes)
    except RunRefused as exc:
        # A single shard directory does not fit even alone, which is a real
        # limit rather than a batching decision. See `plan_shard_batches`.
        return _refused_by(exc, notes)

    say(f"reading {sum(len(g) for g in groups):,} shard directories in "
        f"{len(groups):,} group(s). This is the slow part, and it is quiet: "
        "one shard document per directory per generation, and nothing ever "
        "removes a generation")

    found: Dict[str, Condemnation] = {}
    _done = [0]
    era_names: Dict[ShardLocation, Dict[int, FrozenSet[str]]] = {}
    eligible_operations = segment_eligible_operations(chain, notes)

    def _condemn_and_release(locations: List[ShardLocation],
                             histories: Dict[ShardLocation, ShardHistory]
                             ) -> None:
        # Runs while this group's era documents still exist, and nowhere
        # else: `survey_shards` discards `history.documents` for every
        # location named here the moment this returns. Segment condemnation
        # is sound one group at a time because a segment edge is complete by
        # construction inside a single shard directory (see garbage.py); the
        # snapshot-name-only summary kept in `era_names` is what
        # `attribution_coverage` needs later, and it is cheap enough to hold
        # for the whole run because it never carries a blob name.
        _done[0] += len(locations)
        say(f"  shard directories read: {_done[0]:,}")
        group_survey = ShardSurvey(
            histories={location: histories[location] for location in locations},
            dropped={}, considered=0, retired={})
        condemn_segments(group_survey, eligible_operations, index, found)
        for location in locations:
            era_names[location] = {
                generation: frozenset(document.by_snapshot_name)
                for generation, document
                in histories[location].documents.items()}

    def _release_writer_uuid_collisions(
            locations: List[ShardLocation]) -> None:
        # `survey_shards` calls this once, after every group has been read
        # and the whole-run writer-uuid collision check has run against all
        # of them together. A location named here may already have had
        # segments condemned by `_condemn_and_release`, back when only its
        # own group's evidence was in, so that condemnation is taken back
        # here rather than left standing on a directory the run no longer
        # trusts.
        for location in locations:
            era_names.pop(location, None)
            prefix = f"{location.directory}/"
            for key in [k for k, c in found.items()
                       if c.category == CATEGORY_SEGMENT and k.startswith(prefix)]:
                del found[key]

    try:
        survey = survey_shards(source, chain, keys, index, notes,
                               groups=groups, on_group=_condemn_and_release,
                               on_collision=_release_writer_uuid_collisions)
    except RunRefused as exc:
        return _refused_by(exc, notes)

    notes.extend(chain.notes)
    for earlier, later in chain.mixed_transitions:
        notes.append(
            f"the step from generation {earlier} to {later} both adds and "
            "removes snapshots, which is not one Elasticsearch operation, so "
            "nothing was attributed to it")

    found.update(condemn_repository_wide(chain, index, notes,
                                         unread_indices_of(survey)))
    condemned = sorted(found.values(), key=lambda c: c.key)

    try:
        verdict = decide(chain, survey, keys, condemned, notes, veto)
    except RunRefused as exc:
        return _refused_by(exc, notes)

    operations_found, operations_attributed = attribution_coverage(
        chain, era_names)
    coverage = Coverage(
        corroborated_by=veto.endpoint if veto is not None else None,
        repository_uuid=chain.repository_uuid,
        current_generation=chain.current_generation,
        latest_generation=chain.latest_generation,
        anchored_by=chain.anchored_by,
        generations_present=chain.present,
        generations_usable=chain.usable,
        generations_rejected=dict(chain.rejected),
        generations_missing=chain.missing,
        transitions_total=chain.transitions_total,
        transitions_explained=len(chain.adjacent_pairs),
        transitions_mixed=len(chain.mixed_transitions),
        operations_found=operations_found,
        operations_attributed=operations_attributed,
        shards_considered=survey.considered,
        shards_dropped=dict(survey.dropped),
        shards_retired=dict(survey.retired),
        shards_partly_read={
            history.location.directory: sorted(
                str(doubt) for doubt in history.unreadable.values())
            for history in survey.histories.values() if history.unreadable},
        existence_unanswered=tuple(index.unanswered),
        commit_oracle_checked=survey.commit_oracle_checked,
        commit_oracle_skipped=survey.commit_oracle_skipped,
        notes=notes,
    )
    return AuditResult(condemned=verdict.manifest, coverage=coverage,
                       classification=verdict.placements)


def _refused(why: str, notes: List[str],
             transient: bool = False) -> AuditResult:
    """A run that cannot be anchored explains nothing and says so.

    The manifest is empty and coverage carries the refusal, so an operator
    cannot read the empty manifest as a clean repository.
    """
    return AuditResult(
        condemned=[],
        coverage=Coverage(refused=why, refusal_is_transient=transient,
                          notes=notes))


def _refused_by(exc: RunRefused, notes: List[str]) -> AuditResult:
    """The `_refused` case where a caught exception already carries the facts.

    `RunRefused` knows both whether it is worth retrying and whether the host
    is the problem; this is the one place that reads `needs_a_bigger_host` off
    it, so the call sites above stay two-argument calls to this rather than
    `_refused` growing a fourth parameter.
    """
    result = _refused(str(exc), notes, exc.transient)
    result.coverage.refusal_needs_a_bigger_host = exc.needs_a_bigger_host
    return result
