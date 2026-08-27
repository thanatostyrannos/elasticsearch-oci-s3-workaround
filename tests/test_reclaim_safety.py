"""The audit path still cannot delete, with this package sitting beside it.

Adding a delete path to this project is exactly the change that could weaken
the read-only guarantee the audit tool depends on, by accident, through a
shared import or a loosened assertion. These are the static tripwires: they
read source rather than run behaviour, so a change that reintroduces a delete
method into the read path, or that lets `generation_chain.reclaim` leak into
`derivation`, `sources`, `reporting` or the audit `cli`, fails here before it
fails anywhere an operator would notice.
"""

import ast
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.sources.http_reads import ALLOWED_METHODS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "generation_chain")

# Every module the audit derivation runs through. `reclaim` is deliberately
# absent: it is the one package allowed to import the read transports (for
# credentials and signing) and it must never be imported back.
AUDIT_PATH_DIRECTORIES = ("derivation", "formats", "reporting", "sources")
AUDIT_PATH_FILES = ("cli.py", "model.py", "errors.py", "supported.py",
                    "corroboration.py", "selftest.py", "credentials.py")


def _python_files():
    for directory in AUDIT_PATH_DIRECTORIES:
        for base, _dirs, names in os.walk(os.path.join(PACKAGE, directory)):
            for name in names:
                if name.endswith(".py"):
                    yield os.path.join(base, name)
    for name in AUDIT_PATH_FILES:
        yield os.path.join(PACKAGE, name)


def _package_for(path: str) -> str:
    """The dotted `__package__` a module at `path` would report at runtime.

    `derivation/audit.py` needs two leading dots to reach a sibling of
    `derivation/` (`from ..reclaim import cli`), while a module at the
    package root needs only one (`from .reclaim import cli`). Both are
    dropping the file's own last path segment; the depth is what changes,
    and only resolving against it, rather than assuming every file in this
    scan sits at the package root, gets a relative import's target right.
    """
    relative = os.path.relpath(path, os.path.dirname(PACKAGE))
    return ".".join(relative.split(os.sep)[:-1])


def _imports(path: str):
    """Every module this file imports, as an absolute dotted name.

    A relative import (`from ..reclaim import cli`) is resolved against the
    importing file's own package before being yielded, so a caller checking
    for `generation_chain.reclaim` sees it regardless of how many dots the
    file itself used or how deep under the package it lives.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    package = _package_for(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                yield importlib.util.resolve_name(
                    "." * node.level + node.module, package)
            else:
                yield node.module


class AuditPathCannotDelete(unittest.TestCase):

    def test_allowed_methods_is_still_get_and_head_only(self):
        # The structural guard this whole project is built around. Neutered
        # in tests/genchain_neuter.py's existing case set would be redundant
        # with the assertion inside http_reads.py itself; this test is the
        # one that notices if the frozenset's VALUE is ever widened, which an
        # assertion phrased against the same frozenset cannot catch.
        self.assertEqual(ALLOWED_METHODS, frozenset({"GET", "HEAD"}))

    def test_nothing_in_the_audit_path_imports_the_reclaim_package(self):
        # Abuse case: if a future change had the derivation call into
        # `reclaim` for "efficiency", the audit tool would gain a delete path
        # through the back door, and this is the assertion that notices.
        # Neutered under "the-audit-path-never-imports-reclaim".
        offenders = []
        for path in _python_files():
            for name in _imports(path):
                if name == "generation_chain.reclaim" or \
                        name.startswith("generation_chain.reclaim."):
                    offenders.append(path)
        self.assertEqual(offenders, [])

    def test_the_reclaim_package_never_imports_http_reads(self):
        # This package sends the one request type http_reads.py exists to
        # forbid, so it must build that request independently rather than
        # reach into the read transport's own machinery, even to borrow
        # something that looks convenient.
        offenders = []
        reclaim_dir = os.path.join(PACKAGE, "reclaim")
        for base, _dirs, names in os.walk(reclaim_dir):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                for imported in _imports(path):
                    if "http_reads" in imported:
                        offenders.append(path)
        self.assertEqual(offenders, [])

    def test_no_get_or_head_only_source_sends_a_write_method(self):
        # Belt and suspenders on top of the ALLOWED_METHODS pin: greps the
        # transports themselves for a literal HTTP method string this project
        # has decided the read path may never carry.
        forbidden = ("\"POST\"", "'POST'", "\"DELETE\"", "'DELETE'",
                    "\"PUT\"", "'PUT'")
        offenders = []
        for path in _python_files():
            if os.path.join("generation_chain", "reclaim") in path:
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if any(token in text for token in forbidden):
                offenders.append(path)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
