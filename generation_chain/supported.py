"""The repository format this tool supports, and the precondition that enforces it.

Declared here rather than left as a consequence of some function needing a
field. An operator and a contributor should both find the answer in one place.

WHAT IS SUPPORTED. A root generation whose `min_version` is 7.12.0 or later.

`min_version` is RepositoryData's own declaration of the minimum Elasticsearch
version able to READ the repository, which Elasticsearch raises when it writes
something that needs newer code. That reading was verified by measurement
rather than taken on trust: two independent repositories written by
Elasticsearch 9.5.2, the captured fixture in tests/fixtures and a repository
built on the test cluster, both carry `min_version: 7.12.0` on every
generation. A field naming the WRITER's version would say 9.5.2 there. It says
the floor.

WHY THE DECLARATION RATHER THAN A SNIFF. The obvious alternative is to look
for `index_metadata_identifiers` and `index_metadata_lookup`, the two fields
the guards below actually consume. Three reasons not to:

  * A declaration says what the format is. Field presence infers it, and an
    inference silently changes meaning the day Elasticsearch adds or removes
    an unrelated field.
  * `min_version` is on EVERY generation. A chain can span a format change,
    because the older generations are in the bucket precisely due to the leak,
    so a per-generation declaration makes that visible instead of assumed.
  * A sniff invites a later contributor to relax it one field at a time.

WHY 7.12.0. From that version Elasticsearch writes `index_metadata_identifiers`
in the catalog and `index_metadata_lookup` on every snapshot. Those two are the
second source that proves the catalog's `indices` map is complete: the map and
the lookups are written from one state, so each checks the other, and that
check is what makes "this index is not in the map" an answer rather than an
unanswered question. Without it, an index missing from the map reads as an
empty live set for its shards, which is how this tool would come to name a
blob a live snapshot still references.

Half support is the hole, not a compromise. A reviewer produced exactly that
failure: the metadata functions accepted an old catalog by name while the
guard protecting the live set could not see into it, and the manifest grew by
a live key with no store misbehaving at all. A precondition removes the
possibility of any code path quietly accepting a shape the guards do not
cover.

A MISSING OR UNREADABLE `min_version` IS UNSUPPORTED, not "probably fine".
Absence is not evidence anywhere else in this package and it is not evidence
here.

WHAT HAPPENS TO A CHAIN THAT SPANS THE FLOOR. The anchor generation must be
supported or the run refuses, because everything is measured against it. An
OLDER generation below the floor is dropped from the derivation with its
reason recorded, rather than taking the whole run down. That is the same
choice this package makes everywhere else: evidence it cannot read is evidence
it does not use, the manifest gets shorter, and the coverage report says how
much shorter.

WHY THE CHECK READS THE CATALOG AND NOT THE ELASTICSEARCH API. Asking the
cluster is the obvious-looking answer and it is the wrong one, three times
over. Measured against the 9.5.2 test cluster rather than recalled: `GET /`
reports `version.number`, `minimum_wire_compatibility_version` and
`minimum_index_compatibility_version`, and `GET /_snapshot/<repo>` reports
`type`, `uuid` and `settings`. Neither reports what format a repository is in.

  * The cluster version is not the repository format. This tool reads a chain
    of historical generations, so a generation written years ago by an older
    cluster sits beside one written yesterday. The catalog in front of you
    describes itself; today's cluster does not describe it.
  * This tool talks to no Elasticsearch, and that is load bearing. It reads
    the store's own record, which is what lets it run against a local mirror,
    against a bucket whose cluster is gone, and from a host with no cluster
    credentials. It also keeps it clear of the common-mode path in issue #21,
    where a corroboration asked Elasticsearch and Elasticsearch read the same
    bucket. A version number is not worth reintroducing that.
  * Even free, it would be the weaker signal. A cluster can report 9.5.2 while
    its repository still holds generations in the shape this tool refuses.

An operator who meets the refusal can run `GET /` and `GET /_snapshot/<repo>`
themselves to understand their cluster. That is a diagnostic for a human, and
never something this package does.

This floor costs nothing real. The delete fault this project exists for needs
Elasticsearch 8.19.17 or 9.5.0 and later to be performing the deletes, so a
catalog below the floor is not a repository this tool will meet.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Tuple

from .errors import UnsupportedRepository

MINIMUM_SUPPORTED_REPOSITORY_FORMAT: Tuple[int, int, int] = (7, 12, 0)
MIN_VERSION_FIELD = "min_version"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

SUPPORTED_SUMMARY = (
    "This tool supports a snapshot repository whose root generation declares "
    "min_version "
    + ".".join(str(part) for part in MINIMUM_SUPPORTED_REPOSITORY_FORMAT)
    + " or later, which is RepositoryData's own statement of the minimum "
    "Elasticsearch version able to read it. An older or undeclared catalog is "
    "refused rather than partly read, because the cross-check that keeps the "
    "live set complete is built on fields Elasticsearch only writes from that "
    "version. This tool asks Elasticsearch nothing; the catalog declares its "
    "own format."
)


def parse_version(value: Any) -> Tuple[int, int, int]:
    """The three leading numbers of a version string."""
    if not isinstance(value, str):
        raise UnsupportedRepository(
            f"min_version is {type(value).__name__}, not a version string")
    match = _VERSION.match(value.strip())
    if not match:
        raise UnsupportedRepository(f"min_version {value!r} is not a version")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def require_supported_format(document: Mapping[str, Any],
                             generation: int) -> None:
    """Refuse an unsupported catalog before anything is derived from it.

    Called on the raw decoded document, before the record the derivation uses
    is built, so no live set, no classification and no manifest can come from
    a shape the guards do not cover.
    """
    if MIN_VERSION_FIELD not in document:
        raise UnsupportedRepository(
            f"generation {generation} declares no {MIN_VERSION_FIELD}. "
            + SUPPORTED_SUMMARY)
    try:
        declared = parse_version(document[MIN_VERSION_FIELD])
    except UnsupportedRepository as exc:
        raise UnsupportedRepository(
            f"generation {generation}: {exc}. " + SUPPORTED_SUMMARY) from None
    if declared < MINIMUM_SUPPORTED_REPOSITORY_FORMAT:
        raise UnsupportedRepository(
            f"generation {generation} declares min_version "
            f"{document[MIN_VERSION_FIELD]}. " + SUPPORTED_SUMMARY)
