"""The manifest another tool reads, and the classification an operator reads.

The comparison against the reachability sweeper happens outside this package,
with `comm` over the first column, exactly as the log-driven cross-check
already works. That makes the column order a contract rather than a
presentation choice.

Nothing with a tab, a newline or a control character in it is ever written.
Snapshot names and object keys come out of the repository, so a name holding a
newline would append a row to a manifest an operator is about to act on, which
is the one way this package could be made to name a key it never derived.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, TextIO

from ..derivation.classification import Placement
from ..model import Condemnation

MANIFEST_COLUMNS: Sequence[str] = (
    "key", "reason", "category", "snapshot_uuid", "snapshot_name",
    "from_generation", "to_generation")
CLASSIFICATION_COLUMNS: Sequence[str] = ("key", "disposition", "detail")

UNSAFE = re.compile(r"[\x00-\x1f\x7f]")

# A `#`-prefixed line, so it sorts and greps apart from the tab-separated
# rows above it without changing their column count. Written to a manifest
# FILE only, and only once every row has been written, so its presence is
# what tells a reviewer the file in front of them describes the whole
# repository rather than a run that stopped partway. `write_manifest` never
# writes this itself: a caller writing to a file appends it, and a caller
# reading the return value of `write_manifest` alone (this module's own
# tests, and the direct callers in tests/test_generation_chain_liveness.py)
# never sees rows that were not actually written pretending otherwise.
COMPLETION_MARKER = "# derivation complete\n"


def is_writable_key(key: str) -> bool:
    """Whether a key can appear in a tab separated file without lying.

    A key carrying a tab or a newline cannot be written to this format at all.
    Escaping it would put a key in the manifest that does not match the key in
    the store, and an operator comparing manifests by identity would act on
    the escaped spelling.
    """
    return bool(key) and not UNSAFE.search(key)


def _field(value: object) -> str:
    return UNSAFE.sub(" ", str(value))


def excluded_keys(condemned: Iterable[Condemnation]) -> List[str]:
    return [c.key for c in condemned if not is_writable_key(c.key)]


def write_manifest(condemned: Iterable[Condemnation], stream: TextIO) -> int:
    stream.write("\t".join(MANIFEST_COLUMNS) + "\n")
    written = 0
    for row in condemned:
        if not is_writable_key(row.key):
            continue
        stream.write("\t".join([
            row.key, _field(row.reason), _field(row.category),
            _field(row.snapshot_uuid), _field(row.snapshot_name),
            str(row.from_generation), str(row.to_generation),
        ]) + "\n")
        written += 1
    return written


def write_classification(placements: Iterable[Placement],
                         stream: TextIO) -> int:
    stream.write("\t".join(CLASSIFICATION_COLUMNS) + "\n")
    written = 0
    for placement in placements:
        if not is_writable_key(placement.key):
            continue
        stream.write("\t".join([
            placement.key, _field(placement.disposition),
            _field(placement.detail)]) + "\n")
        written += 1
    return written
