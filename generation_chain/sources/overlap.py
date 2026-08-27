"""Running store reads at the same time, without changing what they return.

Every read this package makes is a round trip to a store on the other side of
a network, and the measured cost is entirely that round trip: on the scale
harness a run of 894 generations spent 100 percent of its wall clock waiting,
one request at a time, and never had two requests outstanding. At a 40
millisecond round trip that is 163.5 seconds for the narrowest repository that
exists, and 32 minutes for 53,063 objects over a thousand shard directories.

OVERLAP MUST NOT MOVE THE ANSWER. That is the constraint everything here is
shaped by, and it is met by keeping the concurrency underneath a memo. Work is
submitted by KEY, the outcome of each key is recorded against that key, and
the caller consumes keys in exactly the order it always did. A thread decides
WHEN bytes arrive; nothing about a thread decides WHICH bytes, or which error,
belongs to a key. An exception is held and re-raised, unchanged, at the moment
the caller asks, so a failure lands in the same place in the derivation as it
did when the read was serial.

ONE POOL FOR THE PROCESS. A pool per wrapper would leave threads behind on
every run, and a test suite building hundreds of sources would accumulate
them. The pool here is created once, on first use, and bounded. Callers keep
their own record of what they submitted, keyed by key, and settle it in an
order they choose rather than in the order the threads finished.

Reads only. This module has no way to express a write, because the package it
serves has none.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional, Tuple, TypeVar

# How many reads may be outstanding at once, counting every kind together.
# Eight against a 40 millisecond round trip took a 894 generation audit from
# 149 seconds to 43. Higher numbers keep paying, but a store that throttles
# answers a burst with 429s and 503s and the retry backoff then costs more
# than the overlap saved. Eight is the setting this package ships;
# --concurrency 1 restores the fully serial behaviour for a store that cannot
# take it.
DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 32

T = TypeVar("T")

_lock = threading.Lock()
_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None


def pool() -> concurrent.futures.ThreadPoolExecutor:
    """The one executor this process uses, built on first use."""
    global _pool
    with _lock:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_CONCURRENCY,
                thread_name_prefix="generation-chain-read")
        return _pool


def clamp(concurrency: int) -> int:
    """Keep a caller's request inside what this package will actually run."""
    return max(1, min(int(concurrency), MAX_CONCURRENCY))


class Budget:
    """How many store reads this run may have outstanding, all kinds together.

    One of these per run, shared by everything that reads. Without a shared
    cap `--concurrency 8` would mean eight document reads AND eight existence
    checks AND whatever a later caller added, which is not what an operator
    asking a throttling store for eight requests at a time means.

    `submit` waits for room rather than queueing without limit, so a caller
    warming ten thousand keys hands the store the same number of requests at
    once as a caller warming three.
    """

    def __init__(self, width: int = DEFAULT_CONCURRENCY) -> None:
        self.width = clamp(width)
        self._room = threading.Semaphore(self.width)

    def submit(self, call: Callable[[str], T], key: str):
        """A future carrying `(value, error)` for one key. Never raises here."""
        self._room.acquire()
        try:
            return pool().submit(self._run, call, key)
        except BaseException:
            self._room.release()
            raise

    def _run(self, call: Callable[[str], T], key: str):
        try:
            return outcome(call, key)
        finally:
            self._room.release()


def outcome(call: Callable[[str], T], key: str) -> Tuple[Optional[T],
                                                        Optional[BaseException]]:
    """One call, with its result captured as `(value, error)` rather than raised.

    The error is carried back to the caller's thread instead of surfacing in
    a worker, so the caller decides what a failed read means and decides it in
    the place the serial version decided it. Every caller in this package
    turns one into less output.
    """
    try:
        return call(key), None
    except Exception as exc:  # re-raised by the caller, never swallowed
        return None, exc
