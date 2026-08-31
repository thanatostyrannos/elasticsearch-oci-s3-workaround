"""The one HTTP read both endpoint transports make, and its retry policy.

Both stores are read with GET and nothing else. The allowlist below is the
runtime half of "version one has no delete path": a later change that wanted
to delete would have to remove the line that says why it must not, rather than
quietly assembling a method string at run time.

The retry policy is policy rather than plumbing, and each part of it changes
what an operator gets. Throttling is retried because a repository listing is
thousands of calls and any store throttles some. A 403 and a 404 are answers
rather than weather, so retrying them turns one wrong credential into eight
and delays the same message. A store that is simply down ends the run with a
refusal, because a read that never completed must never look like a read that
returned nothing.
"""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Mapping, Optional

from ..errors import ForbiddenMethod, SourceReadError

# This package reads. It has no --execute, no --approve and no delete branch,
# so DELETE and POST are not merely unused here, they are unreachable.
# HEAD is here because confirming that an object is still there before naming
# it in a manifest is a read. DELETE and POST are not merely unused, they are
# unreachable, and a later change that wanted one would have to delete the
# line saying why not.
ALLOWED_METHODS = frozenset({"GET", "HEAD"})
DEFAULT_TIMEOUT_SECONDS = 60.0
USER_AGENT = "generation-chain-auditor (python-urllib)"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 8
    budget_seconds: float = 600.0
    base_seconds: float = 1.0
    growth_factor: float = 2.0
    max_sleep_seconds: float = 30.0
    retry_statuses: FrozenSet[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str) -> Optional[str]:
        """Case-insensitive lookup; Oracle sends `opc-next-page` lowercase."""
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class HttpReader:
    """Performs reads with the retry policy, or raises SourceReadError."""

    def __init__(self, policy: RetryPolicy = RetryPolicy(),
                 sleep: Callable[[float], None] = time.sleep,
                 opener: Callable = urllib.request.urlopen,
                 jitter: Callable[[], float] = random.random,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = policy
        self._sleep = sleep
        self._opener = opener
        self._jitter = jitter
        self._clock = clock

    def get(self, url: str, headers: Mapping[str, str], method: str = "GET",
            timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Response:
        # NOT an assert. `python3 -O` strips assert, and this single check is
        # what makes "reads and never deletes" true. Under -O the stripped
        # version let a DELETE through to the transport, measured, so the
        # strongest promise this tool makes held only for people who happened
        # not to use a flag that exists to make Python faster.
        if method not in ALLOWED_METHODS:
            raise ForbiddenMethod(
                f"{method} is not a method this package may send. It reads "
                f"and never deletes, so only {sorted(ALLOWED_METHODS)} are "
                "possible. Reclaiming is a separate tool that a human "
                "approves.")
        started = self._clock()
        last = ""
        for attempt in range(self.policy.max_attempts):
            try:
                return self._once(url, headers, method, timeout)
            except urllib.error.HTTPError as exc:
                last = f"{exc.code} from {url}: {_detail(exc)}"
                if exc.code not in self.policy.retry_statuses:
                    raise SourceReadError(last) from exc
                pause = self._pause(attempt, _retry_after(exc))
            except Exception as exc:
                # Deliberately broad. The stated contract is that a read
                # produces bytes or a SourceReadError, and `IncompleteRead`
                # is an `http.client.HTTPException` rather than an `OSError`,
                # so a narrow tuple left three real truncation cases escaping
                # as tracebacks.
                last = f"cannot reach {url}: {type(exc).__name__}: {exc}"
                pause = self._pause(attempt, None)
            if attempt + 1 >= self.policy.max_attempts:
                break
            if self._clock() - started + pause > self.policy.budget_seconds:
                break
            self._sleep(pause)
        raise SourceReadError(last or f"no answer from {url}")

    def _once(self, url: str, headers: Mapping[str, str], method: str,
              timeout: float) -> Response:
        request = urllib.request.Request(url, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        request.add_header("User-Agent", USER_AGENT)
        with self._opener(request, timeout=timeout) as response:
            return Response(status=getattr(response, "status", 200),
                            headers=dict(getattr(response, "headers", {})),
                            body=response.read())

    def _pause(self, attempt: int, retry_after: Optional[float]) -> float:
        """Full jitter over an exponential backoff, with Retry-After capped.

        Ignoring Retry-After hammers a store that just asked for room. Obeying
        it uncapped parks the run for the hundreds of seconds stores really
        send, with nothing on screen.
        """
        if retry_after is not None:
            return min(retry_after, self.policy.max_sleep_seconds)
        ceiling = min(self.policy.max_sleep_seconds,
                      self.policy.base_seconds
                      * self.policy.growth_factor ** attempt)
        return self._jitter() * ceiling


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    raw = (exc.headers or {}).get("Retry-After") if exc.headers else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _detail(exc: urllib.error.HTTPError) -> str:
    """A scrap of the store's response body, for the error message.

    This is decoration and must never become the failure. The caller is
    already raising; a helper that adds context has no business deciding
    the exception type.

    So it catches everything, which the narrower `(OSError, AttributeError)`
    did not. Broad on purpose rather than by accident: nothing this helper can
    hit is worth turning into the caller's exception.

    It reads that way because of a real failure. On Python 3.9 an HTTPError
    carrying no body raised `KeyError('file')` here, `addinfourl` having
    subclassed `tempfile._TemporaryFileWrapper` so its `__getattr__` reached
    for a key nobody set. The floor has moved past that interpreter and the
    read now returns empty bytes. The specific bug is gone. The reason to
    catch broadly is not.
    """
    try:
        return exc.read()[:200].decode("utf-8", "replace")
    except Exception:
        return ""


_DEFAULT_READER = HttpReader()


def get(url: str, headers: Mapping[str, str], method: str = "GET",
        timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Response:
    return _DEFAULT_READER.get(url, headers, method=method, timeout=timeout)
