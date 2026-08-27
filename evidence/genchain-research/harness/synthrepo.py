"""Parameterised leaking snapshot repositories, built large.

The formats here are written by hand, in the same shapes the project's own
test fixture builder uses, because the point is to feed the reader something
it did not produce itself. What this adds over that builder is scale knobs:
generation depth, index breadth, shard breadth, blobs per shard, and the
size of the live snapshot window.

The chain it writes alternates: a generation that ADDS one snapshot, then a
generation that DELETES the oldest once the live window is full. Elasticsearch
writes one root generation per operation, and the auditor refuses to interpret
a step that both adds and removes, so a chain built any other way measures the
refusal rather than the work.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from typing import Dict, List, Optional, Sequence

CODEC_MAGIC = 0x3FD76C17
FOOTER_MAGIC = (~CODEC_MAGIC) & 0xFFFFFFFF


def _lucene_vint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def codec_wrap(payload: bytes, codec_name: str = "snapshots",
               version: int = 1, deflate: bool = False) -> bytes:
    if deflate:
        payload = b"DFL\x00" + zlib.compress(payload)
    body = (struct.pack(">I", CODEC_MAGIC)
            + _lucene_vint(len(codec_name)) + codec_name.encode("utf-8")
            + struct.pack(">I", version)
            + payload)
    footer = struct.pack(">I", FOOTER_MAGIC) + struct.pack(">I", 0)
    crc = zlib.crc32(body + footer) & 0xFFFFFFFF
    return body + footer + struct.pack(">Q", crc)


class Writer:
    """Collects objects in memory, then puts them wherever the run needs them."""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def to_disk(self, root: str) -> None:
        made = set()
        for key, data in self.objects.items():
            path = os.path.join(root, key)
            directory = os.path.dirname(path)
            if directory not in made:
                os.makedirs(directory, exist_ok=True)
                made.add(directory)
            with open(path, "wb") as handle:
                handle.write(data)


def index_uuid(n: int) -> str:
    return f"iuuid-{n:05d}"


def snapshot_uuid(n: int) -> str:
    return f"uuid-snap-{n:06d}"


def snapshot_name(n: int) -> str:
    return f"snap-{n:06d}"


FILE_LENGTH = 42


def build(generations: int, indices: int = 1, shards: int = 1,
          blobs_per_shard_per_snapshot: int = 2, live_window: int = 3,
          repo_uuid: str = "repo-uuid-scale", root: Optional[str] = None,
          snapshot_documents: Optional[bool] = None) -> Writer:
    """Write `generations` root generations of a repository that leaks.

    Returns the writer holding every object, so a caller can put them on disk
    or into a bucket without building them twice.
    """
    if snapshot_documents is None:
        # Later versions of the tool read `snap-<uuid>.dat` and check the
        # extent it declares. Set GENCHAIN_SNAPSHOT_DOCUMENTS=1 to write real
        # ones, so a bench can be pointed at such a version without editing.
        snapshot_documents = os.environ.get("GENCHAIN_SNAPSHOT_DOCUMENTS") == "1"
    writer = Writer()
    live: List[int] = []
    next_snapshot = 0
    # Which blobs each (snapshot, index, shard) contributed. A snapshot's file
    # list is its own blobs plus everything the snapshots before it left in
    # the shard, which is how Elasticsearch's incremental shard really reads.
    blobs_of: Dict[int, List[str]] = {}
    history: List[List[int]] = []

    for generation in range(generations):
        if generation == 0:
            live.append(next_snapshot)
            blobs_of[next_snapshot] = list(range(blobs_per_shard_per_snapshot))
            next_snapshot += 1
        elif generation % 2 == 1 and len(live) >= live_window:
            live.pop(0)
        else:
            live.append(next_snapshot)
            blobs_of[next_snapshot] = list(range(blobs_per_shard_per_snapshot))
            next_snapshot += 1
        history.append(list(live))
        _write_generation(writer, generation, live, indices, shards,
                          blobs_per_shard_per_snapshot, repo_uuid,
                          snapshot_documents)

    # Segment blobs, one object each. Written once, and never removed, which
    # is the whole fault this project exists for.
    for snapshot in range(next_snapshot):
        for i in range(indices):
            for s in range(shards):
                for j in range(blobs_per_shard_per_snapshot):
                    writer.put(
                        f"indices/{index_uuid(i)}/{s}/"
                        f"{_blob(snapshot, j, i, s)}", b"segment")
    writer.put("index.latest", struct.pack(">q", generations - 1))
    writer.history = history          # type: ignore[attr-defined]
    writer.snapshots = next_snapshot  # type: ignore[attr-defined]
    if root is not None:
        writer.to_disk(root)
    return writer


def _blob(snapshot: int, j: int, index: int, shard: int) -> str:
    """A blob name that exists in exactly one directory.

    Lucene blob names are globally unique ids, and the auditor leans on that:
    a document that names no blob unique to its own directory is one it
    refuses to attribute. A generator that reused a name across shards would
    measure that refusal instead of the work.
    """
    return f"__b{index:04d}_{shard:04d}_{snapshot:06d}_{j:03d}"


def _snapshot_document(n: int, indices: int, shards: int, blobs: int) -> bytes:
    """`snap-<uuid>.dat`, declaring the extent later versions check against.

    `size_in_bytes` is the TOTAL this snapshot occupies in that index, summed
    over the shard file lists including the inline Lucene commit entry, which
    is what the reader computes from the current shard documents.
    """
    per_index = shards * (blobs + 1) * FILE_LENGTH
    document = {"snapshot": {
        "uuid": snapshot_uuid(n),
        "name": snapshot_name(n),
        "indices": [index_uuid(i) for i in range(indices)],
        "state": "SUCCESS",
        "total_shards": indices * shards,
        "successful_shards": indices * shards,
        "index_details": {index_uuid(i): {"shard_count": shards,
                                          "size_in_bytes": per_index,
                                          "max_segments_per_shard": 1}
                          for i in range(indices)},
    }}
    return codec_wrap(json.dumps(document).encode("utf-8"), "snapshot")


def _write_generation(writer: Writer, generation: int, live: Sequence[int],
                      indices: int, shards: int, blobs: int,
                      repo_uuid: str, snapshot_documents: bool = False) -> None:
    index_entries: Dict[str, dict] = {}
    for i in range(indices):
        index_entries[index_uuid(i)] = {
            "id": index_uuid(i),
            "snapshots": [snapshot_uuid(n) for n in live],
            "shard_generations": [f"sg-{i}-{s}-{generation}"
                                  for s in range(shards)],
        }
    root_doc = {
        "min_version": "7.12.0",
        "uuid": repo_uuid,
        "cluster_id": "cluster-scale",
        "snapshots": [
            {"name": snapshot_name(n), "uuid": snapshot_uuid(n), "state": 1,
             "index_metadata_lookup": {index_uuid(i): index_uuid(i) + "-md"
                                       for i in range(indices)},
             "version": "8.11.0"}
            for n in live],
        "indices": index_entries,
        "index_metadata_identifiers": {index_uuid(i) + "-md": f"md-{i}"
                                       for i in range(indices)},
    }
    writer.put(f"index-{generation}",
               json.dumps(root_doc).encode("utf-8"))

    for i in range(indices):
        writer.put(f"indices/{index_uuid(i)}/meta-md-{i}.dat", b"index metadata")
        for s in range(shards):
            names = sorted({_blob(n, j, i, s) for n in live
                            for j in range(blobs)})
            commit = f"v__commit-{i}-{s}-{generation}"
            document = {
                "files": [{"name": n, "physical_name": "_" + n[2:],
                           "length": FILE_LENGTH, "checksum": "abc",
                           "written_by": "9.11.1"} for n in names]
                         + [{"name": commit,
                             "physical_name": f"segments_{generation + 1}",
                             "length": FILE_LENGTH, "checksum": "abc",
                             "written_by": "9.11.1",
                             "writer_uuid": f"w-{i}-{s}"}],
                "snapshots": {
                    snapshot_name(n): {
                        "files": [_blob(n, j, i, s) for j in range(blobs)]
                             + [commit],
                        "shard_state_id": f"state-{i}-{s}-{generation}"}
                    for n in live},
            }
            writer.put(
                f"indices/{index_uuid(i)}/{s}/index-sg-{i}-{s}-{generation}",
                codec_wrap(json.dumps(document).encode("utf-8"),
                           deflate=(generation % 2 == 1)))
    for n in live:
        writer.put(f"snap-{snapshot_uuid(n)}.dat",
                   _snapshot_document(n, indices, shards, blobs)
                   if snapshot_documents else b"snapshot document")
        writer.put(f"meta-{snapshot_uuid(n)}.dat", b"global metadata")
        for i in range(indices):
            for s in range(shards):
                writer.put(
                    f"indices/{index_uuid(i)}/{s}/snap-{snapshot_uuid(n)}.dat",
                    b"shard snapshot document")
