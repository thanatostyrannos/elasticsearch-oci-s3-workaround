"""Snapshot repositories built to be WRONG in one named way at a time.

The builder in `genchain_fixtures.py` writes healthy leaking repositories and
the tests that use it mutate bytes afterwards. That is enough for the format
readers and it was not enough for the derivation: a reviewer showed the
generator built on it could not reach three failing regions at all, because
the shapes it needed had no donor anywhere in the corpus.

So this builder takes the adversarial shapes as ARGUMENTS. Every counterexample
the derivation has to survive is a keyword here rather than a byte edit applied
after the fact, which is what lets a property search compose several of them at
once and lets a reachability test assert that a given guard was reached with the
guards ahead of it satisfied.

What it writes is Elasticsearch's formats by hand, never by calling the code
that reads them, so a disagreement between a test and the tool is a
disagreement about the format rather than a reader agreeing with itself.

Every repository it writes LEAKS: deleting a snapshot drops it from the next
root generation and the next shard document and leaves every blob behind. That
is the fault this project exists for and the only state in which a
generation-chain derivation has anything to read.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

CODEC_MAGIC = 0x3FD76C17
FOOTER_MAGIC = (~CODEC_MAGIC) & 0xFFFFFFFF
# One length for every file entry, so a snapshot document's declared
# size_in_bytes is a plain multiple and a shortened file list moves it visibly.
FILE_LENGTH = 42
INLINE_COMMIT_PREFIX = "v__commit-"


def lucene_vint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def codec_wrap(payload: bytes, codec_name: str = "snapshots",
               version: int = 1, deflate: bool = False) -> bytes:
    """Frame a payload the way ChecksumBlobStoreFormat does."""
    if deflate:
        payload = b"DFL\x00" + zlib.compress(payload)
    body = (struct.pack(">I", CODEC_MAGIC)
            + lucene_vint(len(codec_name)) + codec_name.encode("utf-8")
            + struct.pack(">I", version)
            + payload)
    footer = struct.pack(">I", FOOTER_MAGIC) + struct.pack(">I", 0)
    crc = zlib.crc32(body + footer) & 0xFFFFFFFF
    return body + footer + struct.pack(">Q", crc)


def snapshot_uuid(key: str) -> str:
    """A history key `name#tag` gives one NAME two uuids.

    Elasticsearch identifies a snapshot by uuid in the root catalog and by name
    inside a shard document, so a name that has belonged to two snapshots over
    time is the one state where those two views cannot be joined.
    """
    return "uuid-" + key


def snapshot_name(key: str) -> str:
    return key.split("#", 1)[0]


def index_uuid(name: str) -> str:
    return "iuuid-" + name


def shard_generation_id(index: str, shard: int, generation: int,
                        numeric: bool = False) -> str:
    """The id the catalog gives one shard's document at one generation.

    Both shapes are real and they are not interchangeable for a test. A numeric
    id makes `indices/<uuid>/0/index-3` match the same pattern as a root
    catalog `index-3`, so a derivation that checks the pattern without checking
    the depth reads a shard file list as a repository catalog. A generator
    producing only uuid-shaped ids never enters that region.
    """
    return str(generation) if numeric else f"sg-{index}-{shard}-{generation}"


def directory_of(index: str, shard: int) -> str:
    return f"indices/{index_uuid(index)}/{shard}"


# One generation of history: snapshot key -> index name -> shard -> blob names.
Placement = Mapping[str, Mapping[str, Mapping[int, Sequence[str]]]]


@dataclass
class Defects:
    """Every way a repository is allowed to be wrong, named one at a time.

    Each field below exists because a reviewer produced a counterexample the
    derivation walked straight through, or because a guard had no input that
    could reach it. Naming them here rather than editing bytes afterwards is
    what lets the property search compose two of them and lets the reachability
    test assert which guard a given shape arrives at.
    """

    # index.latest names a LOWER generation than the highest one on disk. This
    # is what an ordinary crash between writing index-N+1 and updating
    # index.latest leaves, and it made the retired code name live keys.
    latest_lags_by: int = 0
    # index.latest names a generation no blob backs.
    latest_points_nowhere: bool = False
    # min_version on one generation, so the precondition can be checked per
    # generation rather than once at the anchor.
    min_version: str = "7.12.0"
    min_version_by_generation: Mapping[int, str] = field(default_factory=dict)
    # A shard document with no files and no snapshots. It parses, names
    # nothing, and is therefore a subset of every directory in the repository.
    empty_documents: Sequence[Tuple[str, int, int]] = ()
    # A shard document whose only entries are inline `v__` ones, which is what
    # Elasticsearch really writes for a snapshot of an empty index. It also
    # yields no blob names.
    inline_only: Sequence[Tuple[str, int, int]] = ()
    # Objects placed in a directory under a name that also lives in another
    # directory, so a document from the second is CONTAINED in the first.
    decoy_blobs: Mapping[str, Sequence[str]] = field(default_factory=dict)
    # writer_uuid overrides, so a document can be given another shard's Lucene
    # identity or none at all.
    writer_uuid_of: Mapping[Tuple[str, int], Optional[str]] = field(
        default_factory=dict)
    # A snapshot document that overstates or understates its own extent.
    declared_shard_count: Mapping[Tuple[str, str], int] = field(
        default_factory=dict)
    declared_total_shards: Mapping[str, int] = field(default_factory=dict)
    # A snapshot document that is absent, unreadable, or declares a partial run.
    missing_snapshot_documents: Sequence[str] = ()
    partial_snapshots: Sequence[str] = ()
    # A catalog that names no index metadata identifiers, which is the shape
    # the format floor refuses.
    drop_index_metadata: bool = False
    # A repository uuid that changes at one generation, which is a co-tenant's
    # blob sitting under our numbering.
    foreign_uuid_at: Mapping[int, str] = field(default_factory=dict)
    # Numeric shard generation ids rather than uuid-shaped ones. Not a defect,
    # a second real shape. See `shard_generation_id`.
    numeric_shard_generations: bool = False
    # One index metadata blob PER SNAPSHOT rather than one per index, which is
    # what Elasticsearch writes when an index's metadata changes between two
    # snapshots. Without it a deleted snapshot's metadata blob is always also
    # the live snapshot's, so the branch that condemns one is unreachable.
    per_snapshot_index_metadata: bool = False


@dataclass
class Built:
    root: str
    keys: List[str]
    generations: int
    latest: int
    repository_uuid: str
    # Blob key -> the shard directory it was written into, for tests that need
    # to name a specific object without re-deriving the layout.
    blobs: Dict[str, str]
    live_blob_keys: Set[str]


def build(root: str, history: Sequence[Placement],
          repository_uuid: str = "repo-uuid-aaaa",
          defects: Optional[Defects] = None) -> Built:
    """Write a leaking repository whose root generations are 0..len(history)-1.

    `history[g]` is the catalog AT generation g. A snapshot present at g and
    absent at g+1 was deleted by the operation that wrote g+1, and every blob
    it named stays on disk.
    """
    defects = defects or Defects()
    plan = _normalise(history)
    blobs: Dict[str, str] = {}

    for generation, spec in enumerate(plan):
        _write_root_generation(root, generation, spec, repository_uuid, defects)
        for index, shards in _by_index(spec).items():
            for shard, per_snapshot in shards.items():
                _write_shard_document(root, index, shard, generation,
                                      per_snapshot, defects)
                for names in per_snapshot.values():
                    for name in names:
                        key = f"{directory_of(index, shard)}/{name}"
                        blobs[key] = directory_of(index, shard)
                        _write(root, key, b"segment data")

    for snapshot_key in {k for spec in plan for k in spec}:
        _write_snapshot_documents(root, snapshot_key, plan, defects)

    for directory, names in defects.decoy_blobs.items():
        for name in names:
            _write(root, f"{directory}/{name}", b"decoy segment data")

    current = len(plan) - 1
    latest = current - defects.latest_lags_by
    if defects.latest_points_nowhere:
        latest = current + 99
    _write(root, "index.latest", struct.pack(">q", latest))

    return Built(root=root, keys=read_keys(root), generations=len(plan),
                 latest=latest, repository_uuid=repository_uuid, blobs=blobs,
                 live_blob_keys=_live_blob_keys(plan, current))


def _live_blob_keys(plan: List[Dict[str, Dict[str, Dict[int, List[str]]]]],
                    current: int) -> Set[str]:
    """Every blob object a snapshot in the FINAL catalog still references.

    This is the answer the tool must never name, computed here from the
    fixture's own declaration rather than from anything the tool derives, so a
    test comparing the two is comparing independent statements.
    """
    out: Set[str] = set()
    for indices in plan[current].values():
        for index, shards in indices.items():
            for shard, names in shards.items():
                for name in names:
                    out.add(f"{directory_of(index, shard)}/{name}")
    return out


def _normalise(history: Sequence[Placement]
               ) -> List[Dict[str, Dict[str, Dict[int, List[str]]]]]:
    return [{snap: {index: {int(s): list(names) for s, names in shards.items()}
                    for index, shards in indices.items()}
             for snap, indices in spec.items()}
            for spec in history]


def _by_index(spec: Mapping[str, Mapping[str, Mapping[int, List[str]]]]
              ) -> Dict[str, Dict[int, Dict[str, List[str]]]]:
    """Turn snapshot-first into index-first, which is how the store is laid out."""
    out: Dict[str, Dict[int, Dict[str, List[str]]]] = {}
    for snapshot_key, indices in spec.items():
        for index, shards in indices.items():
            for shard, names in shards.items():
                out.setdefault(index, {}).setdefault(shard, {})[
                    snapshot_name(snapshot_key)] = list(names)
    return out


def _write_root_generation(root: str, generation: int,
                           spec: Mapping[str, Mapping[str, Mapping[int, List[str]]]],
                           repository_uuid: str, defects: Defects) -> None:
    by_index = _by_index(spec)
    indices: Dict[str, dict] = {}
    for index, shards in by_index.items():
        holders = sorted({snapshot_uuid(k) for k, ix in spec.items()
                          if index in ix})
        indices[index] = {
            "id": index_uuid(index),
            "snapshots": holders,
            "shard_generations": [
                shard_generation_id(index, shard, generation,
                                    defects.numeric_shard_generations)
                for shard in range(max(shards) + 1)],
        }
    snapshots = []
    for snapshot_key in sorted(spec):
        snapshots.append({
            "name": snapshot_name(snapshot_key),
            "uuid": snapshot_uuid(snapshot_key),
            "state": 1,
            "version": "8.11.0",
            "index_metadata_lookup": {
                index_uuid(index): _lookup_value(index, snapshot_key, defects)
                for index in spec[snapshot_key]},
        })
    document = {
        "min_version": defects.min_version_by_generation.get(
            generation, defects.min_version),
        "uuid": defects.foreign_uuid_at.get(generation, repository_uuid),
        "cluster_id": "cluster-aaaa",
        "snapshots": snapshots,
        "indices": indices,
    }
    if not defects.drop_index_metadata:
        identifiers = {}
        for snapshot_key, indices_of in spec.items():
            for index in indices_of:
                value = _lookup_value(index, snapshot_key, defects)
                identifiers[value] = _metadata_blob_id(index, snapshot_key,
                                                       defects)
        document["index_metadata_identifiers"] = identifiers
        for value, blob_id in identifiers.items():
            index = value.split("-")[1]
            _write(root, f"indices/{index_uuid(index)}/meta-{blob_id}.dat",
                   b"index metadata")
    _write(root, f"index-{generation}",
           json.dumps(document, sort_keys=True).encode("utf-8"))


def _lookup_value(index: str, snapshot_key: str, defects: "Defects") -> str:
    if defects.per_snapshot_index_metadata:
        return f"lookup-{index}-{snapshot_name(snapshot_key)}"
    return f"lookup-{index}"


def _metadata_blob_id(index: str, snapshot_key: str,
                      defects: "Defects") -> str:
    if defects.per_snapshot_index_metadata:
        return f"md-{index}-{snapshot_name(snapshot_key)}"
    return f"md-{index}"


def metadata_key(index: str, snapshot_key: str = "",
                 per_snapshot: bool = False) -> str:
    """The index metadata object one snapshot reads for one index."""
    blob = (f"md-{index}-{snapshot_name(snapshot_key)}" if per_snapshot
            else f"md-{index}")
    return f"indices/{index_uuid(index)}/meta-{blob}.dat"


def _write_shard_document(root: str, index: str, shard: int, generation: int,
                          per_snapshot: Mapping[str, List[str]],
                          defects: Defects) -> None:
    key = (f"{directory_of(index, shard)}/"
           f"index-{shard_generation_id(index, shard, generation, defects.numeric_shard_generations)}")
    where = (index, shard, generation)

    if where in defects.empty_documents:
        # Parses, declares nothing, and is a subset of every directory in the
        # repository. This is the shape the retired sweeper refused and this
        # package once accepted.
        _write(root, key, codec_wrap(
            json.dumps({"files": [], "snapshots": {}}).encode("utf-8")))
        return

    inline_only = where in defects.inline_only
    writer = defects.writer_uuid_of.get(
        (index, shard), f"writer-{index}-{shard}")

    files: List[dict] = []
    snapshots: Dict[str, dict] = {}
    seen: Set[str] = set()
    for snapshot, names in sorted(per_snapshot.items()):
        commit = f"{INLINE_COMMIT_PREFIX}{index}-{shard}-{generation}"
        listed = ([] if inline_only else list(names)) + [commit]
        for name in listed:
            if name in seen:
                continue
            seen.add(name)
            entry = {
                "name": name,
                "physical_name": (f"segments_{generation + 1}"
                                  if name.startswith(INLINE_COMMIT_PREFIX)
                                  else f"_{name[2:]}.cfs"),
                "length": FILE_LENGTH,
                "checksum": "cksum",
                "written_by": "9.5.2",
            }
            if writer is not None:
                entry["writer_uuid"] = writer
            files.append(entry)
        snapshots[snapshot] = {"files": listed,
                               "shard_state_id": f"state-{generation}"}
    _write(root, key, codec_wrap(
        json.dumps({"files": files, "snapshots": snapshots},
                   sort_keys=True).encode("utf-8"),
        deflate=generation % 2 == 1))


def _write_snapshot_documents(
        root: str, snapshot_key: str,
        plan: List[Dict[str, Dict[str, Dict[int, List[str]]]]],
        defects: Defects) -> None:
    """`snap-<uuid>.dat` and `meta-<uuid>.dat` at the repository root.

    The extent is taken from the LAST generation that held the snapshot, which
    is what Elasticsearch wrote when the snapshot finished.
    """
    if snapshot_key in defects.missing_snapshot_documents:
        return
    latest_spec = None
    for spec in plan:
        if snapshot_key in spec:
            latest_spec = spec[snapshot_key]
    if latest_spec is None:
        return

    details = {}
    total = 0
    for index, shards in latest_spec.items():
        declared = defects.declared_shard_count.get(
            (snapshot_key, index), max(shards) + 1)
        total += declared
        details[index] = {
            "shard_count": declared,
            "size_in_bytes": sum((len(names) + 1) * FILE_LENGTH
                                 for names in shards.values()),
            "max_segments_per_shard": max(
                (len(names) for names in shards.values()), default=0),
        }
    total = defects.declared_total_shards.get(snapshot_key, total)
    successful = 0 if snapshot_key in defects.partial_snapshots else total

    uuid = snapshot_uuid(snapshot_key)
    body = {
        "name": snapshot_name(snapshot_key),
        "uuid": uuid,
        "state": "SUCCESS",
        "indices": sorted(latest_spec),
        "total_shards": total,
        "successful_shards": successful,
        "index_details": details,
    }
    _write(root, f"snap-{uuid}.dat", codec_wrap(
        json.dumps({"snapshot": body}, sort_keys=True).encode("utf-8"),
        codec_name="snapshot"))
    _write(root, f"meta-{uuid}.dat", b"global metadata")
    for index, shards in latest_spec.items():
        for shard in shards:
            _write(root, f"{directory_of(index, shard)}/snap-{uuid}.dat",
                   b"shard snapshot document")


def _write(root: str, rel: str, data: bytes) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def extract_fixture(name: str, destination: str) -> str:
    """Unpack one captured repository from tests/fixtures into a directory.

    This lives here rather than in the sweeper rig because the sweepers and
    their rig are being retired, and a genchain test that imports from them
    would go with them.
    """
    import tarfile
    archive = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", name)
    os.makedirs(destination, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith(("/", "..")) or ".." in member.name.split("/"):
                raise ValueError(f"{archive} holds an unsafe path: {member.name}")
        tar.extractall(destination)
    return destination


def read_keys(root: str) -> List[str]:
    out: List[str] = []
    for directory, _, names in os.walk(root):
        for name in names:
            out.append(os.path.relpath(os.path.join(directory, name),
                                       root).replace(os.sep, "/"))
    return sorted(out)


def read(root: str, rel: str) -> bytes:
    with open(os.path.join(root, rel), "rb") as handle:
        return handle.read()


def overwrite(root: str, rel: str, data: bytes) -> None:
    _write(root, rel, data)


def remove(root: str, rel: str) -> None:
    path = os.path.join(root, rel)
    if os.path.exists(path):
        os.remove(path)


# -- a store that is wrong in composable ways --------------------------------
#
# Faults used to be whole store objects, one per run, and that alone put a
# counterexample out of reach: no single fault both swapped bytes AND changed
# the listing, so the arrangement where an over-reporting listing admits an
# otherwise-caught swap could not be generated at all. These compose.


@dataclass
class Fault:
    """One way a store can be wrong."""

    # key -> bytes to answer with, whoever asks.
    swap_bytes: Mapping[str, bytes] = field(default_factory=dict)
    # key -> another key whose bytes to answer with. A successful read of the
    # wrong object, which no per-read check can see.
    swap_keys: Mapping[str, str] = field(default_factory=dict)
    # Keys the listing reports and the store does not hold.
    extra: Sequence[str] = ()
    # Keys the store holds and the listing does not report.
    hidden: Sequence[str] = ()
    # Keys the listing reports and the existence check then DENIES. Separate
    # from `extra` because a store that is consistently wrong and a store whose
    # listing lags its own contents are different failures, and only the second
    # is caught by confirming a key before naming it.
    denied: Sequence[str] = ()
    # Keys whose existence check RAISES. The store neither confirmed nor
    # denied, which must never be recorded as a denial.
    unanswerable: Sequence[str] = ()
    # Keys whose fetch raises.
    unreadable: Sequence[str] = ()
    # The listing comes back in an arbitrary order, or fails outright.
    shuffle: bool = False
    listing_fails: bool = False


class FaultySource:
    """A local mirror wrapped in any combination of faults."""

    def __init__(self, root: str, faults: Sequence[Fault] = ()) -> None:
        from generation_chain.errors import SourceReadError
        from generation_chain.sources.local import LocalMirrorSource
        self._error = SourceReadError
        self.inner = LocalMirrorSource(root)
        self.swap_bytes: Dict[str, bytes] = {}
        self.swap_keys: Dict[str, str] = {}
        self.extra: List[str] = []
        self.hidden: Set[str] = set()
        self.denied: Set[str] = set()
        self.unanswerable: Set[str] = set()
        self.unreadable: Set[str] = set()
        self.shuffle = False
        self.listing_fails = False
        for fault in faults:
            self.swap_bytes.update(fault.swap_bytes)
            self.swap_keys.update(fault.swap_keys)
            self.extra.extend(k for k in fault.extra if k)
            self.hidden.update(k for k in fault.hidden if k)
            self.denied.update(k for k in fault.denied if k)
            self.unanswerable.update(k for k in fault.unanswerable if k)
            self.unreadable.update(k for k in fault.unreadable if k)
            self.shuffle = self.shuffle or fault.shuffle
            self.listing_fails = self.listing_fails or fault.listing_fails

    def describe(self) -> str:
        return "a local mirror carrying faults"

    def list_keys(self) -> List[str]:
        if self.listing_fails:
            raise self._error("the listing could not be completed")
        keys = [k for k in self.inner.list_keys() if k not in self.hidden]
        keys += list(self.extra)
        if self.shuffle:
            import random
            random.Random(7).shuffle(keys)
        return keys

    def exists(self, key: str) -> bool:
        if key in self.unanswerable:
            raise self._error(f"the store could not answer for {key}")
        if key in self.denied:
            return False
        return key in self.extra or self.inner.exists(key)

    def fetch(self, key: str) -> bytes:
        if key in self.unreadable:
            raise self._error(f"the store would not hand over {key}")
        if key in self.swap_bytes:
            return self.swap_bytes[key]
        return self.inner.fetch(self.swap_keys.get(key, key))


def shard_document_key(root: str, index: str, shard: int, generation: int,
                       numeric: bool = False) -> str:
    """The key one shard document lives at, whichever id shape was written.

    Found on disk rather than computed, so a caller that does not track which
    shape a fixture used still reaches the right object.
    """
    for candidate in (shard_generation_id(index, shard, generation, numeric),
                      shard_generation_id(index, shard, generation, not numeric)):
        key = f"{directory_of(index, shard)}/index-{candidate}"
        if os.path.exists(os.path.join(root, key)):
            return key
    return f"{directory_of(index, shard)}/index-" + shard_generation_id(
        index, shard, generation, numeric)


def forge_document(per_snapshot: Mapping[str, Sequence[str]],
                   commit: str = "segments_9") -> bytes:
    """A well-formed shard document carrying exactly these file lists.

    Used to build the donors that SATISFY a check and are still wrong: a
    document naming nothing, a document naming only inline entries, and a
    document whose blobs are contained in the directory it is planted in.
    """
    names = sorted({n for files in per_snapshot.values() for n in files})
    return codec_wrap(json.dumps({
        "files": [{"name": n,
                   "physical_name": commit if n.startswith("v__") else f"_{n[2:]}.cfs",
                   "length": FILE_LENGTH, "checksum": "cksum",
                   "written_by": "9.5.2", "writer_uuid": "forged-writer"}
                  for n in names],
        "snapshots": {name: {"files": list(files), "shard_state_id": "forged"}
                      for name, files in per_snapshot.items()},
    }, sort_keys=True).encode("utf-8"))
