"""Parsing a memory size an operator typed, without guessing a unit.

`--memory-mb 4096` and `--memory-mb 4` differ by a typo and by three orders of
magnitude, and neither was refused. A tool that refuses an ambiguous
repository should refuse an ambiguous size the same way, so `--max-ram` takes
a unit and rejects anything that does not name one.
"""

from __future__ import annotations

import re

# Binary units only. Elasticsearch, the object stores this tool reads, and the
# operators running it all reason in KiB/MiB/GiB, so a decimal "GB" would be
# one more place a typed number quietly means something other than what an
# operator expects.
_UNITS = {
    "b": 1,
    "kib": 1 << 10,
    "mib": 1 << 20,
    "gib": 1 << 30,
    "tib": 1 << 40,
}

_PATTERN = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(" + "|".join(_UNITS) + r")\s*$",
    re.IGNORECASE)


class InvalidSize(ValueError):
    """The text is not a size this tool will accept.

    A `ValueError` subclass, so `argparse` reports it as a usage error, the
    same way it reports a bad `type=int` conversion.
    """


def parse_byte_size(text: str) -> int:
    """A byte count from a string like "4GiB" or "512MiB".

    A bare number is refused rather than assumed to be bytes, or megabytes, or
    whatever unit the last flag this tool shipped happened to use. An unknown
    suffix is refused for the same reason: guessing the closest unit would
    trade one silent misconfiguration for another.
    """
    match = _PATTERN.match(text)
    if not match:
        if text.strip().replace(".", "", 1).isdigit():
            raise InvalidSize(
                f"{text!r} names no unit. Write it as 4GiB or 512MiB; a bare "
                "number is refused rather than guessed as bytes or megabytes")
        raise InvalidSize(
            f"{text!r} is not a size this tool understands. Use a number "
            f"followed by one of: {', '.join(sorted(_UNITS, key=_UNITS.get))}"
            " (for example 4GiB)")
    number, unit = match.groups()
    return int(float(number) * _UNITS[unit.lower()])
