"""Deciding whether this repository fits on this host, before reading it.

Measured on the scale harness: 1.9 KB resident per object, linear all the way
to 585,194 objects at 1.55 GB peak. A 2 GB host dies near 750,000 objects, and
it dies at the END of a run, after thirty minutes of round trips, with an OOM
kill, no manifest, and nothing on screen connecting the two.

The cost per object is what it is because the run holds the whole listing, the
keys indexed for the two questions attribution asks, and one parsed document
per shard directory per generation. Shaving that moves the cliff rather than
removing it, and a cliff that moved is still a cliff nobody sees coming.

So this measures at the door instead. The listing arrives, the object count is
known, and a run that will not fit says so in one sentence naming the count,
the estimate and the flag that overrides it. An operator reading that in the
first thirty seconds moves the job to a bigger host or narrows the prefix. An
operator reading an OOM kill at minute thirty cannot tell it from a crash.

WHAT IT WILL NOT DO IS GUESS. On anything that is not Linux, and inside some
containers, there is no number here to read. No number means no opinion and no
refusal, because stopping a run that would have completed is a worse failure
than the one this exists to prevent.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional

from ..errors import RunRefused

# Measured, not estimated. `bench_memory.py` on the scale harness reports
# resident bytes against object count across five repository shapes, and the
# slope is flat at this value from ten thousand objects to 585,194.
RESIDENT_BYTES_PER_OBJECT = 1900

# What fraction of what the host says is available this run may plan to use.
# The rest is the interpreter, the transport buffers and whatever else shares
# the host. Planning to use all of it means planning to be killed at the end.
USABLE_SHARE = 0.8

MEMINFO = "/proc/meminfo"
CGROUP_MAX = "/sys/fs/cgroup/memory.max"
CGROUP_V1_MAX = "/sys/fs/cgroup/memory/memory.limit_in_bytes"


class RepositoryTooLarge(RunRefused):
    """This repository does not fit in the memory this host will give the run.

    A RunRefused, so it lands where every other refusal lands and produces a
    coverage record rather than a traceback. It is not transient, because
    running the same command on the same host reaches the same answer after
    burning the backoff, and it carries `needs_a_bigger_host` so a scheduled
    caller can route it somewhere with more memory rather than giving up.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)
        self.needs_a_bigger_host = True


class MemoryBudget:
    """Wraps a source and refuses a listing bigger than this host can hold."""

    def __init__(self, inner, limit_bytes: Optional[int] = None,
                 bytes_per_object: int = RESIDENT_BYTES_PER_OBJECT) -> None:
        self._inner = inner
        self.limit_bytes = limit_bytes
        self.bytes_per_object = bytes_per_object

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
        keys = self._inner.list_keys()
        self._check(len(keys))
        return keys

    def fetch(self, key: str) -> bytes:
        return self._inner.fetch(key)

    def fetch_critical(self, key: str) -> bytes:
        reader = getattr(self._inner, "fetch_critical", None)
        return reader(key) if reader is not None else self._inner.fetch(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def _check(self, objects: int) -> None:
        if not self.limit_bytes or self.limit_bytes <= 0:
            return
        needed = objects * self.bytes_per_object
        if needed <= self.limit_bytes:
            return
        raise RepositoryTooLarge(
            f"this repository lists {objects} objects, which this run needs "
            f"about {_megabytes(needed)} MB of memory to hold, and only "
            f"{_megabytes(self.limit_bytes)} MB is available to it. Nothing "
            "was read. Run it on a host with more memory, narrow it with "
            "--prefix, or raise the ceiling with --memory-mb if this host "
            "really has more than it reports")


def _megabytes(value: int) -> int:
    return value // (1 << 20)


def available_bytes() -> Optional[int]:
    """What this run may plan on using, or None when the host does not say.

    Both numbers matter and the smaller one wins. A container with a 2 GB
    limit on a 128 GB node reads MemAvailable and sees 128 GB, then gets
    killed at 2 GB, so the limit the kernel will enforce is the one to
    believe.
    """
    smallest = _smallest([_meminfo_available(), _cgroup_limit()])
    if smallest is None:
        return None
    return int(smallest * USABLE_SHARE)


def _smallest(values: Iterable[Optional[int]]) -> Optional[int]:
    known = [value for value in values if value is not None]
    return min(known) if known else None


def _meminfo_available() -> Optional[int]:
    try:
        with open(MEMINFO, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cgroup_limit() -> Optional[int]:
    """The container's own ceiling, if it has one it is willing to state.

    cgroup v2 writes "max" when there is no limit, and v1 writes a number so
    large it means the same thing. Both are read as no opinion.
    """
    for path in (CGROUP_MAX, CGROUP_V1_MAX):
        try:
            with open(path, encoding="ascii") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # v1 spells "no limit" as a number near the top of a signed 64 bit
        # range, which is not a ceiling anybody is going to hit.
        if 0 < value < (1 << 62):
            return value
    return None


def with_budget(source, megabytes: Optional[int] = None):
    """A source that refuses a repository this host cannot hold.

    `megabytes` is the operator's own ceiling. Without one the host is asked,
    and a host that does not answer gets no opinion rather than a guess.
    """
    if megabytes is not None and megabytes <= 0:
        return source
    limit = (megabytes * (1 << 20)) if megabytes else available_bytes()
    return MemoryBudget(source, limit_bytes=limit)
