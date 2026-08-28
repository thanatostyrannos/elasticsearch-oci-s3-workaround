"""Which paths this tool opens, and which it refuses before opening them.

Every path here was typed by whoever ran the command: --manifest,
--credentials, --report, --classification, --coverage-json. A path is data,
and this module treats it the way the rest of the package treats a key read
out of a listing, rather than as something already known to be safe.

ALWAYS CHECKED. An empty path names nothing. A path holding a NUL byte makes
`open` raise a bare ValueError from inside a write that has already announced
its target, which reads as a crash rather than a refusal. Both are refused
here. What comes back is expanded, absolute, and resolved through its
symlinks, so the place an error message names and the place that gets opened
are the same place.

CHECKED WHEN SOMEONE ASKS FOR IT. GENCHAIN_FILE_ROOT names a directory every
path must resolve inside. It is unset by default, and deliberately so: an
operator running the audit by hand writes the manifest wherever they keep
evidence, and a root hardcoded here would refuse every real invocation. It is
for the case where something other than a person drives the tool, a scheduled
job or an agent, where the caller knows up front which tree the run may touch
and a path outside it is a defect rather than a preference. An environment
variable rather than a flag, because a confinement you can lift on the same
command line you are confining does not confine anything.

RESOLVED, NOT JUST NORMALISED. `os.path.realpath` follows symlinks, so a link
pointing out of the root is judged on where it lands rather than on how it is
spelled. Tidying the text alone would let `root/link` through and write
outside the root anyway.
"""

from __future__ import annotations

import os
from typing import Optional

from .errors import GenerationChainError

FILE_ROOT_ENV_VAR = "GENCHAIN_FILE_ROOT"


class PathRefused(GenerationChainError):
    """A path this tool was handed is not one it may open, and why."""


def checked_path(path: str, purpose: str) -> str:
    """The absolute, symlink-resolved path to open, or a refusal.

    `purpose` names the flag or the file the path came from, because a
    message that only says a path was rejected costs an hour, and one that
    says which of four paths it was costs a minute.
    """
    if not path or not path.strip():
        raise PathRefused(
            f"{purpose} was given an empty path. Nothing was opened")
    if "\0" in path:
        raise PathRefused(
            f"{purpose} was given a path holding a NUL byte: {path!r}. "
            "Nothing was opened")
    resolved = os.path.realpath(os.path.expanduser(path))
    root = confined_root()
    if root is not None and not is_inside(resolved, root):
        raise PathRefused(
            f"{purpose} resolves to {resolved}, which is outside {root}. "
            f"{FILE_ROOT_ENV_VAR} confines this run to that directory, so "
            "nothing was opened")
    return resolved


def confined_root() -> Optional[str]:
    """The directory this run is confined to, or None when nobody said."""
    named = os.environ.get(FILE_ROOT_ENV_VAR, "").strip()
    if not named:
        return None
    return os.path.realpath(os.path.expanduser(named))


def is_inside(resolved: str, root: str) -> bool:
    """Whether `resolved` is `root` itself or something underneath it.

    Compared component by component rather than as a string prefix, because
    /var/tmp-evil starts with /var/tmp and is not inside it.
    """
    return (resolved == root
            or resolved.startswith(root.rstrip(os.sep) + os.sep))
