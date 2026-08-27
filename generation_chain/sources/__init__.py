"""The transport boundary.

One derivation reads through one interface, and the three transports sit
behind it. The split in this project's older tools, where the same derivation
was written twice for two stores and then drifted apart, is the thing this
boundary exists to prevent.

The contract is deliberately tiny and deliberately fail-closed. `fetch` either
returns bytes or raises `SourceReadError`, and every caller in the derivation
turns that exception into less output. A transport that returned b"" for a
missing object, or that let a socket error escape as something else, would be
the one way this package could be made to name MORE keys.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable

from ..errors import GenerationChainError, SourceReadError
from . import overlap
from .readahead import CriticalReads, ReadAhead


@runtime_checkable
class RepositorySource(Protocol):
    """Read-only access to the objects of one snapshot repository.

    Keys are relative to the repository root, so `index-3` and
    `indices/<uuid>/0/__abc`. Whatever prefix or base path the store needs is
    the transport's business, which keeps the manifest comparable with the
    reachability sweeper's, whose keys are relative in the same way.
    """

    def describe(self) -> str:
        """One line naming the transport and the location, for the report."""

    def list_keys(self) -> List[str]:
        """Every key under the repository root.

        Raises SourceReadError if the listing cannot be completed. A partial
        listing is never returned, because a listing that silently stopped
        early looks exactly like a repository with fewer objects in it.
        """

    def fetch(self, key: str) -> bytes:
        """The bytes of one object, or SourceReadError."""

    def exists(self, key: str) -> bool:
        """Whether the store holds this object right now.

        A listing is a snapshot of the store taken earlier, and an entry for
        an object already deleted would otherwise put that key into a manifest
        an operator acts on. Anything other than a definite yes raises, and
        the caller treats a raise as a no.
        """

    def fetch_critical(self, key: str) -> bytes:
        """A read whose failure ends the run, so it retries for longer.

        Optional. A transport without one is read the ordinary way, which is
        the behaviour every transport had before this existed.
        """

    def prefetch(self, keys: List[str]) -> None:
        """A hint that these keys are about to be read, in this order.

        Optional, advisory, and never load bearing. It changes when bytes
        arrive and never which bytes arrive, so a transport that ignores it
        is correct and merely slower.
        """


class GuardedSource:
    """Makes the contract above TRUE for any source, including a third party's.

    `http.client.IncompleteRead` is neither an `OSError` nor a `URLError`, so
    a body shorter than its Content-Length, a chunked response that stops
    early and a socket closed after the headers all used to escape the
    derivation as a traceback. A crash is fail-closed by accident rather than
    by design, and an invariant that is stated and false is worse than one
    that is not stated, so every escape is converted here at the boundary.

    Only the store calls are wrapped. A bug in this package's own parsing
    still raises, because that is a defect rather than a store being a store.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def describe(self) -> str:
        return self._inner.describe()


    def sizes(self) -> Dict[str, int]:
        """Whatever the wrapped transport could size, unchanged.

        Delegated explicitly. A wrapper that silently drops this turns the
        reclaimable figure off without failing anything.
        """
        sizer = getattr(self._inner, "sizes", None)
        return sizer() if callable(sizer) else {}

    def list_keys(self) -> List[str]:
        return self._guard("list the repository", self._inner.list_keys)

    def fetch(self, key: str) -> bytes:
        return self._bytes(key, self._guard(f"read {key}",
                                            self._inner.fetch, key))

    def fetch_critical(self, key: str) -> bytes:
        reader = getattr(self._inner, "fetch_critical", None)
        if reader is None:
            return self.fetch(key)
        return self._bytes(key, self._guard(f"read {key}", reader, key))

    @staticmethod
    def _bytes(key: str, data) -> bytes:
        if not isinstance(data, (bytes, bytearray)):
            raise SourceReadError(
                f"reading {key} produced {type(data).__name__}, not bytes")
        return bytes(data)

    def exists(self, key: str) -> bool:
        checker = getattr(self._inner, "exists", None)
        if checker is None:
            # A source with no existence check cannot confirm anything, and an
            # unconfirmed key never reaches the manifest.
            raise SourceReadError(
                f"{type(self._inner).__name__} cannot confirm that {key} is "
                "still there")
        return bool(self._guard(f"confirm {key}", checker, key))

    @staticmethod
    def _guard(what: str, call, *args):
        try:
            return call(*args)
        except GenerationChainError:
            # A decision this package made on purpose, such as a refusal or a
            # malformed document. Dressing one up as a store failure would
            # tell an operator to retry something that will not change.
            raise
        except Exception as exc:
            raise SourceReadError(
                f"cannot {what}: {type(exc).__name__}: {exc}") from exc


def prepared(source, concurrency: int = overlap.DEFAULT_CONCURRENCY):
    """One source, wrapped in everything a run needs of it.

    Three layers, innermost first. `GuardedSource` makes the contract above
    TRUE, so nothing escapes as a traceback. `CriticalReads` gives the three
    reads that can end a run a longer retry policy than the reads that only
    shorten it. `ReadAhead` overlaps the rest.

    None of the three can change what a key answers. That is the property
    this project's own determinism tests hold, and it is why this stack was
    allowed anywhere near a derivation nobody wants rewritten.
    """
    if isinstance(source, ReadAhead):
        # Already prepared by the caller, who may have chosen a different
        # concurrency. Wrapping it again would silently override that choice.
        return source
    return ReadAhead(CriticalReads(GuardedSource(source)),
                     concurrency=concurrency)


def hint(source, keys) -> None:
    """Tell a source what is about to be read, if it is a source that cares.

    Advisory in both directions: a transport with no `prefetch` is correct and
    merely slower, and a caller that stops making these calls gets the same
    manifest at the speed the package had before read-ahead existed.
    """
    ahead = getattr(source, "prefetch", None)
    if ahead is not None:
        ahead(keys)
