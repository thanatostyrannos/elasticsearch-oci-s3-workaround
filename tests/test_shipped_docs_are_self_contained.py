"""A shipped document may not send its reader to something the release lacks.

Someone who downloads `dist/elasticsearch-oci-s3-workaround-*.zip`, or
hand-copies it into a lab with no internet, has to be able to follow every
reference inside it. `package.py`'s own docstring records the first time this
went wrong: the load generator was left out of the release once, which shipped
a document instructing someone to run a file the archive did not contain. It
was fixed by adding the file to `PACKAGED_FILES`, not by softening the
instruction.

That class of defect came back twice more, worse. `README.md` documented
`snapshot_sizes.py` in detail, flags and all, though `package.py` deliberately
leaves it unshipped as "a reporting side tool, not on the reclaim path". And
`docs/blast-radius.md`, `README.md` and a dozen other shipped files pointed
readers at `evidence/`, `tests/`, `skills/`, `manifests/` and `terraform/`,
none of which ship, and several described the flags and internals of three
sweepers (`s3_repo_sweeper.py`, `oci_repo_sweeper.py`,
`es_log_driven_sweeper.py`) that are not merely unshipped: they were deleted
from the repository entirely, so no reader can obtain or inspect them by any
route.

Both defects share one shape and one fix. The shape: a document was written
against the whole working tree, which is what the author had open, rather
than against the archive, which is what the reader has. The fix has to be
enforced rather than remembered, the same conclusion `test_read_only_claim.py`
reached about a different recurring claim, because a reference that looks
fine in the source tree and is broken only in the tarball is exactly the kind
of defect nobody catches by reading.

So this file rebuilds the shipped set the same way `package.py` does --
by calling `package.members()`, never a hardcoded list, so it tracks the
packager rather than drifting from it -- and checks every shipped text file
for two things: a path-shaped reference to something outside that set, and
any mention at all of one of the three retired sweeper filenames, which is
unconditional because none of the three could possibly resolve to a real path
any more. `evidence/`, `tests/`, `skills/`, `terraform/` and `manifests/` are
free to reference each other and the shipped docs; the rule below is
one-directional; it only ever fires on a file that ships.
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

import package  # noqa: E402

# The three tools this project deleted along with their tests and runbooks.
# Unconditional: unlike a stale path, which might someday be recreated, these
# names can never resolve to something in this repository again without a
# deliberate, documented decision to bring one back.
RETIRED_SWEEPERS = (
    "s3_repo_sweeper.py",
    "oci_repo_sweeper.py",
    "es_log_driven_sweeper.py",
)

# Raw scanner output. It quotes whatever the tool matched, which routinely
# includes real paths from the unshipped tree (tests/, terraform/) as part of
# the finding it is reporting, not as an instruction to a reader. Treating
# that as a violation would make every security rescan fail this check by
# design, which is the "cries wolf" failure mode that gets a check ignored.
NON_PROSE_SUFFIXES = (".json",)

# A relative markdown link target: [text](target) or [text](<target>).
MD_LINK = re.compile(r"\[[^\]\n]*\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")

# A token shaped like a path inside this repository: a lowercase first
# segment, at least one slash, and either a trailing slash (a directory) or a
# recognised source-file extension. Conservative by construction -- an
# object-store key or an Elasticsearch API route (`_snapshot/x`, `a/b`) does
# not have a lowercase-letter-led first segment gated against a real
# top-level directory below, so ordinary prose does not trip it.
REPO_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_./:-])"
    r"([a-z][a-z0-9_-]*/(?:[A-Za-z0-9_.-]+/)*"
    r"(?:[A-Za-z0-9_.-]+\.(?:py|md|yaml|yml|json|txt|sh|toml|ini|cfg|bin|latest|tar\.gz))?)"
    r"(?![A-Za-z0-9_.-])"
)

# A bare top-level filename with no directory component at all --
# `snapshot_sizes.py`, `package.py`, `CONTRIBUTING.md` -- which
# REPO_PATH_TOKEN cannot see because it requires a slash. Gated below against
# files that actually exist at the repository root, so this never fires on an
# arbitrary "word.py" that is not a real file this project ships or withholds.
ROOT_FILENAME_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z][A-Za-z0-9_.-]*\.(?:py|md))(?![A-Za-z0-9_.-])"
)

URL_RE = re.compile(r"\S*://\S*")

# A self-link written as a full GitHub URL, which still names a path inside
# this repository and has to be checked like any other reference. Without
# this, a shipped doc could point at github.com/.../blob/main/evidence/x.md
# and read as "external" to every rule below.
SELF_REPO_URL_RE = re.compile(
    r"^https?://github\.com/[^/]+/elasticsearch-oci-s3-workaround/"
    r"(?:blob|tree|raw)/[^/]+/(?P<path>.*)$"
)


def shipped_members() -> frozenset[str]:
    """The archive's own member list, exactly as `package.py` computes it."""
    return frozenset(package.members())


def shipped_dirs(members: frozenset[str]) -> frozenset[str]:
    """Every directory prefix a shipped member lives under, trailing-slashed.

    A link to `generation_chain/` (a directory, no file named) is legitimate
    when the directory holds shipped members, even though the directory
    itself is not a member.
    """
    dirs = set()
    for member in members:
        parts = member.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]) + "/")
    return frozenset(dirs)


def top_level_entries(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Real top-level directory names and top-level file names, for gating.

    Rule 1 below (path-shaped tokens) only judges a token whose first
    segment names a real top-level directory, so an object-store key or an
    `mc alias/bucket` shape is left alone. Rule 2 (bare root filenames) only
    judges a name that is a real top-level file, so ordinary prose never
    matches by accident.
    """
    skip = {".git", "__pycache__", ".venv", "node_modules"}
    dirs, files = set(), set()
    for entry in root.iterdir():
        if entry.name in skip:
            continue
        if entry.is_dir():
            dirs.add(entry.name)
        elif entry.is_file():
            files.add(entry.name)
    return frozenset(dirs), frozenset(files)


def _is_external(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)) or target.startswith("//")


def _localize(target: str) -> str:
    match = SELF_REPO_URL_RE.match(target)
    return "/" + match.group("path") if match else target


def _norm(path: str) -> str:
    normalized = posixpath.normpath(path)
    return "" if normalized == "." else normalized


def _candidates(doc_member: str, target: str) -> list[str]:
    """Where `target`, written inside `doc_member`, could resolve to.

    Both a doc-relative and a root-relative reading are tried, because this
    project's docs mix both conventions (`docs/blast-radius.md` writes
    `../FACTS.md`; a skill writes `./snapshot_sizes.py` meaning "from the
    repo root"). A leading slash means root-relative only.
    """
    if target.startswith("/"):
        return [_norm(target.lstrip("/"))]
    doc_dir = posixpath.dirname(doc_member)
    doc_relative = _norm(posixpath.join(doc_dir, target)) if doc_dir else _norm(target)
    root_relative = _norm(target)
    return [doc_relative] if doc_relative == root_relative else [doc_relative, root_relative]


def _resolves(doc_member: str, target: str, members: frozenset[str],
             dirs: frozenset[str]) -> bool:
    target = target.split("#", 1)[0].split("?", 1)[0]
    target = target.replace("%20", " ").rstrip("/")
    if not target:
        return True
    for candidate in _candidates(doc_member, target):
        if candidate in members or (candidate + "/") in dirs:
            return True
    return False


def check_shipped_paths(root: Path, members: frozenset[str]) -> list[str]:
    """Rule 1: every repository path a shipped file names must also ship."""
    findings = []
    dirs = shipped_dirs(members)
    top_dirs, top_files = top_level_entries(root)
    for member in sorted(members):
        if member.endswith(NON_PROSE_SUFFIXES):
            continue
        try:
            text = (root / member).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = URL_RE.sub(" ", line)

            for match in MD_LINK.finditer(line):
                target = match.group(1)
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                target = _localize(target)
                if _is_external(target) or target.startswith("#"):
                    continue
                if not _resolves(member, target, members, dirs):
                    findings.append(
                        f"{member}:{lineno}: links to {target!r}, which is "
                        f"not in the shipped archive")

            for match in REPO_PATH_TOKEN.finditer(stripped):
                token = match.group(1)
                if token.split("/", 1)[0] not in top_dirs:
                    continue
                if not _resolves(member, token, members, dirs):
                    findings.append(
                        f"{member}:{lineno}: names {token!r}, which is not "
                        f"in the shipped archive")

            for match in ROOT_FILENAME_TOKEN.finditer(stripped):
                name = match.group(1)
                if name not in top_files or name in members:
                    continue
                findings.append(
                    f"{member}:{lineno}: names {name!r}, a repository file "
                    f"that does not ship")
    return findings


def check_no_retired_sweeper_names(root: Path, members: frozenset[str]) -> list[str]:
    """Rule 2: no shipped file may mention a retired sweeper, ever.

    Unconditional rather than existence-gated, because the whole point is
    that these three names can never resolve to a real path in this
    repository again. A document describing one's flags or behaviour is
    describing software the reader cannot obtain or inspect, independent of
    whether the surrounding sentence looks like an instruction.
    """
    findings = []
    for member in sorted(members):
        if member.endswith(NON_PROSE_SUFFIXES):
            continue
        try:
            text = (root / member).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in RETIRED_SWEEPERS:
                if name in line:
                    findings.append(
                        f"{member}:{lineno}: names retired sweeper {name!r}: "
                        f"{line.strip()[:90]}")
    return findings


# --------------------------------------------------------------------------
# A miniature shipped tree, for the abuse tests
# --------------------------------------------------------------------------


class _MiniRelease:
    """A throwaway directory standing in for a repository root."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def write(self, relpath: str, text: str) -> None:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def mkdir(self, relpath: str) -> None:
        (self.root / relpath).mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self._tmp.cleanup()


class MiniReleaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.release = _MiniRelease()
        self.addCleanup(self.release.close)

    def assertFinding(self, findings: list[str], needle: str) -> None:
        self.assertTrue(
            any(needle in f for f in findings),
            f"expected a finding mentioning {needle!r}, got: {findings}",
        )


# --------------------------------------------------------------------------
# Rule 1: shipped files only reference the shipped set
# --------------------------------------------------------------------------


class TestShippedFilesStayInsideTheShippedSet(MiniReleaseTestCase):
    def test_use_the_real_release_names_nothing_outside_itself(self):
        # The regression guard. This is what the fixes in FACTS.md,
        # README.md, docs/blast-radius.md and the rest of the shipped tree
        # were for: every reference a real shipped file makes has to resolve
        # inside the set `package.members()` actually produces.
        findings = check_shipped_paths(ROOT, shipped_members())
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_a_link_to_an_unshipped_directory_is_a_finding(self):
        # The exact shape of the original defect: a link into `evidence/`,
        # which package.py deliberately never packages.
        members = frozenset({"README.md"})
        self.release.write("README.md", "See [the raw data](evidence/campaign-data.md).\n")
        findings = check_shipped_paths(self.release.root, members)
        self.assertFinding(findings, "evidence/campaign-data.md")

    def test_abuse_running_an_unshipped_tool_is_a_finding(self):
        # The snapshot_sizes.py defect: a shipped doc telling the reader to
        # run a file that exists in the repository but that the packager
        # deliberately leaves out of the archive.
        members = frozenset({"README.md"})
        self.release.write("snapshot_sizes.py", "# a real, unshipped file\n")
        self.release.write(
            "README.md",
            "Size what is there with `snapshot_sizes.py --emit-classified`.\n",
        )
        findings = check_shipped_paths(self.release.root, members)
        self.assertFinding(findings, "snapshot_sizes.py")

    def test_use_a_link_to_a_shipped_file_is_not_a_finding(self):
        members = frozenset({"README.md", "FACTS.md"})
        self.release.write("README.md", "See [the facts](FACTS.md#some-heading).\n")
        self.release.write("FACTS.md", "# Facts\n")
        findings = check_shipped_paths(self.release.root, members)
        self.assertEqual(findings, [])

    def test_use_a_link_into_a_shipped_directory_is_not_a_finding(self):
        # `generation_chain/` never appears in `members()` itself, only the
        # files under it, so the directory link has to resolve through
        # `shipped_dirs`, not through direct membership.
        members = frozenset({"generation_chain/cli.py"})
        self.release.write("README.md", "See [the audit](generation_chain/).\n")
        findings = check_shipped_paths(self.release.root, members)
        self.assertEqual(findings, [])

    def test_use_a_link_to_a_partially_shipped_directory_is_still_checked(self):
        # generation_chain/ ships only its .py files. A link to a .md file
        # that happens to live in that same directory on disk must still be
        # flagged: the directory shipping is not a blanket exemption.
        members = frozenset({"generation_chain/cli.py", "README.md"})
        self.release.write("generation_chain/README.md", "# Not shipped\n")
        self.release.write(
            "README.md", "See [its README](generation_chain/README.md).\n"
        )
        findings = check_shipped_paths(self.release.root, members)
        self.assertFinding(findings, "generation_chain/README.md")

    def test_abuse_regex_does_not_fire_on_ordinary_prose_or_urls(self):
        # "and/or", an object-store key, an Elasticsearch API route, and a
        # full external URL naming a path this repository does not have. If
        # any of these tripped the checker, every page in this project would
        # fail, and the check would get switched off inside a week.
        members = frozenset({"README.md"})
        self.release.write(
            "README.md",
            "Use and/or judgement. Key `indices/GVsRr/0/index-3`. Call "
            "`DELETE /_snapshot/x`. See https://example.com/evidence/x.md "
            "and https://github.com/elastic/elasticsearch/blob/main/x.java.\n",
        )
        findings = check_shipped_paths(self.release.root, members)
        self.assertEqual(findings, [])

    def test_abuse_the_archives_own_filename_is_not_flagged(self):
        # Mentioning the release archive by name is not a repository-path
        # reference, and `dist/` itself never ships, so a doc naming its own
        # output file must not be treated as pointing at unshipped content
        # the way a link into evidence/ or tests/ would be.
        members = frozenset({"README.md"})
        self.release.mkdir("dist")
        self.release.write("package.py", "# a real, unshipped file\n")
        self.release.write(
            "README.md",
            "Build with `python3 package.py`; it writes "
            "`dist/elasticsearch-oci-s3-workaround-1.1.0.zip`.\n",
        )
        findings = check_shipped_paths(self.release.root, members)
        # package.py itself is a real, unshipped root file, so that half is
        # correctly flagged; the archive filename must not add a second,
        # spurious finding of its own.
        self.assertEqual(len(findings), 1, findings)
        self.assertFinding(findings, "package.py")


# --------------------------------------------------------------------------
# Rule 2: no shipped file names a retired sweeper
# --------------------------------------------------------------------------


class TestNoShippedFileNamesARetiredSweeper(MiniReleaseTestCase):
    def test_use_the_real_release_names_no_retired_sweeper(self):
        findings = check_no_retired_sweeper_names(ROOT, shipped_members())
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_a_docstring_mentioning_one_is_a_finding(self):
        # Unconditional: even a purely historical, provenance-style mention
        # -- "written from the format rather than imported from X" -- names
        # a file the reader cannot obtain, so it is still a finding.
        members = frozenset({"generation_chain/formats/smile.py"})
        self.release.write(
            "generation_chain/formats/smile.py",
            '"""Written from the spec rather than adapted from '
            "`s3_repo_sweeper.py`.\n\"\"\"\n",
        )
        findings = check_no_retired_sweeper_names(self.release.root, members)
        self.assertFinding(findings, "s3_repo_sweeper.py")

    def test_abuse_each_of_the_three_names_is_caught(self):
        members = frozenset({"docs/blast-radius.md"})
        self.release.write(
            "docs/blast-radius.md",
            "`s3_repo_sweeper.py`, `oci_repo_sweeper.py` and "
            "`es_log_driven_sweeper.py` were removed.\n",
        )
        findings = check_no_retired_sweeper_names(self.release.root, members)
        for name in RETIRED_SWEEPERS:
            self.assertFinding(findings, name)

    def test_use_the_word_sweeper_alone_is_not_a_finding(self):
        # The rule is aimed at the three literal filenames, not at the
        # general word. blast-radius.md and README.md both still discuss
        # "the retired sweepers" and "a reachability sweep" in the abstract,
        # which is legitimate history and design rationale, not a pointer
        # to an unobtainable file.
        members = frozenset({"README.md"})
        self.release.write(
            "README.md",
            "Two published runbooks used to do that, one over each API, "
            "and both drove a sweeper retired for deciding what to delete "
            "by absence from a set it computed itself.\n",
        )
        findings = check_no_retired_sweeper_names(self.release.root, members)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
