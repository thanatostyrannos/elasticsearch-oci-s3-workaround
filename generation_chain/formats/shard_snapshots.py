"""`indices/<index-uuid>/<shard>/index-<gen>`: a BlobStoreIndexShardSnapshots.

This document is the file list of one shard at one generation. It carries a
`files` array of FileInfo records and a `snapshots` object keyed by SNAPSHOT
NAME, whose `files` arrays hold blob names rather than physical Lucene names.
That was read off a captured repository rather than inferred, because getting
it backwards would attribute every blob to the wrong snapshot.

Renaming one field in this document once deleted 96.4% of a rig repository by
bytes. Everything below refuses on the smallest disagreement for that reason:
A DOCUMENT THAT YIELDS NO FILE LIST MUST RAISE, NEVER RETURN AN EMPTY ONE.

Three gates carry that, and all three were verified against the captured
9.5.2 repository rather than assumed:

  * A snapshot entry whose `files` array is EMPTY. A shard snapshot always
    carries at least its Lucene commit, so an empty list is a list that was
    not read. Every one of the twelve snapshot entries in the captured
    repository names four or more files.
  * A snapshot entry that names files and no `segments_N` commit among their
    physical names. Restoring a shard means restoring a Lucene commit, so a
    list without one cannot describe what it claims to. Real 9.5.2 stores that
    commit INLINE, as a `v__` entry, which is exactly where a drift that moved
    the inline entries would land. All twelve entries in the capture name one,
    with names from `segments_3` to `segments_t`, so the counter is base 36.
  * A snapshot entry whose commit point, once decoded, requires a segment
    that entry's own file list does not reference. This is issue #1's fix:
    the first two gates only ever asked whether `segments_N` was PRESENT BY
    NAME, never what it said. A tamper (or a genuine upstream format change)
    that removes the same live segment from both `index-<gen>` and
    `snap-<uuid>.dat`, keeping `segments_N` and patching the counts to match,
    satisfied both of the gates above and every check that compares those two
    Elasticsearch-owned copies to each other, because the two copies still
    agreed. `segments_N` is written by a different layer for a different
    reason and never round-trips through that agreement, so comparing what it
    requires against the file list this tool was handed catches the drift
    without asking Elasticsearch anything. Only blob-backed entries count as
    a segment being present: Elasticsearch stores every `.si` inline, so an
    inline entry is still there after a segment's blobs are gone. The limit
    that remains: a segment with more than one blob-backed file, which every
    compound segment is and every segment with deletion or doc-values update
    files is, still passes after losing all of its blobs but one. See
    `generation_chain/formats/lucene_segments.py` for the decoder, the
    fixture verification behind it, and the limit this gate does not remove.

An earlier version of this module documented the first gate and did not have
it, and a reviewer walked straight through the hole: a document naming nothing
parses, yields an empty blob set, and is then indistinguishable from every
other shard's document. A docstring describing a guard the code does not have
is worse than no docstring.
"""

from __future__ import annotations

import re
from typing import (Any, Dict, FrozenSet, List, Mapping, NamedTuple,
                    Optional, Set, Tuple)

from ..errors import ShapeGateError
from ..model import ShardDocument
from .codec import unwrap
from .lucene_segments import SegmentsFileError, required_segment_names

# A real segment blob, and its `.part<K>` pieces. Elasticsearch also writes
# `v__<id>` entries, which hold their content inline in the document and have
# no object behind them, so they must never reach a manifest.
#
# ONE function answers "is this a segment, and which one". The live set and
# the object attachment both go through it. Two predicates that disagree about
# what a segment is will always end up naming a live object: a live file list
# holding `__a.part0` was invisible to a live-set predicate that rejected the
# dot, while the attachment side happily hung `__a.part0` off a condemned
# `__a`.
BLOB_STEM = re.compile(r"^(__[A-Za-z0-9_\-]+)(?:\.part(?:0|[1-9][0-9]*))?$")
INLINE_PREFIX = "v__"
# Lucene names a commit `segments_N` with N in base 36. `segments_t` appears in
# the captured repository, so a decimal-only pattern would reject real data.
LUCENE_COMMIT = re.compile(r"^segments_[0-9a-zA-Z]+$")


def segment_stem(name: str) -> Optional[str]:
    """The blob one file-list name refers to, or None if it is not a segment.

    A file longer than the repository's part size has no object under its bare
    name, only `.part<K>` pieces, so the stem is what both sides reason about.
    """
    if name.startswith(INLINE_PREFIX):
        return None
    match = BLOB_STEM.match(name)
    return match.group(1) if match else None


def is_segment_blob(name: str) -> bool:
    return segment_stem(name) is not None


def parse_shard_snapshots(data: bytes, where: str) -> ShardDocument:
    """Decode one shard document and put it through the shape gate."""
    document = unwrap(data, "snapshots")
    if not isinstance(document, dict):
        raise ShapeGateError(
            f"{where} decoded to a {type(document).__name__}, not a shard "
            "document")
    entries = _declared(document, where)
    by_snapshot, lengths, checked, skipped = _by_snapshot(
        document, entries, where)
    return ShardDocument(
        blob_names=frozenset(
            stem for stem in (segment_stem(n) for n in entries) if stem),
        by_snapshot_name=by_snapshot,
        writer_uuids=frozenset(entry.writer_uuid
                               for entry in entries.values()
                               if entry.writer_uuid is not None),
        length_by_snapshot_name=lengths,
        commit_oracle_checked=checked,
        commit_oracle_skipped=skipped,
    )


class _FileEntry(NamedTuple):
    """What one entry of the `files` array declares about the file it names.

    `physical` is the Lucene name the file was written from. `writer_uuid` is
    Lucene's IndexWriter identity, or None when the entry carries none: an
    older segment may have none, and Elasticsearch's own
    `StoreFileMetadata.isSame` uses it only when it is there, so None means
    no signal, never "this belongs to nobody". `commit` holds the bytes of a
    `segments_N` the document stores inline, and is None for any other file.
    """

    physical: str
    length: int
    writer_uuid: Optional[object]
    commit: Optional[bytes]


def _declared(document: Mapping[str, Any],
             where: str) -> Dict[str, _FileEntry]:
    """Every entry in the document's `files` array, by declared name.

    The `files` array is the union of what the snapshots in this document
    reference. It is read first so the per-snapshot lists can be checked
    against it, which is how a half-decoded document gets caught: two
    independently written parts of the same file have to agree. One pass
    over it yields everything later steps need, under one rule: an entry
    Elasticsearch's own parser would refuse is refused here too.
    """
    raw = document.get("files")
    if not isinstance(raw, list):
        raise ShapeGateError(
            f"{where} has no files array; the field may have been renamed")
    entries: Dict[str, _FileEntry] = {}
    for entry in raw:
        name, declared = _declared_entry(entry, where)
        entries[name] = declared
    return entries


def _declared_entry(entry: Any, where: str) -> Tuple[str, _FileEntry]:
    """The declared name of one `files` entry and what it says about the file.

    Elasticsearch's FileInfo parser refuses an entry whose physical name is
    missing and one whose length is missing or negative, and so does this.
    Real 9.5.2 stores `segments_N` inline, as a `v__` entry whose `meta_hash`
    field holds its content, so the commit bytes come out of the same pass
    rather than costing a second read.
    """
    if not isinstance(entry, dict):
        raise ShapeGateError(f"{where} has a non-object files entry")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ShapeGateError(f"{where} has a files entry with no name")
    physical = entry.get("physical_name")
    if not isinstance(physical, str) or not physical:
        raise ShapeGateError(
            f"{where} files entry {name!r} has no physical name")
    length = entry.get("length")
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise ShapeGateError(
            f"{where} files entry {name!r} declares length {length!r}, "
            "which is not a byte count")
    writer = entry.get("writer_uuid")
    commit = None
    if LUCENE_COMMIT.match(physical):
        content = entry.get("meta_hash")
        if isinstance(content, (bytes, bytearray)):
            commit = bytes(content)
    return name, _FileEntry(
        physical=physical, length=length,
        writer_uuid=writer if isinstance(writer, (bytes, str)) and writer
        else None,
        commit=commit)


def _by_snapshot(document: Mapping[str, Any], entries: Dict[str, _FileEntry],
                 where: str) -> Tuple[Dict[str, FrozenSet[str]],
                                      Dict[str, int], int, int]:
    """Each snapshot's segment blobs and byte total, and the oracle's tally.

    The byte total is what the snapshot's files in this shard add up to. The
    extent check compares it with `index_details[<index>].size_in_bytes` in
    the snapshot's own document, which measurement showed is the TOTAL for
    that snapshot in that index rather than an increment: exact on five
    index and snapshot pairs across two independently built 9.5.2
    repositories.
    """
    raw = document.get("snapshots")
    if not isinstance(raw, dict):
        raise ShapeGateError(
            f"{where} has no snapshots object; the field may have been "
            "renamed, or this is a root generation rather than a shard")
    if entries and not raw:
        raise ShapeGateError(
            f"{where} names {len(entries)} files and no snapshot that "
            "references them")
    out: Dict[str, FrozenSet[str]] = {}
    lengths: Dict[str, int] = {}
    commit_oracle_checked = 0
    commit_oracle_skipped = 0
    for snapshot_name, entry in raw.items():
        if not isinstance(snapshot_name, str) or not isinstance(entry, dict):
            raise ShapeGateError(f"{where} has a malformed snapshots entry")
        location = f"{where} snapshot {snapshot_name!r}"
        blobs, blob_physical, commit_names, length = _snapshot_file_list(
            entry, entries, location)
        for commit_name in commit_names:
            if _cross_check_commit(entries[commit_name].physical,
                                   entries[commit_name].commit,
                                   blob_physical, location):
                commit_oracle_checked += 1
            else:
                commit_oracle_skipped += 1
        out[snapshot_name] = blobs
        lengths[snapshot_name] = length
    return out, lengths, commit_oracle_checked, commit_oracle_skipped


def _snapshot_file_list(entry: Mapping[str, Any],
                        entries: Dict[str, _FileEntry], location: str
                        ) -> Tuple[FrozenSet[str], Set[str], List[str], int]:
    """What one snapshot entry's file list names, once it has passed the gates.

    Returns the segment blobs behind the names, the Lucene names of the
    blob-backed files among them, the names whose Lucene name is a
    `segments_N` commit, and the byte total of the listed files. Inline
    `v__` entries have no Lucene name in the second set, because they have
    no object behind them. Every refusal
    below is a refusal to return a SHORT list: a list this reader only
    partly understood is not a shard that references less, and the caller
    has no way to tell those apart from what comes back.
    """
    files = entry.get("files")
    if not isinstance(files, list):
        raise ShapeGateError(f"{location} has no files array")
    if not files:
        # A shard snapshot always carries at least its Lucene commit, so
        # an empty list is a list that was not read rather than a shard
        # that references nothing. Returning it empty makes this document
        # indistinguishable from every other shard's document, and the
        # live set built from it condemns the whole directory.
        raise ShapeGateError(
            f"{location} lists no files at all. A shard snapshot always "
            "carries at least its Lucene commit, so an empty list is a list "
            "that was not read")
    blobs: Set[str] = set()
    commit_names: List[str] = []
    blob_physical: Set[str] = set()
    length = 0
    for name in files:
        if not isinstance(name, str) or not name:
            raise ShapeGateError(f"{location} lists a non-string file")
        if name not in entries:
            # The two halves of this document disagree. One of them was
            # decoded wrongly, and there is no way to tell which, so the
            # caller drops the whole shard rather than pick a half.
            raise ShapeGateError(
                f"{location} references {name!r}, which the files array does "
                "not declare")
        length += entries[name].length
        if LUCENE_COMMIT.match(entries[name].physical):
            commit_names.append(name)
        stem = segment_stem(name)
        if stem is not None:
            blobs.add(stem)
            blob_physical.add(entries[name].physical)
    if not commit_names:
        # Restoring a shard means restoring a Lucene commit, and a commit
        # is named by its segments_N file. A list without one cannot
        # restore what it claims to describe, so it is not a list this
        # tool read correctly. Real 9.5.2 keeps that commit inline, which
        # is where a drift that moved the inline entries would land.
        raise ShapeGateError(
            f"{location} names {len(files)} file(s) and none of them is a "
            "Lucene segments_N commit, so the file list is incomplete")
    return frozenset(blobs), blob_physical, commit_names, length


def _segment_is_represented(segment: str, blob_physical: Set[str]) -> bool:
    """Whether a blob-backed file in this snapshot's file list is `segment`'s.

    A segment's files all share its name as either the whole name
    (`segments_N` itself is never one of these) or a prefix ending in `.` or
    `_` (`_2.cfs`, `_2.cfe`, `_2_1.liv`). Checking the boundary rather than a
    bare prefix is what keeps `_2` from matching a declared `_20.cfs`, which
    belongs to a different segment that happens to share its first two
    characters. `_2.si` never counts: it is stored inline, so it stays in
    the list when every blob of `_2` is gone.
    """
    return any(candidate == segment
               or candidate.startswith(segment + ".")
               or candidate.startswith(segment + "_")
               for candidate in blob_physical)


def _cross_check_commit(commit_physical: str, commit_bytes: Optional[bytes],
                        blob_physical: Set[str], location: str) -> bool:
    """Issue #1's independent oracle, for one commit in one snapshot entry.

    Compares what `segments_N` requires against the blob-backed files this
    snapshot's own file list declares. Runs only when this document carried
    the commit's bytes inline, which is what real 9.5.2 does for a shard of
    realistic size (verified in `lucene_segments.py`). When it did not,
    there is nothing to compare, and this falls back to the name-only
    presence gate that existed before this check: a known, named gap rather
    than a new one, and every fixture built before this fix relies on
    exactly that fallback.

    Returns whether the comparison actually ran, so the caller can count it.
    A run where this returns False for every commit it saw is a run where
    issue #1's fix never engaged, and an operator reading the coverage
    report has to be able to tell that apart from a run where it engaged and
    found nothing wrong; see `Coverage.commit_oracle_checked` and
    `commit_oracle_skipped`.
    """
    if commit_bytes is None:
        return False
    try:
        required = required_segment_names(commit_bytes)
    except SegmentsFileError as exc:
        raise ShapeGateError(
            f"{location} names Lucene commit {commit_physical!r} whose "
            f"content this reader could not decode ({exc}); refusing rather "
            "than trusting a file list this cannot corroborate") from exc
    missing = sorted(segment for segment in required
                     if not _segment_is_represented(segment, blob_physical))
    if missing:
        raise ShapeGateError(
            f"{location}: Lucene commit {commit_physical!r} requires "
            f"segment(s) {missing} that this snapshot's file list does not "
            "reference; the file list under-references what Lucene needs")
    return True
