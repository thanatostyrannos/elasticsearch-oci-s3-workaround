"""What the run could and could not see, said where nobody can miss it.

An operator reading a short manifest has to be able to tell "there is little
to clean up" from "I could not see most of this repository". Those look
identical without these numbers, and mistaking the second for the first is how
a leak gets signed off as clean.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, TextIO

from ..derivation.classification import Placement
from ..model import AuditResult


def as_document(result: AuditResult, transport: str,
                location: str) -> Dict[str, Any]:
    """The machine-readable record, to keep as evidence and for a later differential."""
    coverage = result.coverage
    return {
        "transport": transport,
        "location": location,
        "refused": coverage.refused,
        "corroborated_by": coverage.corroborated_by,
        "repository_uuid": coverage.repository_uuid,
        "repository_uuid_is_unassigned": coverage.repository_uuid_is_unassigned,
        "current_generation": coverage.current_generation,
        "generations_present": list(coverage.generations_present),
        "generations_usable": list(coverage.generations_usable),
        "generations_missing": list(coverage.generations_missing),
        "generations_rejected": {str(k): v
                                 for k, v in coverage.generations_rejected.items()},
        "transitions_total": coverage.transitions_total,
        "transitions_explained": coverage.transitions_explained,
        "transitions_mixed": coverage.transitions_mixed,
        "operations_found": coverage.operations_found,
        "operations_attributed": coverage.operations_attributed,
        "explained_fraction": coverage.explained_fraction,
        "shards_considered": coverage.shards_considered,
        "shards_dropped": {k: str(v) for k, v in coverage.shards_dropped.items()},
        "shards_retired": {k: str(v) for k, v in coverage.shards_retired.items()},
        "latest_generation": coverage.latest_generation,
        "anchored_by": coverage.anchored_by,
        "existence_unanswered": list(coverage.existence_unanswered),
        "commit_oracle_checked": coverage.commit_oracle_checked,
        "commit_oracle_skipped": coverage.commit_oracle_skipped,
        "dispositions": dict(_dispositions(result.classification)),
        "condemned": len(result.condemned),
        "notes": list(coverage.notes),
    }


def _dispositions(placements: List[Placement]) -> Counter:
    return Counter(placement.disposition for placement in placements)


def write_report(result: AuditResult, transport: str, location: str,
                 stream: TextIO, sizes=None) -> None:
    """The human report. Coverage first, because it qualifies everything else."""
    coverage = result.coverage
    def write(line: str = "") -> None:
        stream.write(line + "\n")

    sizes = sizes or {}
    write(f"transport: {transport}, {location}")
    if coverage.refused:
        write(f"REFUSED: {coverage.refused}")
        write("This run explains nothing. An empty manifest here is not "
              "evidence that the repository is clean.")
        return

    uuid = coverage.repository_uuid
    write(f"repository uuid: {uuid}")
    if coverage.repository_uuid_is_unassigned:
        write("  This repository has never been assigned a uuid, so matching "
              "it separates nothing. Generation blobs from a co-tenant "
              "sharing this bucket would match too.")
    else:
        write("  The uuid is a field whoever wrote the blob controls. It "
              "separates tenants sharing a bucket. It is not proof of "
              "authorship.")

    write()
    write("Coverage")
    write(f"  current root generation: {coverage.current_generation}")
    if coverage.latest_generation != coverage.current_generation:
        write(f"  index.latest names generation {coverage.latest_generation}, "
              f"and the listing holds {coverage.current_generation}. "
              "Elasticsearch anchors on the highest generation it can list, so "
              "this run used the higher one.")
        write("    A repository left by a crash between writing the newer "
              "generation and updating index.latest looks exactly like this. "
              "Seeing it repeatedly is worth chasing.")
    write(f"  generations read and believed: "
          f"{_numbers(coverage.generations_usable)}")
    write(f"  generations missing from the chain: "
          f"{_numbers(coverage.generations_missing)}")
    for generation, why in sorted(coverage.generations_rejected.items()):
        write(f"    generation {generation} was not used: {why}")
    fraction = coverage.explained_fraction
    percent = "n/a" if fraction is None else f"{fraction * 100:.0f}%"
    write(f"  history this run can explain: {percent}")
    write(f"    delete operations whose file lists it attributed in full: "
          f"{coverage.operations_attributed} of {coverage.operations_found} "
          f"found in the chain")
    write(f"    generation transitions it could read both ends of: "
          f"{coverage.transitions_explained} of {coverage.transitions_total}")
    write(f"  shard directories read: "
          f"{coverage.shards_considered - len(coverage.shards_dropped)} of "
          f"{coverage.shards_considered}")
    for where, why in sorted(coverage.shards_dropped.items()):
        write(f"    {where} was dropped whole: {why}")
    if coverage.shards_retired:
        write(f"  shard directories of indices no live snapshot references: "
              f"{len(coverage.shards_retired)}")
        write("    Their blobs are reported as unexplained rather than "
              "condemned, because this run established no live set there.")
    if coverage.existence_unanswered:
        write(f"  keys the store could neither confirm nor deny: "
              f"{len(coverage.existence_unanswered)}")
        write("    Each one was left OUT of the manifest. This is not a count "
              "of keys that are gone, it is a count of questions that went "
              "unanswered.")
    commit_oracle_seen = (coverage.commit_oracle_checked
                         + coverage.commit_oracle_skipped)
    if commit_oracle_seen:
        write(f"  Lucene commit cross-check: ran on "
              f"{coverage.commit_oracle_checked} of {commit_oracle_seen} "
              "snapshot file lists")
        if coverage.commit_oracle_skipped:
            write(f"    {coverage.commit_oracle_skipped} carried no inline "
                  "commit to compare against, so this run's independent "
                  "check on drift between the file list and what Lucene "
                  "needs did not run for them. They still passed the older "
                  "presence-only gate.")

    if (coverage.transitions_explained < coverage.transitions_total
            or coverage.operations_attributed < coverage.operations_found
            or coverage.shards_dropped):
        write()
        write("  Blobs orphaned by the operations above do NOT appear in the "
              "manifest. A key absent from it is not evidence that the key is "
              "live.")

    write()
    if coverage.corroborated_by:
        write(f"Elasticsearch corroboration: CHECKED against "
              f"{coverage.corroborated_by}")
        write("  Everything it reported was removed from the manifest. What it "
              "did not report was not thereby condemned.")
        write("  It protects by snapshot identity, so it cannot catch a key "
              "this tool attributed to the wrong snapshot. Corroboration is "
              "not a check on the derivation.")
    else:
        write("Elasticsearch corroboration: NOT CHECKED")
        write("  Nothing in this run established whether a mounted "
              "searchable-snapshot index depends on the keys below. That "
              "linkage lives in cluster state and repository metadata cannot "
              "see it. Pass --elasticsearch and --es-repository to ask.")
    write()
    write("Dispositions")
    counts = _dispositions(result.classification)
    measured = bytes_by_disposition(result.classification, sizes)
    for name in ("orphaned", "protected", "live", "evidence", "unexplained",
                 "outside-model"):
        n = counts.get(name, 0)
        _, total, unsized = measured.get(name, (0, 0, 0))
        if not sizes or n == 0:
            write(f"  {name}: {n}")
        elif unsized:
            write(f"  {name}: {n}, at least {human_bytes(total)} "
                  f"({unsized} without a size)")
        else:
            write(f"  {name}: {n}, {human_bytes(total)}")
    if sizes:
        write("  Only `orphaned` is a list of things to delete. The sizes beside "
              "the others are there because the manifest is not the size of the "
              "leak: a run that drops its shard directories condemns no segment "
              "blob, and the segments it could not attribute are counted under "
              "`unexplained` instead.")
        write("  `unexplained` is not known garbage. It is what this run could "
              "not decide either way, and some of it is live.")
    write("  Orphaned keys are a SUBSET of what Elasticsearch's own delete "
          "collects. Its rule is the segment blobs in a shard directory that "
          "the current BlobStoreIndexShardSnapshots does not reference. This "
          "tool computes that set difference and then keeps only the members "
          "it can attribute to a delete operation it actually observed.")
    write("  An unexplained key is not a key to delete. Some of them, the "
          "partial blob an aborted snapshot leaves behind for one, are named "
          "by nothing and Elasticsearch will never reclaim them either.")
    write("  Elasticsearch's own delete also removes the superseded root "
          "generations and the superseded shard generation documents. This "
          "tool never names either, because its derivation reads them. They "
          "are counted as evidence rather than left silently out.")

    write()
    write("Reclaimable")
    condemned_keys = result.keys
    total, unsized = reclaimable(condemned_keys, sizes)
    if not sizes:
        write(f"  {len(condemned_keys)} orphaned objects, no sizes available "
              "from this transport, so this run cannot say what they occupy.")
    elif unsized:
        write(f"  at least {human_bytes(total)} across {len(condemned_keys)} "
              f"orphaned objects")
        write(f"    A floor, not a total: {unsized} of them came back from the "
              "listing without a size, and an object with no size is counted "
              "as unknown rather than as zero.")
    else:
        write(f"  {human_bytes(total)} across {len(condemned_keys)} orphaned "
              f"objects ({total:,} bytes)")
    write("  Stored object size, as the store reported it in the listing. That "
          "is what a delete gives back. The `length` recorded in shard metadata "
          "is a different number: it is per logical Lucene file and summed per "
          "snapshot, so it counts a file shared by two snapshots twice.")

    if coverage.notes:
        write()
        write("Notes")
        for note in coverage.notes:
            write(f"  {note}")


# Decimal units, not binary. Object stores bill and display in powers of ten,
# and an operator comparing this line against the bucket console should not have
# to reconcile 1.4 TiB with 1.5 TB and wonder which of the two is lying.
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")


def human_bytes(count: int) -> str:
    """A byte count at a scale a person can read, to three significant figures.

    Raises on a negative count. A negative total is a summing bug upstream, and
    "-4.0 GB" would read like a measurement of something rather than a defect.
    """
    if count < 0:
        raise ValueError(f"a byte count cannot be negative: {count}")
    if count < 1000:
        return f"{count} B"
    value = float(count)
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS[1:]:
        value /= 1000.0
        if value < 1000:
            break
    text = f"{value:.2f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return f"{text} {unit}"


def reclaimable(keys, sizes) -> tuple:
    """Bytes the named keys occupy, and how many of them had no size.

    A key the listing gave no size for is counted as unsized, never as zero.
    Folding it in as zero produces a total that is quietly too small while
    looking exact, and this number exists to be quoted.
    """
    total = 0
    unsized = 0
    for key in keys:
        size = sizes.get(key)
        if size is None:
            unsized += 1
        else:
            total += size
    return total, unsized


def bytes_by_disposition(placements, sizes) -> dict:
    """Per disposition: how many keys, how many bytes, how many had no size.

    The manifest is not the leak. A run that drops every shard directory
    condemns no segment blob, so its reclaimable figure can be metadata only
    while the segments it could not attribute sit unmeasured in another
    category. Reporting size per disposition is what stops that reading as the
    size of the problem.
    """
    out = {}
    for placement in placements:
        count, total, unsized = out.get(placement.disposition, (0, 0, 0))
        size = sizes.get(placement.key)
        if size is None:
            out[placement.disposition] = (count + 1, total, unsized + 1)
        else:
            out[placement.disposition] = (count + 1, total + size, unsized)
    return out


def _numbers(values) -> str:
    return ", ".join(str(v) for v in values) if values else "(none)"
