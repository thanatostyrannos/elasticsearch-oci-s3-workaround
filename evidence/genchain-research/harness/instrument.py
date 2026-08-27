"""Counting, delaying and failing transports, wrapped around a real one.

Three things this measures that reading the source cannot settle: how many
store calls one run makes, whether any two of them ever overlap, and what the
run does when some of them fail.

The failure injection is deterministic. A seed and a key decide whether that
read fails, so a run that produced a surprising manifest can be replayed
exactly.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from generation_chain.errors import SourceReadError

PAGE_SIZE = 1000


@dataclass
class Counters:
    list_calls: int = 0
    list_pages: int = 0
    fetch_calls: int = 0
    exists_calls: int = 0
    bytes_read: int = 0
    injected_failures: Dict[str, int] = field(default_factory=dict)
    max_in_flight: int = 0

    @property
    def requests(self) -> int:
        """Store round trips, counting a listing as its pages."""
        return self.list_pages + self.fetch_calls + self.exists_calls


class InstrumentedSource:
    """Wraps any source. Counts every call, and optionally delays or fails it.

    `latency` is per round trip, so a listing of P pages costs P delays. That
    matches what the endpoint transports do, and it is the only way a local
    mirror can stand in for a store on the other side of a network.

    `fail_rate` is the chance that ONE round trip ends in a SourceReadError
    that no retry recovered. It is deliberately applied per round trip rather
    than per key, so the listing of a large repository is more likely to fail
    than the listing of a small one, which is the real behaviour.
    """

    def __init__(self, inner, latency: float = 0.0, fail_rate: float = 0.0,
                 seed: int = 0, fail_listing: bool = True,
                 page_size: int = PAGE_SIZE, fail_ops=("list", "fetch", "exists")
                 ) -> None:
        self._inner = inner
        self.latency = latency
        self.fail_rate = fail_rate
        self.seed = seed
        self.fail_listing = fail_listing
        self.page_size = page_size
        # Which call kinds may fail. Narrowing this to one kind is how the
        # harness tells apart a failure the coverage report accounts for from
        # one it does not mention at all.
        self.fail_ops = frozenset(fail_ops)
        self.counters = Counters()
        self._lock = threading.Lock()
        self._in_flight = 0
        self.failed_keys: List[str] = []

    # -- plumbing ---------------------------------------------------------

    def _enter(self) -> None:
        with self._lock:
            self._in_flight += 1
            if self._in_flight > self.counters.max_in_flight:
                self.counters.max_in_flight = self._in_flight

    def _leave(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def _roll(self, token: str) -> bool:
        """Deterministic coin, so a surprising run can be replayed."""
        if self.fail_rate <= 0:
            return False
        digest = hashlib.sha256(f"{self.seed}:{token}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
        return draw < self.fail_rate

    def _delay(self, round_trips: int = 1) -> None:
        if self.latency > 0:
            time.sleep(self.latency * round_trips)

    def _count_failure(self, kind: str, token: str) -> None:
        self.counters.injected_failures[kind] = \
            self.counters.injected_failures.get(kind, 0) + 1
        self.failed_keys.append(f"{kind}:{token}")

    # -- the source interface ---------------------------------------------

    def describe(self) -> str:
        return self._inner.describe()

    def list_keys(self) -> List[str]:
        self._enter()
        try:
            self.counters.list_calls += 1
            keys = self._inner.list_keys()
            pages = max(1, math.ceil(len(keys) / self.page_size))
            self.counters.list_pages += pages
            self._delay(pages)
            if self.fail_listing and "list" in self.fail_ops:
                for page in range(pages):
                    if self._roll(f"list:{page}"):
                        self._count_failure("list", f"page{page}")
                        raise SourceReadError(
                            f"the listing failed on page {page}")
            return keys
        finally:
            self._leave()

    def fetch(self, key: str) -> bytes:
        self._enter()
        try:
            self.counters.fetch_calls += 1
            self._delay()
            if "fetch" in self.fail_ops and self._roll(f"fetch:{key}"):
                self._count_failure("fetch", key)
                raise SourceReadError(f"cannot read {key}: injected failure")
            data = self._inner.fetch(key)
            self.counters.bytes_read += len(data)
            return data
        finally:
            self._leave()

    def exists(self, key: str) -> bool:
        self._enter()
        try:
            self.counters.exists_calls += 1
            self._delay()
            if "exists" in self.fail_ops and self._roll(f"exists:{key}"):
                self._count_failure("exists", key)
                raise SourceReadError(f"cannot confirm {key}: injected failure")
            return self._inner.exists(key)
        finally:
            self._leave()
