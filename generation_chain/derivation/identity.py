"""Did this read return the object it asked for.

A BlobStoreIndexShardSnapshots names neither its own shard, nor its own index,
nor its own generation. That was read off real documents rather than assumed:
the only top-level fields are `files` and `snapshots`. So nothing INSIDE the
bytes ties a document to the key it arrived under, and identity has to be built
from outside them.

Three signals, and each one is stated with what it can and cannot separate.

  SNAPSHOT NAMES. The set of names the document carries has to match what the
  catalog of that generation says. This separates GENERATIONS and it separates
  a foreign repository's document from ours. It cannot separate one shard from
  another: at a single generation every shard of every index a snapshot covers
  carries exactly the same snapshot names.

  BLOBS IN THIS DIRECTORY. Necessary, and on its own nowhere near sufficient,
  because containment is a SUBSET test and a subset test cannot separate two
  documents when the smaller side is empty or contained in the larger. A
  reviewer walked all three openings: a document naming nothing is a subset of
  every directory, a document naming only inline `v__` entries is the same
  shape and Elasticsearch really writes it for a snapshot of an empty index,
  and a real document from a neighbouring shard whose blobs happen to be
  contained in the victim directory passes while trading a dead segment for a
  live one. So the rule here is ATTRIBUTABILITY rather than containment: the
  document has to name at least one blob the listing puts in THIS directory
  and in NO OTHER, and the store has to confirm that blob. Blob names are
  globally unique ids, so a genuine document always has such a witness, and a
  document that has none does not distinguish this directory from the
  candidates it would equally fit.

  WRITER UUID. Lucene's IndexWriter identity, carried per file entry. What it
  is used for here is narrower than the retired version claimed, because
  measurement contradicted that claim. See `writer_uuid_collisions` below.

AND THE DOCUMENT HAS TO NAME A BLOB AT ALL. `require_blob_names` is the gate the
retired sweeper carried at `s3_repo_sweeper.py:2743` and this package dropped.
A shard snapshot always restores a Lucene commit, so a document that yields no
blob names is a document that was not read, never a shard that references
nothing. Returning it empty is what makes it indistinguishable from every other
shard's document, and an empty live set condemns the whole directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Set

from ..errors import ShapeGateError
from ..model import ShardDocument
from .keys import CONFIRMED, KeyIndex

NO_BLOB_NAMES = "no-blob-names"
NAMES_BLOBS_NOT_HERE = "names-blobs-not-in-this-directory"
NO_UNIQUE_WITNESS = "no-witness-unique-to-this-directory"
WITNESS_UNCONFIRMED = "witness-not-confirmed-by-the-store"
SNAPSHOT_NAMES_DISAGREE = "snapshot-names-disagree-with-the-catalog"
WRITER_UUID_COLLISION = "writer-uuid-belongs-to-another-directory"


@dataclass(frozen=True)
class Doubt:
    """Why a read cannot be believed, as a code a test can assert on.

    The code is the fact; the detail is for the operator. Tests assert the
    code, so rewording an explanation never turns a suite green or red, and a
    guard cannot be pinned by a sentence that happens to survive a refactor.
    """

    code: str
    detail: str

    def __str__(self) -> str:
        return self.detail


def require_blob_names(document: ShardDocument, where: str) -> None:
    """Refuse a shard document that names no segment blob.

    Raises rather than returning a verdict, because every caller in this
    package treats a raise as "this read did not happen", and a document that
    names nothing has to reach exactly that path. Returning an empty set here
    is the single change that would let one document satisfy the per-directory
    subset test against every directory in the repository.
    """
    if not document.blob_names:
        raise ShapeGateError(
            f"{where} yields no segment blob name. A shard snapshot always "
            "carries at least its Lucene commit, so this is a document that "
            "was not read rather than a shard that references nothing")


def check_directory(document: ShardDocument, where: str, directory: str,
                    listed_stems: FrozenSet[str],
                    owners: Mapping[str, Set[str]],
                    keys: KeyIndex) -> Optional[Doubt]:
    """Whether this document can be tied to the directory it came from."""
    if not document.blob_names:
        return Doubt(NO_BLOB_NAMES,
                     f"{where} names no segment blob at all, so nothing in it "
                     "distinguishes this shard from any other")
    stray = document.blob_names - set(listed_stems)
    if stray:
        return Doubt(
            NAMES_BLOBS_NOT_HERE,
            f"{where} names blobs the store does not hold in this directory: "
            + ", ".join(sorted(stray)))
    witnesses = sorted(stem for stem in document.blob_names
                       if owners.get(stem) == {directory})
    if not witnesses:
        return Doubt(
            NO_UNIQUE_WITNESS,
            f"{where} names no blob that belongs to this directory alone, so "
            "it does not distinguish this shard from the others it would "
            "equally fit")
    if not any(keys.objects_for(f"{directory}/{stem}") for stem in witnesses):
        # The listing suggested the witness and the store has to settle it, by
        # the same path that confirms a key before the manifest names it.
        # Trusting the listing here while distrusting it there was an asymmetry
        # pointing the wrong way: it distrusted the listing where it could ADD
        # a key and trusted it where it protects live data.
        return Doubt(
            WITNESS_UNCONFIRMED,
            f"{where} names {len(witnesses)} blob(s) unique to this directory "
            "in the listing and the store confirmed none of them")
    return None


def check_snapshot_names(document: ShardDocument, where: str,
                         expected: Set[str]) -> Optional[Doubt]:
    """Whether the document belongs to the generation it was fetched for.

    This is what stands against a document swapped for ANOTHER GENERATION of
    the SAME shard, whose blobs are that directory's own and whose witness is
    therefore genuinely unique to it. Attributability cannot see that swap.
    Stating the boundary rather than dressing it up.
    """
    found = set(document.by_snapshot_name)
    if found == expected:
        return None
    return Doubt(
        SNAPSHOT_NAMES_DISAGREE,
        f"{where} names {_listed(found)} where the catalog says this shard is "
        f"referenced by {_listed(expected)}")


def writer_uuid_collisions(
        by_directory: Mapping[str, Iterable[ShardDocument]]
) -> Dict[str, Set[str]]:
    """Directories whose documents claim a Lucene writer another directory owns.

    WHAT WAS MEASURED. Two Elasticsearch 9.5.2 repositories, both captured and
    kept in `tests/fixtures`.

    `real-es952-repo.tar.gz`, nine shard documents across three indices of one
    shard each:

      every cross-directory pairing, 27 of them:   overlap 0
      same-directory pairings, 6 of them:          overlap 0 in one, 2 or 9 in
                                                   the other five

    `real-es952-twoshard-repo.tar.gz`, built for the case the first repository
    could not reach, an index with TWO shards:

      shard 0 against shard 1 of the same index:   overlap 0, 8 against 8

    The cross-directory lines are the property this function uses, and the
    second repository closes the gap: disjointness holds across two shards of
    ONE index and not only across two indices.

    The same-directory line REFUTES the claim the retired version of this
    module made, that a writer uuid set is stable within a shard across
    generations. In `indices/KMsiARacSXSgCnGLMZ191w/0` the documents for
    `v9-snap-1` and `v9-snap-2` share nothing at all, because the index was
    rewritten between the two snapshots. A guard built on within-shard
    stability would drop that shard on healthy data, so nothing here uses it.

    A CAVEAT THE NUMBERS HIDE. In the two-shard repository all sixteen values
    share a fifteen-byte prefix and differ only in the last byte or two, which
    is what a per-node counter looks like. They are exactly disjoint, which is
    all this function tests, and they are not far apart. So this is a set
    membership check and never a distance or a prefix check.

    THE DIRECTION. A writer uuid seen under two directories is a positive
    contradiction: one of the two reads returned the other's document. Both
    directories are named, because there is no way to tell which read was
    wrong. A MATCHING writer uuid never blesses anything, so this function can
    only ever add a directory to the dropped set. Elasticsearch treats the
    field the same way in `StoreFileMetadata.isSame`, where a mismatch decides
    and a match falls through to the other comparisons.
    """
    seen: Dict[object, Set[str]] = {}
    for directory, documents in by_directory.items():
        for document in documents:
            for writer in document.writer_uuids:
                seen.setdefault(writer, set()).add(directory)
    out: Dict[str, Set[str]] = {}
    for writer, directories in seen.items():
        if len(directories) > 1:
            for directory in directories:
                out.setdefault(directory, set()).update(directories - {directory})
    return out


def _listed(names: Set[str]) -> str:
    return ", ".join(sorted(names)) if names else "no snapshots"
