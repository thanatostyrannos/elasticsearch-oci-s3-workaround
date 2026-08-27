"""One `DeleteObjects` request: the body built once, and its response read
per key.

`build_request_body` is called exactly once per batch and its return value is
the one `bytes` object that gets checksummed, hashed for `x-amz-content-sha256`
and sent. Nothing here re-renders the body to compute a second thing about it;
a checksum computed over a re-rendered copy proves nothing about the bytes the
store actually receives, which is the same scan-versus-send gap this project
has already written about elsewhere.

`parse_response` is the other half of the point of this package. A batch
delete answers 200 for a request where some keys failed inside it, so the
per-key `<Deleted>` and `<Error>` elements are the only source of truth about
which of the requested keys are actually gone. A key this function cannot
account for from either list is `unconfirmed`, never `deleted`: this project
exists because a 200 was trusted as a whole, and treating an unlisted key as
successful would be that same mistake one layer up.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator, List, Sequence, Tuple

from ..errors import GenerationChainError

NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
MAX_KEYS_PER_BATCH = 1000

# Error codes a store uses to say "this key was never here", distinct from a
# genuine failure to delete something that was. Reported as `already_absent`
# rather than `failed` because it is not a defect in the delete, and distinct
# from `deleted` because nothing was removed by this call.
NOT_FOUND_CODES = frozenset({"NoSuchKey", "NotFound"})


class BatchDeleteError(GenerationChainError):
    """The store's response could not be read as a `DeleteResult` at all.

    Every key in the batch that produced this is `unconfirmed`, because a
    response this package cannot parse is not evidence that any of them were
    removed.
    """


def chunks(keys: Sequence[str],
          size: int = MAX_KEYS_PER_BATCH) -> Iterator[Sequence[str]]:
    """`keys` split at the S3 multi-object delete limit, order preserved."""
    if size < 1:
        raise ValueError(f"a batch size of {size} cannot hold a key")
    for start in range(0, len(keys), size):
        yield keys[start:start + size]


def build_request_body(keys: Sequence[str]) -> bytes:
    """The `<Delete>` XML body for one batch, rendered exactly once.

    `Quiet` is always false. A quiet response omits `<Deleted>` entries, and
    this package's whole reason for existing is reading every key's own
    outcome rather than trusting the request as a unit.
    """
    root = ET.Element("Delete", {"xmlns": NAMESPACE})
    ET.SubElement(root, "Quiet").text = "false"
    for key in keys:
        object_element = ET.SubElement(root, "Object")
        ET.SubElement(object_element, "Key").text = key
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _local(tag: str) -> str:
    """The element name with any XML namespace prefix stripped."""
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class BatchOutcome:
    """What one batch's response said about each key that was asked for."""

    deleted: Tuple[str, ...] = field(default_factory=tuple)
    already_absent: Tuple[Tuple[str, str, str], ...] = field(
        default_factory=tuple)
    failed: Tuple[Tuple[str, str, str], ...] = field(default_factory=tuple)
    unconfirmed: Tuple[str, ...] = field(default_factory=tuple)


def parse_response(body: bytes, requested: Sequence[str]) -> BatchOutcome:
    """Read a `DeleteResult` document, one key at a time.

    `requested` is the exact list this batch asked the store to delete. A key
    named there that appears in neither `<Deleted>` nor `<Error>` is a store
    that answered a request it did not fully honour, and it is `unconfirmed`
    rather than assumed either way.
    """
    if b"<!DOCTYPE" in body[:2048].lstrip():
        # Same reasoning as the listing parser in sources/s3.py: a store does
        # not send a DOCTYPE, and entity expansion inside one can be made to
        # exhaust this process. This response decides which keys are reported
        # deleted, so it is refused rather than parsed.
        raise BatchDeleteError(
            "the delete response declares a DOCTYPE, which a store does not "
            "send; refused rather than parsed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise BatchDeleteError(
            f"the delete response is not XML: {exc}") from exc
    if _local(root.tag) != "DeleteResult":
        raise BatchDeleteError(
            f"the delete response's root element is {root.tag!r}, not "
            "DeleteResult")

    deleted: List[str] = []
    already_absent: List[Tuple[str, str, str]] = []
    failed: List[Tuple[str, str, str]] = []
    accounted = set()

    for child in root:
        name = _local(child.tag)
        key = _text(child, "Key")
        if key is None:
            raise BatchDeleteError(
                f"a {name!r} entry in the delete response carries no Key")
        if name == "Deleted":
            deleted.append(key)
            accounted.add(key)
        elif name == "Error":
            code = _text(child, "Code") or ""
            message = _text(child, "Message") or ""
            if code in NOT_FOUND_CODES:
                already_absent.append((key, code, message))
            else:
                failed.append((key, code, message))
            accounted.add(key)
        else:
            raise BatchDeleteError(
                f"the delete response carries an unrecognised element "
                f"{child.tag!r}")

    unconfirmed = tuple(key for key in requested if key not in accounted)
    return BatchOutcome(deleted=tuple(deleted),
                        already_absent=tuple(already_absent),
                        failed=tuple(failed), unconfirmed=unconfirmed)


def _text(element: ET.Element, local_name: str):
    for child in element:
        if _local(child.tag) == local_name:
            return child.text
    return None
