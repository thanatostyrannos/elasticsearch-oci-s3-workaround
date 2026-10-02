"""The exception taxonomy, and what each one means for the output.

Every exception here is caught somewhere and turned into LESS output. That is
the whole safety model, so the taxonomy is small on purpose: a reader deciding
where to catch should never have to guess whether a new exception class means
"skip this blob" or "skip this shard".
"""

from __future__ import annotations

from typing import Optional

DENIAL_STATUSES = (401, 403)


class ForbiddenMethod(Exception):
    """A request this package must never send was built anyway.

    Deliberately OUTSIDE the GenerationChainError tree. Three places in
    `derivation/` catch SourceReadError to mean "this read failed, so drop
    what it would have told us", which resolves safely for a read. A
    forbidden method is not a read that failed. It is the one thing this
    package promises it cannot do, and it must reach the operator rather than
    be folded into a coverage number.
    """


class GenerationChainError(Exception):
    """Base for everything this package raises deliberately."""


class SourceReadError(GenerationChainError):
    """The store did not hand over bytes.

    A missing object, a 403, a timeout and a truncated body all land here,
    because the derivation treats them identically: an operation it cannot
    read is an operation it cannot explain.

    `status` carries the HTTP status when the store answered one, so a caller
    can tell a store that refuses this credential from one that is busy. It is
    None for a failure with no status, such as a refused connection.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_denial(self) -> bool:
        """The store answered 401 or 403, and will answer the same again."""
        return self.status in DENIAL_STATUSES


class BlobFormatError(GenerationChainError):
    """Bytes arrived and are not the document they were supposed to be."""


class ShapeGateError(BlobFormatError):
    """The document parsed and does not look like what it claims to be.

    Separate from BlobFormatError because the cause is different: this is a
    well-formed JSON object that Elasticsearch would never have written, which
    is what a co-tenant's file or a renamed field looks like.
    """


class UnsupportedRepository(ShapeGateError):
    """The repository is a shape this tool does not support.

    Separate from a malformed document because nothing is wrong with it. A
    catalog written before Elasticsearch 7.12 names index metadata a different
    way and carries neither `index_metadata_lookup` nor
    `index_metadata_identifiers`, and the cross-checks that keep this tool's
    live set complete are built entirely on those two fields.

    Half support is the hole. Accepting the shape while the guards cannot see
    into it reads as support and behaves as an unguarded path, so the shape is
    refused at the door instead.
    """


class RunRefused(GenerationChainError):
    """The run cannot be anchored, so it produces no manifest at all.

    Raised only for the two facts everything else hangs off: the current root
    generation, and the repository uuid that says which generation blobs are
    ours. Without either, nothing downstream can be attributed to anything.

    `transient` says whether running the same command again could succeed. A
    scheduled job derives success from the exit code and retries on it, so a
    store that answered 503 and a repository whose format is unsupported must
    not look alike: retrying the first is right, and retrying the second burns
    the backoff to reach the same answer.

    `needs_a_bigger_host` says whether the same command would succeed on a
    host with more memory. A scheduled job that only reads `transient` cannot
    tell "try again here" from "try this somewhere else", and those are
    different instructions to send an operator or a scheduler.

    `invocation_is_wrong` says the command line or the credential names
    something the store will not accept, such as a revoked key or a prefix
    with no repository under it. A retry gets the same answer, and the person
    who can fix it is the one running the command.
    """

    needs_a_bigger_host = False

    def __init__(self, message: str, transient: bool = False,
                 invocation_is_wrong: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
        self.invocation_is_wrong = invocation_is_wrong


def denial_refusal(exc: SourceReadError, what: str) -> RunRefused:
    """The refusal for a store that answered 401 or 403 to `what`."""
    return RunRefused(
        f"{what}: the store refused this credential ({exc}). The access key, "
        "secret, region or bucket policy is wrong, and a retry gets the same "
        "answer",
        invocation_is_wrong=True)
