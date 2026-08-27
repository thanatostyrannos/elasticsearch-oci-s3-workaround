"""Read a manifest, and refuse one that cannot prove it is whole.

`reporting/manifest.py` writes a header row, one row per condemned key, and
(since issue #7) a trailing `# derivation complete` line. The CLI appends
that COMPLETION_MARKER to a manifest FILE only once every row is written and
only when the run was not refused, so this module requires it. Its absence
means one of three things, none of them safe to act on: the run refused, the
manifest was written to stdout and redirected by hand rather than through
`--manifest FILE`, or the process was killed before reaching the marker line.
All three read the same way here: untrusted, and the file is refused.

TWO SEPARATE GUARANTEES, NOT ONE. The marker proves the DERIVATION finished.
It says nothing about what happened to the file afterward: a manifest hand
edited or copied after the marker was written still carries a marker that
reads as valid, because the marker is a property of how the bytes were
produced, not of what they currently say. That gap is `approval.py`'s job,
not this module's: approval ties a digest to the EXACT bytes an operator is
about to act on, so an edited or superseded manifest fails there even when it
fails nothing here. Marker and approval answer two different questions, and
the safety argument needs both answered.

The structural checks that remain, a trailing newline and a matching column
count on every row, are belt-and-suspenders on top of the marker rather than
instead of it: they catch the ordinary shapes a corrupted or hand-spliced
file actually leaves even when a marker line, correct or forged, sits at the
end of it.

WHY THE KEY LIST IS NEVER SORTED, DEDUPLICATED OR OTHERWISE TOUCHED. The
manifest names what a delete should remove; this reader's only job is to hand
that list back exactly as given, in the order given, duplicates included. Any
transformation, however harmless it looks, is this module starting to derive
keys instead of reading them, which is the one thing the design here forbids.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Tuple

from ..errors import GenerationChainError
from ..reporting.manifest import COMPLETION_MARKER, MANIFEST_COLUMNS

EXPECTED_HEADER = "\t".join(MANIFEST_COLUMNS)
_KEY_COLUMN = MANIFEST_COLUMNS.index("key")
_MARKER_LINE = COMPLETION_MARKER.rstrip("\n")


class ManifestError(GenerationChainError):
    """The file named is not a manifest this package can safely act on."""


@dataclass(frozen=True)
class ManifestData:
    """One manifest, read once: its keys in file order, and its exact digest.

    Both come from the same read of the same bytes, so an approval checked
    against `digest` and a delete run against `keys` can never drift apart
    from a second, later read seeing a file that changed underneath.
    """

    path: str
    keys: Tuple[str, ...]
    digest: str
    byte_length: int


def load_manifest(path: str) -> ManifestData:
    """Parse and structurally validate a manifest in one pass over one read.

    Raises `ManifestError` for anything that is not unambiguously a complete,
    well-formed, marked-whole manifest: a missing or foreign header, a row
    with the wrong number of columns, a file that does not end on a newline,
    or a file with no completion marker as its last line.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ManifestError(f"cannot read the manifest {path}: {exc}") from None

    digest = hashlib.sha256(raw).hexdigest()
    if not raw:
        raise ManifestError(
            f"{path} is empty. A manifest always carries at least a header "
            "row; an empty file is not a manifest naming zero keys, it is "
            "not a manifest")
    if not raw.endswith(b"\n"):
        raise ManifestError(
            f"{path} does not end on a newline. A write stopped partway "
            "through its last row looks exactly like this, so the file is "
            "refused rather than read as though its last row were complete")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{path} is not valid UTF-8: {exc}") from None

    lines = text.split("\n")
    assert lines[-1] == "", "raw ends with \\n, so split leaves a trailing empty"
    lines.pop()
    header, rest = lines[0], lines[1:]
    if header != EXPECTED_HEADER:
        raise ManifestError(
            f"{path} does not start with the manifest header this package "
            f"knows how to read. Expected {EXPECTED_HEADER!r}, found "
            f"{header!r}. A file from a different tool, a different version "
            "of this one, or a listing rather than a manifest, is refused "
            "rather than guessed at")
    if not rest or rest[-1] != _MARKER_LINE:
        raise ManifestError(
            f"{path} carries no {_MARKER_LINE!r} as its last line. That "
            "marker is written only once every row is in place and only "
            "when the derivation was not refused, so its absence means the "
            "run refused, the file was written to stdout and redirected by "
            "hand instead of through --manifest FILE, or the write never "
            "reached this line. None of those is a manifest this package "
            "may act on")
    rows = rest[:-1]

    keys: List[str] = []
    for number, line in enumerate(rows, start=2):
        fields = line.split("\t")
        if len(fields) != len(MANIFEST_COLUMNS):
            raise ManifestError(
                f"{path} line {number} has {len(fields)} tab separated "
                f"field(s), not {len(MANIFEST_COLUMNS)}. A row cut short by "
                "an interrupted write has exactly this shape, so the whole "
                "manifest is refused rather than read up to this line")
        keys.append(fields[_KEY_COLUMN])

    return ManifestData(path=path, keys=tuple(keys), digest=digest,
                        byte_length=len(raw))
