"""Reading ahead of the derivation, and reading the three fatal keys harder.

Two wrappers live here and both sit between the derivation and a transport.

READ AHEAD. `prefetch` is a HINT and never anything else. A caller names keys
it is about to ask for, in the order it will ask for them, and this warms a
bounded window of them. `fetch` then hands back exactly what that key produced,
bytes or the original exception, and falls back to a plain read for anything
that was not warmed. Delete every `prefetch` call in the package and the
manifest is byte for byte what it was; the run just waits for one round trip at
a time again, which is what it did before this existed.

The window is what keeps memory bounded. How many reads are outstanding is
the shared budget's business, so the window is not what limits concurrency; it
limits how many warmed bodies are held at once. An unbounded read-ahead over a
repository this package already measures at 1.9 KB resident per object would
hold a body for every key in it.

READ THE FATAL ONES HARDER. Exactly three reads end a run: the listing,
`index.latest`, and the root generation `index.latest` names. Everything else
degrades locally, so a failure there costs a few keys and says so. Refusal
probability scales with `listing_pages + 2`, which at 129,000 objects and a one
percent error rate refuses 69 percent of runs, and each of those retries costs
the whole thirty minutes over again. So these three get a longer retry policy
than an ordinary read.

`CriticalReads` recognises the anchor the same way `derivation/chain.py` does,
by reading the generation number out of `index.latest` as it goes past. That
coupling is deliberate and pinned by a test, because a wrapper that escalated
the wrong key would be silently useless.
"""

from __future__ import annotations

import collections
from typing import Deque, Dict, Iterable, List, Optional

from ..errors import GenerationChainError
from ..formats.latest import INDEX_LATEST_KEY, parse_index_latest
from . import overlap


class CriticalReads:
    """Marks the reads whose failure ends the run, so the transport retries."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._anchor: Optional[str] = None

    def describe(self) -> str:
        return self._inner.describe()

    def sizes(self) -> Dict[str, int]:
        """Whatever the wrapped transport could size, unchanged.

        A wrapper that quietly drops this turns the reclaimable figure off
        without failing anything, so it is delegated explicitly rather than
        left to __getattr__.
        """
        sizer = getattr(self._inner, "sizes", None)
        return sizer() if callable(sizer) else {}

    def list_keys(self) -> List[str]:
        # A listing is fatal by construction: there is no partial listing and
        # no run without one. The transports apply the longer policy to every
        # page of it themselves.
        return self._inner.list_keys()

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def fetch(self, key: str) -> bytes:
        if key == INDEX_LATEST_KEY:
            data = self._critical(key)
            self._anchor = _anchor_key(data)
            return data
        if self._anchor is not None and key == self._anchor:
            return self._critical(key)
        return self._inner.fetch(key)

    @property
    def anchor_key(self) -> Optional[str]:
        """The root generation this wrapper will escalate, once it knows it."""
        return self._anchor

    def _critical(self, key: str) -> bytes:
        reader = getattr(self._inner, "fetch_critical", None)
        return reader(key) if reader is not None else self._inner.fetch(key)


def _anchor_key(data: bytes) -> Optional[str]:
    """The generation blob `index.latest` names, or None if it does not parse.

    An unparseable `index.latest` refuses the run a moment later in the
    derivation. There is nothing to escalate and nothing to say here.
    """
    try:
        return f"index-{parse_index_latest(data)}"
    except GenerationChainError:
        return None


class ReadAhead:
    """Warms a bounded window of the reads a caller says it is about to make."""

    def __init__(self, inner,
                 concurrency: int = overlap.DEFAULT_CONCURRENCY) -> None:
        self._inner = inner
        self.concurrency = overlap.clamp(concurrency)
        # Shared with everything else that reads for this run, so the number
        # an operator gave is the number of requests the store sees.
        self.budget = overlap.Budget(self.concurrency)
        self._plan: Deque[str] = collections.deque()
        self._warm: Dict[str, object] = {}

    # -- the source interface ---------------------------------------------

    def describe(self) -> str:
        return self._inner.describe()

    def sizes(self) -> Dict[str, int]:
        """Whatever the wrapped transport could size, unchanged.

        A wrapper that quietly drops this turns the reclaimable figure off
        without failing anything, so it is delegated explicitly rather than
        left to __getattr__.
        """
        sizer = getattr(self._inner, "sizes", None)
        return sizer() if callable(sizer) else {}

    def list_keys(self) -> List[str]:
        return self._inner.list_keys()

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def fetch(self, key: str) -> bytes:
        self._fill()
        future = self._warm.pop(key, None)
        if future is None:
            return self._inner.fetch(key)
        value, error = future.result()
        if error is not None:
            raise error
        return value

    # -- the hint ---------------------------------------------------------

    def prefetch(self, keys: Iterable[str]) -> None:
        """Name the keys about to be read, in the order they will be read.

        Replaces any previous plan. A caller that abandons one leaves at most
        a window's worth of warmed bodies behind, which is dropped here rather
        than held for the rest of the run.
        """
        self._discard()
        self._plan = collections.deque(keys)
        self._fill()

    @property
    def warmed(self) -> int:
        """How many warmed bodies are being held right now."""
        return len(self._warm)

    def _fill(self) -> None:
        while self._plan and len(self._warm) < self.concurrency:
            key = self._plan.popleft()
            if key in self._warm:
                continue
            self._warm[key] = self.budget.submit(self._inner.fetch, key)

    def _discard(self) -> None:
        for future in self._warm.values():
            future.cancel()
        self._warm.clear()
