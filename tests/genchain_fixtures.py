"""Synthetic snapshot repositories for the generation-chain tests.

Every repository this builds LEAKS. Deleting a snapshot removes it from the
next root generation and from the next shard document, and leaves every blob
and every superseded document in place. That is the fault this project exists
for, and it is the only state in which a generation-chain derivation has
anything to read.

The builder writes Elasticsearch's formats by hand rather than calling the
tool that reads them, so a test failure means the reader disagrees with the
format rather than with itself.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

CODEC_MAGIC = 0x3FD76C17
# One length for every file entry, so a declared size is a simple multiple and
# a test that shortens a file list changes the total visibly.
FILE_LENGTH = 42
FOOTER_MAGIC = (~CODEC_MAGIC) & 0xFFFFFFFF

# A generation is snapshot name -> index name -> shard number -> blob names.
ShardMap = Mapping[int, Sequence[str]]
IndexMap = Mapping[str, Union[Sequence[str], ShardMap]]
GenerationSpec = Mapping[str, IndexMap]


def _lucene_vint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def codec_wrap(payload: bytes, codec_name: str = "snapshots",
               version: int = 1, deflate: bool = False) -> bytes:
    """Wrap a payload the way ChecksumBlobStoreFormat does."""
    if deflate:
        payload = b"DFL\x00" + zlib.compress(payload)
    body = (struct.pack(">I", CODEC_MAGIC)
            + _lucene_vint(len(codec_name)) + codec_name.encode("utf-8")
            + struct.pack(">I", version)
            + payload)
    footer = struct.pack(">I", FOOTER_MAGIC) + struct.pack(">I", 0)
    crc = zlib.crc32(body + footer) & 0xFFFFFFFF
    return body + footer + struct.pack(">Q", crc)


def _write(root: str, rel: str, data: bytes) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _normalise(spec: GenerationSpec) -> Dict[str, Dict[str, Dict[int, List[str]]]]:
    out: Dict[str, Dict[str, Dict[int, List[str]]]] = {}
    for snap, indices in spec.items():
        out[snap] = {}
        for index, shards in indices.items():
            if isinstance(shards, Mapping):
                out[snap][index] = {int(s): list(f) for s, f in shards.items()}
            else:
                out[snap][index] = {0: list(shards)}
    return out


def snapshot_uuid(key: str) -> str:
    return "uuid-" + key


def snapshot_name(key: str) -> str:
    """A history key of the form `name#tag` is one snapshot of several by that name.

    Elasticsearch identifies a snapshot by uuid in the root catalog and by NAME
    in a shard document, so a name that has belonged to two snapshots over time
    is the state where those two views cannot be joined. Building it needs a
    fixture that can give one name two uuids.
    """
    return key.split("#", 1)[0]


def index_uuid(name: str) -> str:
    return "iuuid-" + name


def shard_generation_id(index: str, shard: int, gen: int, numeric: bool) -> str:
    if numeric:
        return str(gen)
    return f"sg-{index}-{shard}-{gen}"


def build_repository(
    root: str,
    history: Sequence[GenerationSpec],
    repo_uuid: str = "repo-uuid-aaaa",
    numeric_shard_generations: bool = False,
    index_metadata: bool = True,
    latest: Optional[int] = None,
    min_version: str = "7.12.0",
) -> Dict[str, Any]:
    """Write a leaking repository whose root generations are 0..len(history)-1.

    Returns the facts a test needs to assert against: which blobs exist, and
    which snapshot each generation held.
    """
    states = [_normalise(g) for g in history]
    all_blobs: set = set()

    for gen, state in enumerate(states):
        indices_in_gen: Dict[str, Dict[str, Any]] = {}
        for snap, indices in state.items():
            for index, shards in indices.items():
                entry = indices_in_gen.setdefault(index, {
                    "id": index_uuid(index),
                    "snapshots": [],
                    "shard_generations": [],
                })
                if snapshot_uuid(snap) not in entry["snapshots"]:
                    entry["snapshots"].append(snapshot_uuid(snap))
                width = max(shards) + 1 if shards else 0
                while len(entry["shard_generations"]) < width:
                    n = len(entry["shard_generations"])
                    entry["shard_generations"].append(
                        shard_generation_id(index, n, gen, numeric_shard_generations))

        root_doc: Dict[str, Any] = {
            "min_version": min_version,
            "uuid": repo_uuid,
            "cluster_id": "cluster-aaaa",
            "snapshots": [
                {
                    "name": snapshot_name(snap),
                    "uuid": snapshot_uuid(snap),
                    "state": 1,
                    "index_metadata_lookup": {
                        index_uuid(i): index_uuid(i) + "-md"
                        for i in state[snap]
                    },
                    "version": "8.11.0",
                }
                for snap in sorted(state)
            ],
            "indices": indices_in_gen,
        }
        if index_metadata:
            root_doc["index_metadata_identifiers"] = {
                index_uuid(i) + "-md": "md-" + i
                for snap in state for i in state[snap]
            }
        _write(root, f"index-{gen}", json.dumps(root_doc).encode("utf-8"))

        # Shard documents of this era: every snapshot the generation still
        # holds, with the files it named in that shard.
        per_shard: Dict[tuple, Dict[str, List[str]]] = {}
        for snap, indices in state.items():
            for index, shards in indices.items():
                for shard, files in shards.items():
                    per_shard.setdefault((index, shard), {})[snap] = list(files)
                    all_blobs.update(files)
        for (index, shard), snaps in per_shard.items():
            names = sorted({f for files in snaps.values() for f in files})
            # Every snapshot entry names a Lucene commit, which real
            # Elasticsearch keeps as an inline `v__` entry. Verified against
            # the captured 9.5.2 repository: all twelve of its snapshot
            # entries name one, from segments_3 to segments_t.
            commit = f"v__commit-{index}-{shard}-{gen}"
            # Lucene's IndexWriter identity: stable for a shard across its
            # generations, distinct between shards. Measured on the rig, where
            # two shards of one index shared none and two generations of one
            # shard shared eight.
            writer = f"writer-{index}-{shard}"
            doc = {
                "files": [
                    {"name": n, "physical_name": "_" + n[2:],
                     "length": FILE_LENGTH, "writer_uuid": writer,
                     "checksum": "abc", "written_by": "9.11.1"}
                    for n in names
                ] + [
                    {"name": commit, "physical_name": f"segments_{gen + 1}",
                     "length": FILE_LENGTH, "writer_uuid": writer,
                     "checksum": "abc", "written_by": "9.11.1"}
                ],
                "snapshots": {
                    snapshot_name(snap): {
                        "files": list(files) + [commit],
                        "shard_state_id": f"state-{index}-{shard}-{gen}"}
                    for snap, files in snaps.items()
                },
            }
            gid = shard_generation_id(index, shard, gen, numeric_shard_generations)
            _write(root, f"indices/{index_uuid(index)}/{shard}/index-{gid}",
                   codec_wrap(json.dumps(doc).encode("utf-8"), deflate=(gen % 2 == 1)))

        for snap in state:
            # A real `snap-<uuid>.dat`, because the derivation reads it now:
            # it declares the snapshot's extent, and that declaration is what
            # turns a live set that came up short into a contradiction.
            details = {}
            shards_total = 0
            for index, shards in state[snap].items():
                details[index] = {
                    "shard_count": len(shards),
                    "size_in_bytes": sum((len(files) + 1) * FILE_LENGTH
                                         for files in shards.values()),
                    "max_segments_per_shard": 1,
                }
                shards_total += len(shards)
            _write(root, f"snap-{snapshot_uuid(snap)}.dat", codec_wrap(
                json.dumps({"snapshot": {
                    "name": snapshot_name(snap), "uuid": snapshot_uuid(snap),
                    "indices": sorted(state[snap]), "state": "SUCCESS",
                    "total_shards": shards_total,
                    "successful_shards": shards_total,
                    "index_details": details,
                }}).encode("utf-8"), codec_name="snapshot"))
            _write(root, f"meta-{snapshot_uuid(snap)}.dat", b"global metadata")
            for index, shards in state[snap].items():
                if index_metadata:
                    _write(root, f"indices/{index_uuid(index)}/meta-md-{index}.dat",
                           b"index metadata")
                for shard in shards:
                    _write(root,
                           f"indices/{index_uuid(index)}/{shard}/snap-{snapshot_uuid(snap)}.dat",
                           b"shard snapshot document")

    for snap, indices in [(s, i) for st in states for s, i in st.items()]:
        for index, shards in indices.items():
            for shard, files in shards.items():
                for blob in files:
                    _write(root, f"indices/{index_uuid(index)}/{shard}/{blob}",
                           b"segment data")

    current = len(states) - 1 if latest is None else latest
    _write(root, "index.latest", struct.pack(">q", current))
    return {"blobs": all_blobs, "generations": len(states), "latest": current,
            "repo_uuid": repo_uuid}


def read_keys(root: str) -> List[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


def corrupt(root: str, rel: str, data: bytes = b"not a document") -> None:
    with open(os.path.join(root, rel), "wb") as fh:
        fh.write(data)


def remove(root: str, rel: str) -> None:
    os.unlink(os.path.join(root, rel))


def truncate(root: str, rel: str, keep: int) -> None:
    path = os.path.join(root, rel)
    with open(path, "rb") as fh:
        data = fh.read()
    with open(path, "wb") as fh:
        fh.write(data[:keep])
