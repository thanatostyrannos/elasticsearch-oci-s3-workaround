"""A mirrored bucket on local disk.

This is the project's rehearsal path: copy the bucket down, work against the
copy, and nothing an operator does can reach the real store. It is also the
transport the derivation is developed against, because it needs no
credentials and no network.
"""

from __future__ import annotations

import os
from typing import Dict, List

from ..errors import SourceReadError


class LocalMirrorSource:
    """Reads a directory that holds a copy of one repository's objects."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    def describe(self) -> str:
        return f"local mirror at {self.root}"

    def _path(self, key: str) -> str:
        """Resolve a repository-relative key inside the mirror root.

        A key is data read out of a listing, so it gets treated as data. A
        key holding ".." would otherwise read a file outside the mirror, and
        the derivation would then attribute someone else's document to this
        repository.
        """
        candidate = os.path.abspath(os.path.join(self.root, key))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise SourceReadError(f"key escapes the mirror root: {key!r}")
        return candidate

    def list_keys(self) -> List[str]:
        if not os.path.isdir(self.root):
            raise SourceReadError(f"no such directory: {self.root}")
        keys: List[str] = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(dirpath, name)
                keys.append(os.path.relpath(full, self.root).replace(os.sep, "/"))
        return sorted(keys)

    def sizes(self) -> Dict[str, int]:
        """Stored bytes per key, from the filesystem.

        Optional across transports: a source that cannot answer cheaply omits
        this and the report says so, rather than paying a request per object to
        find out.
        """
        found: Dict[str, int] = {}
        for key in self.list_keys():
            try:
                found[key] = os.path.getsize(self._path(key))
            except OSError:
                continue
        return found

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def fetch(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise SourceReadError(f"cannot read {key}: {exc}") from exc
