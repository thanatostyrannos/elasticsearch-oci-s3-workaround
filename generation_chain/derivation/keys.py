"""The store's listing, indexed, and the store's second opinion about it.

A listing is an input like any other. It was taken at some earlier moment, it
can lag, it can over-report, and an entry for an object already deleted would
otherwise go straight into a manifest an operator acts on. So the listing
narrows the candidates and the store settles each one.

WHY THE ANSWER IS THREE-VALUED. The retired version of this class caught every
exception from the existence check and recorded False, which made "the store
says it does not hold this" and "the store could not answer" the same value.
Measurement showed what that costs: with HEAD failing 1 in 1000, about 31 of
30,938 keys silently left the manifest while the report still claimed 100%
coverage. It leaks rather than deletes, so it is not the dangerous direction,
and it was the only measured place where this tool's report was WRONG rather
than conservative. Three values fix the report without changing the decision:
anything that is not a confirmation still keeps the key out.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..sources import RepositorySource

CONFIRMED = "confirmed"
DENIED = "denied"
UNANSWERED = "unanswered"

PART_SUFFIX = re.compile(r"\.part(0|[1-9][0-9]*)$")


class KeyIndex:
    """Every key the listing gave, and what the store says about each on demand.

    Nothing reaches the manifest without appearing here first, and nothing
    leaves this class as present without the store confirming it a second time.
    """

    def __init__(self, keys: Iterable[str], source: "RepositorySource") -> None:
        self._source = source
        self._verdict: Dict[str, str] = {}
        self._keys: Set[str] = set(keys)
        self._parts: Dict[str, List[str]] = {}
        for key in self._keys:
            match = PART_SUFFIX.search(key)
            if match:
                self._parts.setdefault(key[:match.start()], []).append(key)

    @property
    def listed(self) -> Set[str]:
        """What the listing said, before the store was asked about any of it."""
        return set(self._keys)

    @property
    def unanswered(self) -> List[str]:
        """Keys the store could neither confirm nor deny.

        Reported rather than folded into the denials, because an operator
        reading a manifest needs to know the difference between "this key is
        gone" and "this run could not find out".
        """
        return sorted(k for k, v in self._verdict.items() if v == UNANSWERED)

    def __contains__(self, key: str) -> bool:
        return key in self._keys and self.confirm(key) == CONFIRMED

    def objects_for(self, key: str) -> List[str]:
        """The objects carrying one blob: itself, or its `.partN` pieces.

        A file longer than the repository's part size has no object under its
        bare name at all, only the pieces, so asking for the bare name and
        stopping there would miss every large segment.
        """
        candidates = sorted(self._parts.get(key, []))
        if key in self._keys:
            candidates.append(key)
        return sorted(c for c in candidates if self.confirm(c) == CONFIRMED)

    def confirm(self, key: str) -> str:
        """CONFIRMED, DENIED, or UNANSWERED, cached for the run."""
        if key not in self._verdict:
            self._verdict[key] = self._ask(key)
        return self._verdict[key]

    def _ask(self, key: str) -> str:
        try:
            return CONFIRMED if self._source.exists(key) else DENIED
        except Exception:
            # A store that raised did not say no. Recording it as a denial is
            # what made the coverage report claim keys it had silently dropped.
            return UNANSWERED
