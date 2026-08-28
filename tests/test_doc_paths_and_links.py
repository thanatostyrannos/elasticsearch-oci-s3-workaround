#!/usr/bin/env python3
"""Documentation drift tests: every path and anchor a doc names must be real.

Two defects in this repository are the reason the file exists.

Three write-ups moved under `evidence/` partway through a session. The move
left nine inbound links pointing at the old root paths, spread across six
files. Nobody caught them by reading. Rule 1 turns that whole class of
rename into a red test.

A results doc sent readers to a `local-rig/` directory this repository has
never had, once in prose and once in a `kubectl apply -f local-rig/` command
sitting in a fenced block for people to copy. It could never have worked for
anyone. Rule 4b pins that name.

Neither defect shows up in the Python unit tests, and neither one is visible
to a reader skimming a 900-line markdown file. Both are cheap for a machine
to settle.

Five rules, over every tracked `.md` in the repo (root, `docs/`, `evidence/`,
`skills/`, `skills/*/`, `manifests/`):

  1. every relative markdown link target exists on disk;
  2. every in-page anchor `](#...)` matches a real heading under GitHub's
     slug rules;
  3. every cross-document anchor `](OTHER.md#anchor)` matches a heading in
     that file;
  4. every bare repo-path in prose (`tests/`, `manifests/`,
     `skills/es-snapshot-audit/SKILL.md`, `local-rig/`) exists; and
  5. every fenced code block that invokes a repo file (`python3
     snapshot_sizes.py`, `kubectl apply -f manifests/minio.yaml`) names a
     file that exists.

The rules are tool-agnostic and were written that way on purpose. They
survived the retirement of the three sweepers by having their examples
repointed at what is still here, which is the whole argument for a checker
that reads the repository instead of carrying a list of what it expects to
find. The one thing the retirement did add is `RETIRED_ARTIFACTS`, below.

Every rule is tested twice. The USE test runs the checker over the real
corpus and demands zero findings, which is the regression guard. The ABUSE
test feeds the same checker a deliberately broken miniature repo and demands
a finding, which is what proves the checker can fail at all. A checker
nobody has ever watched fail is decoration.

External `http(s)://` links are never fetched. A suite that needs the network
goes red on a bad connection, and a suite that goes red for reasons nobody
controls gets skipped. This one only checks claims the repository can settle
on its own.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import functools
import pathlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories the docs must never claim exist, because they do not. Kept as a
# named list so the regression is legible: `local-rig/` is the exact defect a
# reviewer found by hand (the manifests live in `manifests/`).
KNOWN_BAD_REPO_DIRS = ("local-rig",)

# Files this repository removed on purpose when the three reachability
# sweepers were retired. Their names are still all over `evidence/`, inside
# captured terminal output from runs that really happened. Editing a
# transcript to take a command out of it would falsify the record, so rules
# 1, 4a and 5 let these names stand there and nowhere else.
#
# Outside `evidence/` the exemption does not apply, and that is the half that
# does work: a runbook that tells an operator to run `s3_repo_sweeper.py`
# fails rule 5, which is exactly what should happen to a procedure for a tool
# that is not in the repository. TestRepoInventory pins the list against the
# working tree, so if one of these names is ever a real file again the list
# has to be revisited rather than quietly protecting a file that exists.
RETIRED_ARTIFACTS = frozenset({
    "s3_repo_sweeper.py",
    "oci_repo_sweeper.py",
    "es_log_driven_sweeper.py",
    "tests/test_s3_repo_sweeper.py",
    "tests/test_oci_repo_sweeper.py",
    "tests/test_es_log_driven_sweeper.py",
    "tests/test_data_loss_guards.py",
    "tests/test_oci_stdlib_client.py",
    "tests/test_snapshot_sizes.py",
})

# tests/s3rig.py was on the list above and came off it when the
# generation-chain package restored the file. It is a live test helper again,
# so the three link rules apply to it normally: a document that points at it
# is pointing at something real, and a command that runs it is a command that
# works.

# Top-level directories whose documents record what happened rather than
# telling anybody what to do. A command in one of these is history.
HISTORICAL_DIRS = frozenset({"evidence"})


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

MD_LINK = re.compile(r"\[[^\]\n]*\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```+|~~~+)")

# A token that looks like a path inside this repository: a lowercase first
# segment, at least one slash, and either a trailing slash (a directory) or a
# recognised source-file extension. Conservative by construction, so it does
# not fire on prose such as "and/or" or on `_snapshot/oci-repro`.
REPO_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_./:-])"
    r"([a-z][a-z0-9_-]*/(?:[A-Za-z0-9_.-]+/)*"
    r"(?:[A-Za-z0-9_.-]+\.(?:py|md|yaml|yml|json|txt|sh|toml|ini|cfg|bin|latest))?)"
    r"(?![A-Za-z0-9_.-])"
)

URL_RE = re.compile(r"\S*://\S*")

# Invocations inside fenced code blocks that name a file in this repo.
SCRIPT_INVOCATION = re.compile(
    r"(?:python3?\s+(?!-)|(?<![A-Za-z0-9_/.-])\./)([A-Za-z0-9_./-]+\.py)\b"
)
KUBECTL_APPLY = re.compile(r"apply\s+(?:-f|--filename)[=\s]\s*([A-Za-z0-9_./-]+)")


def github_slug(heading: str) -> str:
    """Slugify a heading the way GitHub does for its heading anchors.

    Lowercase; markdown link syntax reduced to its text; backticks and `*`/`~`
    emphasis markers dropped; every remaining character that is not a word
    character, whitespace, or hyphen removed (underscores survive, because
    they are word characters); whitespace collapsed to hyphens.
    """
    text = heading.strip().lower()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = text.replace("`", "")
    text = re.sub(r"[*~]", "", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s", "-", text)
    return text


def markdown_files(root: Path) -> list[Path]:
    """Every tracked `.md` file under `root`, sorted, ignoring VCS and caches.

    Tracked is the operative word, and it is a deliberate call about what
    belongs in the corpus. These rules describe what ships. `HANDOFF.md` is
    the live example of what does not: session notes, listed in `.gitignore`,
    full of half-finished paths. Holding it to the same link discipline as a
    published doc would produce findings nobody should act on, and findings
    nobody acts on are how a suite stops being read. A doc that exists in the
    working tree but has not been committed still gets checked, because it is
    on its way to shipping. Ask git when git can answer; fall back to a
    filesystem walk when it cannot, so the suite still runs from an exported
    tarball.
    """
    skip = {".git", "__pycache__", ".venv", "node_modules"}
    walked = sorted(
        p
        for p in root.rglob("*.md")
        if not skip.intersection(p.relative_to(root).parts)
    )
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return walked
    tracked = {
        (root / name).resolve()
        for name in out.decode("utf-8", "replace").split("\0")
        if name
    }
    if not tracked:  # not a checkout, or no .md committed yet
        return walked
    # Keep anything git knows about. Keep new files too, so a doc added in
    # this working tree is checked before it is ever committed; only drop
    # what git is deliberately ignoring.
    ignored = set()
    unknown = [p for p in walked if p.resolve() not in tracked]
    if unknown:
        try:
            res = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
                input=b"\0".join(str(p).encode() for p in unknown),
                capture_output=True,
                timeout=30,
            )
            ignored = {
                Path(name).resolve()
                for name in res.stdout.decode("utf-8", "replace").split("\0")
                if name
            }
        except (OSError, subprocess.SubprocessError):
            ignored = set()
    return [p for p in walked if p.resolve() not in ignored]


def _strip_frontmatter(lines: list[str]) -> list[str]:
    """Blank out a leading `---` YAML frontmatter block (skills use one)."""
    if not lines or lines[0].strip() != "---":
        return lines
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return [""] * (i + 1) + lines[i + 1 :]
    return lines


def split_fenced(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Split into (prose lines, fenced-code lines), each as (lineno, line)."""
    lines = _strip_frontmatter(text.splitlines())
    prose: list[tuple[int, str]] = []
    fenced: list[tuple[int, str]] = []
    marker: str | None = None
    for i, line in enumerate(lines, 1):
        m = FENCE.match(line)
        if m:
            if marker is None:
                marker = m.group(1)[:3]
            elif line.strip().startswith(marker):
                marker = None
            continue
        (fenced if marker is not None else prose).append((i, line))
    return prose, fenced


def headings(text: str) -> list[str]:
    """ATX headings outside fenced code, in document order."""
    prose, _ = split_fenced(text)
    out = []
    for _, line in prose:
        m = ATX_HEADING.match(line)
        if m:
            out.append(m.group(2))
    return out


def anchor_slugs(text: str) -> set[str]:
    """Every anchor GitHub would mint for this document, duplicates included.

    A repeated heading gets `-1`, `-2`, ... appended, matching GitHub.
    """
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    for heading in headings(text):
        base = github_slug(heading)
        if not base:
            continue
        n = seen.get(base, 0)
        seen[base] = n + 1
        slugs.add(base if n == 0 else f"{base}-{n}")
    return slugs


def links(text: str) -> list[tuple[int, str]]:
    """Markdown link targets outside fenced code, as (lineno, target)."""
    prose, _ = split_fenced(text)
    out = []
    for lineno, line in prose:
        for m in MD_LINK.finditer(line):
            target = m.group(1)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            out.append((lineno, target))
    return out


def _is_historical(root: Path, doc: Path) -> bool:
    """True if `doc` is a record of a past run rather than an instruction."""
    parts = doc.relative_to(root).parts
    return bool(parts) and parts[0] in HISTORICAL_DIRS


def _is_retired(ref: str) -> bool:
    """True if `ref` names a file the retirement removed.

    Matched on the full repo-relative spelling and on the bare filename, so
    `../s3_repo_sweeper.py` in a link, `tests/test_s3_repo_sweeper.py` in
    prose and `python3 s3_repo_sweeper.py` in a command all resolve to the
    same judgement.
    """
    ref = ref.split("#", 1)[0].split("?", 1)[0].strip().lstrip("./")
    return ref in RETIRED_ARTIFACTS or ref.rsplit("/", 1)[-1] in RETIRED_ARTIFACTS


# Docs link with absolute GitHub URLs so they resolve for a reader who is not
# browsing a clone. Such a link still points into this corpus, so it has to be
# checked like any other. Without this the rules went quiet the moment the docs
# adopted full URLs: every self-link matched _is_external and was skipped, and
# the five USE guards passed while reading nothing.
SELF_REPO_URL_RE = re.compile(
    r"^https?://github\.com/[^/]+/elasticsearch-oci-s3-workaround/"
    r"(?:blob|tree|raw)/[^/]+/(?P<path>.*)$"
)


def _localize(target: str) -> str:
    """A GitHub URL naming a file in this repository, as a root-relative path.

    The result carries a leading slash, because a URL names its path from the
    repository root and never from the linking document. Without that marker
    `evidence/README.md` linking to the top-level `README.md` resolves to
    itself, and the anchor gets looked up in the wrong file.

    Anything else is returned unchanged, so a genuinely external URL stays
    external and is still skipped.
    """
    match = SELF_REPO_URL_RE.match(target)
    return "/" + match.group("path") if match else target


def _is_external(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)) or target.startswith("//")


@functools.lru_cache(maxsize=1)
def _tracked_paths(root_str: str) -> frozenset:
    """Everything git carries, plus every directory on the way to it.

    `Path.exists()` alone answers a different question from the one that
    matters. A path can exist on the machine that wrote the document and be
    absent from a clean checkout, and git does not track an empty directory at
    all. `docs/run-proofs/` was exactly that: every local run passed and CI
    failed on the first fresh clone.

    Falls back to an empty set outside a git work tree, where the on-disk
    check is all there is.
    """
    root = pathlib.Path(root_str)
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True,
            text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if listing.returncode != 0:
        return frozenset()
    known = set()
    for name in listing.stdout.split("\0"):
        if not name:
            continue
        known.add(name)
        parent = pathlib.PurePosixPath(name).parent
        while str(parent) not in (".", "/"):
            known.add(str(parent))
            parent = parent.parent
    return frozenset(known)


def _resolve(root: Path, doc: Path, ref: str) -> bool:
    """True if `ref` names something real, relative to the doc or to the root.

    Both bases are accepted because the docs mix the two conventions: a skill
    file writes `./snapshot_sizes.py` meaning "run this from the repo root",
    while `manifests/README.md` writes `minio.yaml` meaning "next to me".
    """
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    ref = ref.replace("%20", " ").rstrip("/")
    if not ref:
        return True
    # A leading slash means the target named itself from the repository root,
    # so the document's own directory is not a candidate base.
    bases = (root,) if ref.startswith("/") else (doc.parent, root)
    ref = ref.lstrip("/")
    if not ref:
        return True
    for base in bases:
        try:
            candidate = (base / ref).resolve()
        except (OSError, ValueError):
            continue
        if candidate.exists():
            # Existing on this machine is not enough. A reference has to be
            # something a clean checkout will also have, or the document is
            # only correct where it was written.
            tracked = _tracked_paths(str(root))
            if tracked:
                try:
                    relative = candidate.relative_to(root.resolve())
                except ValueError:
                    return True          # outside the repo, not ours to judge
                if str(relative) not in tracked:
                    continue
            return True
    return False


# --------------------------------------------------------------------------
# The five checkers. Each takes a repo root and returns a list of findings, so
# the USE tests can run it over the real corpus and the ABUSE tests can run
# the very same code over a crafted broken repo.
# --------------------------------------------------------------------------


def check_relative_links(root: Path) -> list[str]:
    """Rule 1: every relative markdown link target exists on disk.

    A link in `evidence/` to a file the retirement removed is left alone. See
    RETIRED_ARTIFACTS for why, and for why the same link anywhere else is
    still a finding.
    """
    findings = []
    for doc in markdown_files(root):
        text = doc.read_text(encoding="utf-8")
        for lineno, target in links(text):
            target = _localize(target)
            if _is_external(target) or target.startswith("#"):
                continue
            if _is_historical(root, doc) and _is_retired(target):
                continue
            if not _resolve(root, doc, target):
                rel = doc.relative_to(root)
                findings.append(f"{rel}:{lineno}: broken link target {target!r}")
    return findings


def check_inpage_anchors(root: Path) -> list[str]:
    """Rule 2: every `](#...)` anchor matches a heading in the same file."""
    findings = []
    for doc in markdown_files(root):
        text = doc.read_text(encoding="utf-8")
        slugs = anchor_slugs(text)
        for lineno, target in links(text):
            if not target.startswith("#"):
                continue
            anchor = target[1:]
            if anchor and anchor not in slugs:
                rel = doc.relative_to(root)
                findings.append(
                    f"{rel}:{lineno}: anchor '#{anchor}' matches no heading here"
                )
    return findings


def check_cross_doc_anchors(root: Path) -> list[str]:
    """Rule 3: every `](OTHER.md#anchor)` matches a heading in OTHER.md."""
    findings = []
    cache: dict[Path, set[str]] = {}
    for doc in markdown_files(root):
        text = doc.read_text(encoding="utf-8")
        for lineno, target in links(text):
            target = _localize(target)
            if _is_external(target) or target.startswith("#") or "#" not in target:
                continue
            path_part, anchor = target.split("#", 1)
            if not path_part.endswith(".md") or not anchor:
                continue
            other = None
            bases = (root,) if path_part.startswith("/") else (doc.parent, root)
            path_part = path_part.lstrip("/")
            for base in bases:
                candidate = (base / path_part).resolve()
                if candidate.is_file():
                    other = candidate
                    break
            rel = doc.relative_to(root)
            if other is None:
                findings.append(
                    f"{rel}:{lineno}: cross-doc link to missing file {path_part!r}"
                )
                continue
            if other not in cache:
                cache[other] = anchor_slugs(other.read_text(encoding="utf-8"))
            if anchor not in cache[other]:
                findings.append(
                    f"{rel}:{lineno}: anchor '#{anchor}' matches no heading "
                    f"in {path_part}"
                )
    return findings


def _top_level_dirs(root: Path) -> set[str]:
    skip = {".git", "__pycache__", ".venv", "node_modules"}
    return {p.name for p in root.iterdir() if p.is_dir() and p.name not in skip}


def check_bare_repo_paths(root: Path) -> list[str]:
    """Rule 4a: bare paths naming a repo directory must resolve on disk.

    Conservative by construction, because these docs are full of shell
    transcripts whose paths are object-store keys and `mc` aliases
    (`indices/<uuid>/0/index-3`, `rig/es-snapshots`, `vf-test/...`), not
    repository paths. An allowlist of bucket prefixes would need editing
    forever, and would eventually grow wide enough to swallow the defect this
    rule exists to find. So a token is judged only when its first segment is
    an actual top-level directory of this repository.
    Everything under `skills/`, `manifests/`, `tests/` is therefore checked;
    a bucket key is left alone. So is a retired artifact named in `evidence/`,
    which is a record of a file that existed at the time rather than a stale
    path.
    """
    findings = []
    tops = _top_level_dirs(root)
    for doc in markdown_files(root):
        text = doc.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            line = URL_RE.sub(" ", line)
            for m in REPO_PATH_TOKEN.finditer(line):
                token = m.group(1)
                if token.split("/", 1)[0] not in tops:
                    continue
                if _is_historical(root, doc) and _is_retired(token):
                    continue
                if not _resolve(root, doc, token):
                    rel = doc.relative_to(root)
                    findings.append(
                        f"{rel}:{lineno}: references {token!r}, which is not "
                        f"in the repository"
                    )
    return findings


def check_absent_directories(root: Path) -> list[str]:
    """Rule 4b: no doc may send readers to a directory the repo does not have.

    An explicit denylist rather than a heuristic, so it cannot false-positive
    on a bucket prefix. `local-rig/` is the reviewer's finding: a document
    told readers the manifests lived there, when this repository keeps them
    in `manifests/`.
    """
    findings = []
    for name in KNOWN_BAD_REPO_DIRS:
        if (root / name).exists():
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9_./-]){re.escape(name)}/")
        for doc in markdown_files(root):
            for lineno, line in enumerate(
                doc.read_text(encoding="utf-8").splitlines(), 1
            ):
                if pattern.search(URL_RE.sub(" ", line)):
                    rel = doc.relative_to(root)
                    findings.append(
                        f"{rel}:{lineno}: points readers at {name}/, which is "
                        f"not a directory of this repository"
                    )
    return findings


def check_fenced_file_references(root: Path) -> list[str]:
    """Rule 5: files invoked inside fenced code blocks must exist.

    The one exemption is a retired tool invoked inside `evidence/`, where the
    command is a transcript of what was run and not an instruction. Outside
    `evidence/` there is no exemption, which is what makes this rule the
    thing standing between the repository and a runbook for a tool that no
    longer exists.
    """
    findings = []
    for doc in markdown_files(root):
        text = doc.read_text(encoding="utf-8")
        _, fenced = split_fenced(text)
        rel = doc.relative_to(root)
        for lineno, line in fenced:
            line = URL_RE.sub(" ", line)
            refs = [m.group(1) for m in SCRIPT_INVOCATION.finditer(line)]
            for m in KUBECTL_APPLY.finditer(line):
                target = m.group(1)
                if target != "-":  # `apply -f -` reads a manifest from stdin
                    refs.append(target)
            for ref in refs:
                if _is_historical(root, doc) and _is_retired(ref):
                    continue
                if not _resolve(root, doc, ref):
                    findings.append(
                        f"{rel}:{lineno}: code block invokes {ref!r}, "
                        f"which does not exist"
                    )
    return findings


def corpus_coverage(root: Path) -> dict[str, int]:
    """How much material each rule actually has to chew on.

    A checker that inspects nothing passes every time. If the link regex ever
    breaks, or discovery stops descending into subdirectories, all five USE
    guards would go green while checking air. This counts what the rules see
    so that silent vacuity is itself a test failure.
    """
    counts = dict.fromkeys(
        ("docs", "relative_links", "in_page_anchors", "cross_doc_anchors",
         "bare_paths", "fenced_refs"),
        0,
    )
    tops = _top_level_dirs(root)
    docs = markdown_files(root)
    counts["docs"] = len(docs)
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for _, target in links(text):
            target = _localize(target)
            if target.startswith("#"):
                counts["in_page_anchors"] += 1
            elif _is_external(target):
                continue
            else:
                counts["relative_links"] += 1
                if "#" in target and target.split("#", 1)[0].endswith(".md"):
                    counts["cross_doc_anchors"] += 1
        for line in text.splitlines():
            stripped = URL_RE.sub(" ", line)
            for m in REPO_PATH_TOKEN.finditer(stripped):
                if m.group(1).split("/", 1)[0] in tops:
                    counts["bare_paths"] += 1
        _, fenced = split_fenced(text)
        for _, line in fenced:
            stripped = URL_RE.sub(" ", line)
            counts["fenced_refs"] += len(SCRIPT_INVOCATION.findall(stripped))
            counts["fenced_refs"] += len(
                [t for t in KUBECTL_APPLY.findall(stripped) if t != "-"]
            )
    return counts


# --------------------------------------------------------------------------
# A miniature repo for the abuse tests
# --------------------------------------------------------------------------


class _MiniRepo:
    """A throwaway repo on disk, so the checkers can be fed broken input."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def write(self, relpath: str, text: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def mkdir(self, relpath: str) -> None:
        (self.root / relpath).mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self._tmp.cleanup()


class MiniRepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _MiniRepo()
        self.addCleanup(self.repo.close)

    def assertFinding(self, findings: list[str], needle: str) -> None:
        self.assertTrue(
            any(needle in f for f in findings),
            f"expected a finding mentioning {needle!r}, got: {findings}",
        )


# --------------------------------------------------------------------------
# The slug rules themselves
# --------------------------------------------------------------------------


class TestGitHubSlug(unittest.TestCase):
    """We reimplemented GitHub's heading-anchor rule, so it can drift from it.

    Rules 2 and 3 compare our slug against the anchor an author typed. Get the
    slug wrong and the checkers either wave through anchors GitHub will 404
    on, or flag correct anchors until somebody switches the rule off. Both
    ways, broken anchors ship.
    """

    def test_use_real_headings_from_the_corpus(self):
        # Headings in the shapes these docs actually use: a numbered step, a
        # backticked query parameter, a question, a dash inside the title.
        # Each one is a place our slug can disagree with GitHub's, and the
        # disagreement only shows up when a reader clicks and lands nowhere.
        cases = {
            "Root cause and upstream status": "root-cause-and-upstream-status",
            "The failure in detail": "the-failure-in-detail",
            "Step 0 — keep the repository operational (`?verify=false`)":
                "step-0--keep-the-repository-operational-verifyfalse",
            "1. Generate the key": "1-generate-the-key",
            "Is this your bug?": "is-this-your-bug",
        }
        for heading, expected in cases.items():
            with self.subTest(heading=heading):
                self.assertEqual(github_slug(heading), expected)

    def test_use_punctuation_backticks_and_underscores(self):
        # Backticks and emphasis markers vanish, underscores survive. That
        # split is easy to get backwards, and backwards breaks every anchor
        # into a heading that names a code identifier. Those headings are
        # most of the headings in these docs.
        self.assertEqual(
            github_slug("Do not lower `delete_objects_max_size` now"),
            "do-not-lower-delete_objects_max_size-now",
        )
        self.assertEqual(github_slug("**Bold** _and_ *stars*"), "bold-_and_-stars")

    def test_abuse_slug_is_not_a_passthrough(self):
        # If github_slug degraded to a plain lowercase, it would agree with
        # the raw heading text and rules 2 and 3 would rubber-stamp whatever
        # anchor an author typed. The suite goes green and the anchors still
        # 404. This is the shape of failure the anchor rules cannot survive.
        self.assertNotEqual(github_slug("A/B: testing!"), "a/b: testing!")
        self.assertEqual(github_slug("A/B: testing!"), "ab-testing")
        self.assertNotEqual(github_slug("Two  spaces"), "two-spaces")
        self.assertEqual(github_slug("Two  spaces"), "two--spaces")

    def test_use_duplicate_headings_get_numeric_suffixes(self):
        # No document repeats a heading today, so this is the numbering rule
        # waiting for the first one that does, and campaign write-ups that
        # each need a "Results" section are the obvious candidate. GitHub
        # numbers the repeats. If we do not, the correct `#results-1` anchor
        # gets reported as dangling and somebody edits a working link to
        # silence the suite.
        text = "# Notes\n\n## Notes\n\n## Notes\n"
        self.assertEqual(anchor_slugs(text), {"notes", "notes-1", "notes-2"})

    def test_abuse_headings_inside_code_fences_are_not_anchors(self):
        # These docs are largely shell transcripts, and a `# comment` line in
        # a bash block looks exactly like a heading. Count those and we mint
        # anchors nobody can link to, which is worse than missing one: a
        # genuinely dangling anchor passes because some code comment happened
        # to slug the same way.
        text = "# Real\n\n```bash\n# Fake heading\n```\n"
        self.assertEqual(anchor_slugs(text), {"real"})


# --------------------------------------------------------------------------
# Rule 0 (inventory): the repo has the shape the rules assume
# --------------------------------------------------------------------------


class TestRepoInventory(unittest.TestCase):
    """Two assumptions the five rules rest on, checked before the rules run."""

    def test_use_known_bad_directories_are_absent(self):
        # Rule 4b only fires while `local-rig/` is absent, by design, so the
        # day somebody creates one the rule goes quiet and the docs are free
        # to drift back to pointing there. This is the alarm for that: it
        # fails the moment the denylist stops meaning anything, and it says
        # to update the list and the docs together rather than one of them.
        present = [d for d in KNOWN_BAD_REPO_DIRS if (REPO_ROOT / d).exists()]
        self.assertEqual(
            present,
            [],
            f"{present} now exists; update KNOWN_BAD_REPO_DIRS and the docs together",
        )

    def test_use_retired_artifacts_are_absent(self):
        # RETIRED_ARTIFACTS switches off three rules for the names on it,
        # inside evidence/. That is safe only while those names are not files.
        # If one of them is ever restored, the exemption starts protecting a
        # path that exists, and worse, it starts protecting whatever a
        # transcript says to do with it. Same shape as the local-rig alarm
        # above: the list and the tree have to be changed together, and this
        # is what refuses to let them drift apart.
        present = [a for a in sorted(RETIRED_ARTIFACTS) if (REPO_ROOT / a).exists()]
        self.assertEqual(
            present,
            [],
            f"{present} is back in the tree; update RETIRED_ARTIFACTS and the "
            f"docs together, and say why the tool is here again",
        )

    def test_use_markdown_corpus_is_discovered(self):
        # The `docs` floor in TestGuardsAreNotVacuous catches discovery that
        # stops at the repo root. It does not catch discovery that stops one
        # level down: root plus the evidence write-ups plus two README files
        # already clears the floor with every SKILL.md runbook silently
        # unchecked, and the runbooks are where the copy-and-run commands
        # live. Naming a file at each depth is what closes that gap.
        #
        # The named skill is es-snapshot-audit because it is the one that
        # survived the sweeper retirement. It drives snapshot_sizes.py, which
        # is read-only and has no delete path, so it is not going anywhere for
        # the reason the others went.
        #
        # Only structurally stable docs are named. The narrative write-ups
        # get reorganised, and this test is about discovery reaching every
        # depth, not about any given essay sitting at any given path.
        found = {str(p.relative_to(REPO_ROOT)) for p in markdown_files(REPO_ROOT)}
        for expected in (
            "README.md",
            os.path.join("manifests", "README.md"),
            os.path.join("skills", "README.md"),
            os.path.join("skills", "es-snapshot-audit", "SKILL.md"),
        ):
            self.assertIn(expected, found)
        self.assertGreaterEqual(
            len(found), 6, "markdown discovery is sweeping too little of the repo"
        )

class TestGuardsAreNotVacuous(unittest.TestCase):
    """The USE guards pass when they find nothing. So does a broken extractor.

    Every USE guard asserts `findings == []`. That is also what you get when
    the link regex stops matching, or discovery stops descending, or a
    refactor drops a call. The suite would report five green rules while
    reading nothing at all, and the next rename would ship the same nine
    dangling links as last time. These tests count the material each rule
    sees, so going blind fails loudly instead of quietly.
    """

    def test_use_corpus_gives_every_rule_something_to_check(self):
        # If the link regex breaks or discovery stops descending, all five
        # USE guards report zero findings and pass. This is the tripwire for
        # that, and it is the reason those guards can be trusted at all.
        #
        # Floors, not exact counts. A number that has to be edited every time
        # somebody adds a paragraph gets edited without thinking, and then it
        # guards nothing. These sit below today's corpus so ordinary prose
        # changes never touch them, and far enough above zero that an
        # extractor which quietly stopped working cannot clear them.
        counts = corpus_coverage(REPO_ROOT)
        floors = {
            "docs": 6,
            "relative_links": 20,
            "in_page_anchors": 3,
            "cross_doc_anchors": 2,
            "bare_paths": 10,
            "fenced_refs": 20,
        }
        for key, floor in floors.items():
            with self.subTest(rule=key):
                self.assertGreaterEqual(
                    counts[key],
                    floor,
                    f"only {counts[key]} {key} found; the extractor is probably "
                    f"broken, which would make the USE guards pass vacuously",
                )

    def test_abuse_an_empty_repo_yields_no_coverage(self):
        # The floors above mean something only if the counter reports what it
        # actually found. Point it at an empty directory and it has to say
        # zero. A counter that returned a constant, or that counted material
        # from outside the corpus, would clear every floor forever and take
        # the blindness guard down with it, quietly, while looking green.
        with tempfile.TemporaryDirectory() as tmp:
            counts = corpus_coverage(Path(tmp).resolve())
        self.assertEqual(set(counts.values()), {0})

    def test_abuse_cross_doc_anchor_counter_sees_exactly_one_link(self):
        # The floor above says how many deep links the corpus has. It cannot
        # say whether the counter is reading them or guessing. One link, one
        # count, in a repo holding nothing else: that is the difference
        # between a floor that measures the corpus and a floor that measures
        # a constant.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "A.md").write_text("[x](B.md#some-heading)\n", encoding="utf-8")
            (root / "B.md").write_text("## Some heading\n", encoding="utf-8")
            self.assertEqual(corpus_coverage(root)["cross_doc_anchors"], 1)


# --------------------------------------------------------------------------
# Rule 1: relative link targets exist
# --------------------------------------------------------------------------


class TestRelativeLinks(MiniRepoTestCase):
    def test_use_real_corpus_has_no_broken_relative_links(self):
        # The nine dangling links. Three write-ups moved under `evidence/`
        # mid-session and six files kept pointing at the old root paths.
        # Broken relative links still render, still look clickable, and only
        # fail once a reader clicks one, so reading the diff did not catch a
        # single one. This is the check that turns that class of rename into
        # a red test at the moment of the rename.
        findings = check_relative_links(REPO_ROOT)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_checker_catches_a_broken_absolute_self_link(self):
        # The docs write full GitHub URLs so they resolve for a reader who is
        # not browsing a clone. Those are self-links wearing an external
        # costume: skip them as external and every one of the five rules goes
        # quiet, which is exactly what happened the day the docs converted.
        # One good, one dangling, so the rule has to tell them apart rather
        # than flagging or ignoring the whole shape.
        base = ("https://github.com/thanatostyrannos/"
                "elasticsearch-oci-s3-workaround/blob/main")
        self.repo.write("README.md",
                        f"See [here]({base}/FACTS.md) and [gone]({base}/NOPE.md).\n")
        self.repo.write("FACTS.md", "# Facts\n")
        findings = check_relative_links(self.repo.root)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("NOPE.md", findings[0])

    def test_abuse_absolute_self_link_resolves_from_the_root_not_the_doc(self):
        # A URL names its path from the repository root. Resolve it from the
        # linking document instead and evidence/README.md pointing at the
        # top-level README.md silently resolves to itself, so the anchor gets
        # checked against the wrong file and a dangling link reads as fine.
        base = ("https://github.com/thanatostyrannos/"
                "elasticsearch-oci-s3-workaround/blob/main")
        self.repo.write("README.md", "## Real heading\n")
        self.repo.write("evidence/README.md",
                        f"[up]({base}/README.md#real-heading)\n")
        self.assertEqual(check_cross_doc_anchors(self.repo.root), [])
        self.repo.write("evidence/README.md",
                        f"[up]({base}/README.md#not-a-heading)\n")
        findings = check_cross_doc_anchors(self.repo.root)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not-a-heading", findings[0])

    def test_abuse_checker_catches_a_link_to_a_moved_file(self):
        # That rename in miniature: one link to a doc still in place, one to
        # a doc that moved, both in the same sentence, which is how real docs
        # carry them. The checker has to separate the two. Flag them both and
        # people stop believing the rule. Flag neither and we are back where
        # this file started.
        self.repo.write("README.md", "See [the method](METHODOLOGY.md) and\n"
                                     "[the gone one](docs/OLD-PLACE.md).\n")
        self.repo.write("METHODOLOGY.md", "# Method\n")
        findings = check_relative_links(self.repo.root)
        self.assertFinding(findings, "docs/OLD-PLACE.md")
        self.assertFalse(
            any("METHODOLOGY.md'" in f for f in findings),
            f"the live link should not be flagged: {findings}",
        )

    def test_abuse_checker_catches_a_broken_parent_relative_link(self):
        # `../SOMETHING.md` from a subdirectory is the exact shape most of
        # the nine dangling links had, and it is the shape our resolver is
        # likeliest to get wrong: `_resolve` tries the doc's own directory
        # and the repo root, so a bug in either base makes a link that
        # escapes the repo look like it resolves.
        self.repo.write("skills/README.md", "[evidence](../EVIDENCE.md)\n")
        findings = check_relative_links(self.repo.root)
        self.assertFinding(findings, "../EVIDENCE.md")

    def test_abuse_external_urls_are_never_fetched_or_flagged(self):
        # The docs cite Elasticsearch source files and upstream issues by
        # URL, constantly. Treat one of those as a path and the rule reports
        # findings nobody can fix, and a rule that cries wolf gets skipped
        # along with the real findings next to it. Nothing here touches the
        # network either, so a bad connection cannot turn the run red.
        self.repo.write(
            "README.md",
            "[dead](https://example.invalid/nope) "
            "[mail](mailto:nobody@example.invalid)\n",
        )
        self.assertEqual(check_relative_links(self.repo.root), [])


# --------------------------------------------------------------------------
# Rule 2: in-page anchors match real headings
# --------------------------------------------------------------------------


class TestInPageAnchors(MiniRepoTestCase):
    def test_use_real_corpus_has_no_dangling_in_page_anchors(self):
        # README carries internal jumps into its own sections, including one
        # whose slug nobody would guess by hand. Reword any of those headings
        # and the links keep rendering and land the reader at the top of a
        # 900-line page instead of at the section they were promised. This
        # fails at the reword, before anyone ships it.
        findings = check_inpage_anchors(REPO_ROOT)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_checker_catches_an_anchor_to_a_renamed_heading(self):
        # A heading gets reworded and the anchor pointing at it does not.
        # That is the ordinary way in-page links rot: the edit looks local,
        # the breakage is three hundred lines away, and nothing renders red.
        self.repo.write(
            "README.md",
            "[jump](#root-cause-and-upstream-status)\n\n"
            "## Root cause, restated\n",
        )
        findings = check_inpage_anchors(self.repo.root)
        self.assertFinding(findings, "#root-cause-and-upstream-status")

    def test_abuse_checker_catches_an_anchor_that_forgot_the_slug_rules(self):
        # An anchor typed by eye, keeping the punctuation GitHub strips.
        # README's own Step 0 heading carries backticks, a question mark and
        # parentheses, so anyone linking to it by hand writes this exact
        # mistake. The link renders, GitHub 404s it, and the checker has to
        # be the thing that says so.
        self.repo.write(
            "README.md",
            "[jump](#step-0-keep-it-operational-(verify=false))\n\n"
            "## Step 0 — keep it operational (`verify=false`)\n",
        )
        findings = check_inpage_anchors(self.repo.root)
        self.assertTrue(findings, "punctuated anchor should not have matched")


# --------------------------------------------------------------------------
# Rule 3: cross-document anchors match a heading in the target file
# --------------------------------------------------------------------------


class TestCrossDocumentAnchors(MiniRepoTestCase):
    """Rule 3 judges the one link shape rule 1 is blind to.

    Rule 1 strips the fragment and is satisfied once the file exists, so a
    deep link into a heading that got reworded sails straight past it. The
    corpus has real deep links now, several of them into the section of
    docs/blast-radius.md that settles what recovery is available through the
    Amazon S3 Compatibility API. That section is the one a reader is sent to
    before they delete anything, and a link that lands them at the top of a
    thousand-line document instead is the failure this rule exists to stop.
    """

    def test_use_real_corpus_has_no_dangling_cross_doc_anchors(self):
        # Live material, and paired on purpose with the extractor test in
        # TestGuardsAreNotVacuous, which is what stops a broken extractor
        # from reporting green over nothing.
        findings = check_cross_doc_anchors(REPO_ROOT)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_checker_catches_an_anchor_missing_from_the_target(self):
        # The failure rule 1 is blind to. `METHODOLOGY.md#the-rig-recipe`
        # names a file that exists, so rule 1 passes it, while the section it
        # promises is gone. The reader lands at the top of a long document
        # and has to go hunting for something that is not there any more.
        self.repo.write("README.md", "[see](METHODOLOGY.md#the-rig-recipe)\n")
        self.repo.write("METHODOLOGY.md", "# Method\n\n## Building the rig\n")
        findings = check_cross_doc_anchors(self.repo.root)
        self.assertFinding(findings, "#the-rig-recipe")

    def test_use_a_matching_cross_doc_anchor_passes(self):
        # A correct deep link written from a subdirectory, which is the shape
        # these links will take now that the write-ups live under evidence/.
        # If the checker cannot follow `../`, the first person to write one
        # gets a red build for a link that works, and the rule gets deleted
        # rather than fixed.
        self.repo.write("skills/README.md", "[see](../METHODOLOGY.md#building-the-rig)\n")
        self.repo.write("METHODOLOGY.md", "# Method\n\n## Building the rig\n")
        self.assertEqual(check_cross_doc_anchors(self.repo.root), [])


# --------------------------------------------------------------------------
# Rule 4: bare repo paths named in prose exist
# --------------------------------------------------------------------------


class TestBareRepoPaths(MiniRepoTestCase):
    def test_use_real_corpus_names_only_paths_that_exist(self):
        # Prose names repo files constantly, outside any link and outside any
        # code block: read `skills/es-snapshot-audit/SKILL.md`, fixtures live
        # in `tests/fixtures/`. Nothing renders those, so nothing looks broken
        # when they go stale. They just send readers somewhere empty, which
        # is how the same rename that produced the nine dangling links also
        # left wrong paths sitting in plain sentences.
        findings = check_bare_repo_paths(REPO_ROOT)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_checker_catches_a_moved_skill_file(self):
        # A skill directory gets renamed and a doc keeps naming the old path
        # next to a live one. Both cases in one sentence, because that is how
        # they turn up in real docs, and the checker has to tell them apart.
        self.repo.write("skills/es-snapshot-audit/SKILL.md", "# Audit\n")
        self.repo.write(
            "README.md",
            "Read `skills/es-snapshot-audit/SKILL.md` and "
            "`skills/es-snapshot-sizing/SKILL.md`.\n",
        )
        findings = check_bare_repo_paths(self.repo.root)
        self.assertFinding(findings, "skills/es-snapshot-sizing/SKILL.md")
        self.assertFalse(
            any("es-snapshot-audit" in f for f in findings),
            f"the live skill path should not be flagged: {findings}",
        )

    def test_use_object_store_prefixes_are_not_treated_as_repo_paths(self):
        # These docs are largely shell transcripts, and the paths in them are
        # bucket keys and mc aliases, not repo paths. This pins the call that
        # keeps the rule usable: judge a token only when its first segment is
        # a real top-level directory, and keep no allowlist of bucket
        # prefixes. An allowlist needs editing forever and grows wide enough
        # in the end to swallow the stale path the rule exists to find.
        self.repo.write(
            "EVIDENCE.md",
            "Key `indices/GVsRrzdESB-K1azSkyk4fA/0/index-3` and "
            "`mc mirror rig/es-snapshots ./repo-mirror` and `vf-test/x.json`.\n",
        )
        self.assertEqual(check_bare_repo_paths(self.repo.root), [])

    def test_abuse_regex_does_not_fire_on_ordinary_prose(self):
        # "and/or", a ratio, and an Elasticsearch API route. If the path
        # regex fired on any of these, every document in the repo would
        # produce findings, the rule would be switched off inside a week, and
        # the stale-path detection would go with it. The API routes matter
        # most: `_snapshot/...` appears on nearly every page here.
        self.repo.write(
            "README.md",
            "Use and/or judgement; the ratio was 3/4; call `DELETE /_snapshot/x`.\n",
        )
        self.assertEqual(check_bare_repo_paths(self.repo.root), [])


# --------------------------------------------------------------------------
# Rule 4b: no doc points readers at a directory the repo does not have
# --------------------------------------------------------------------------


class TestAbsentDirectories(MiniRepoTestCase):
    def test_use_real_corpus_never_names_an_absent_directory(self):
        # The local-rig defect, pinned. A results doc told readers the rig
        # manifests lived in `local-rig/`. This repository keeps them in
        # `manifests/` and has never had a `local-rig/` at all. Rules 1 and
        # 4a both missed it: a directory that does not exist is not a link,
        # and 4a only judges tokens whose first segment is a real top-level
        # directory, so the wrong name was invisible to every other rule.
        findings = check_absent_directories(REPO_ROOT)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_checker_catches_the_local_rig_defect_in_prose(self):
        # The prose half of the real defect, reproduced against a repo whose
        # manifests sit where ours do. Prose is how a wrong directory name
        # spreads: somebody copies the sentence into the next document as
        # context and now two files are wrong.
        self.repo.mkdir("manifests")
        self.repo.write(
            "TEST-RESULTS.md",
            "Manifests in the companion `local-rig/` directory of the rig.\n",
        )
        findings = check_absent_directories(self.repo.root)
        self.assertFinding(findings, "local-rig/")

    def test_abuse_checker_catches_the_local_rig_defect_in_a_code_block(self):
        # The other half, and the expensive one. `kubectl apply -f local-rig/`
        # sat in a fenced block for readers to copy and paste, and it could
        # never have worked for a single one of them. Rules 1 and 2 skip
        # fenced content by design, so this rule has to read every line of
        # every file, and this is the test that holds it to that.
        self.repo.mkdir("manifests")
        self.repo.write(
            "TEST-RESULTS.md",
            "```bash\nkubectl --context rancher-desktop apply -f local-rig/\n```\n",
        )
        findings = check_absent_directories(self.repo.root)
        self.assertFinding(findings, "local-rig/")

    def test_abuse_rule_goes_quiet_if_the_directory_is_ever_created(self):
        # A denylist nobody can satisfy is a denylist somebody deletes. If a
        # real `local-rig/` is ever added, the rule has to fall silent on its
        # own, so the fix is creating the directory rather than ripping the
        # check out of the file.
        self.repo.mkdir("local-rig")
        self.repo.write("TEST-RESULTS.md", "See `local-rig/`.\n")
        self.assertEqual(check_absent_directories(self.repo.root), [])

    def test_use_a_url_containing_the_name_is_not_flagged(self):
        # No document contains the string `local-rig` any more, so the USE
        # guard above gives this rule no live exercise at all. This is the
        # only test holding the match narrow: the name has to stand as a path
        # segment of its own, not appear inside a URL somebody links to. Lose
        # that and a single external link turns the rule into a false alarm,
        # and a false alarm on the one rule with no live material is a rule
        # that gets deleted.
        self.repo.write("README.md", "See https://example.com/local-rig/setup\n")
        self.assertEqual(check_absent_directories(self.repo.root), [])


# --------------------------------------------------------------------------
# Rule 5: files invoked in fenced code blocks exist
# --------------------------------------------------------------------------


class TestFencedFileReferences(MiniRepoTestCase):
    def test_use_real_corpus_only_invokes_files_that_exist(self):
        # Every fenced block in these docs is meant to be copied and run, and
        # dozens of them name a file in this repo. Rename a script or a
        # manifest and each one becomes a command that dies on the reader's
        # terminal with nothing in the doc to explain why. That is the same
        # defect as `kubectl apply -f local-rig/`, one rule further along.
        findings = check_fenced_file_references(REPO_ROOT)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_abuse_checker_catches_a_renamed_script(self):
        # A script rename with the old name left behind in a command block,
        # sitting next to the new one. Separating those is the whole job:
        # flag the live invocation once and nobody trusts the rule again.
        self.repo.write("snapshot_sizes.py", "")
        self.repo.write(
            "README.md",
            "```bash\npython3 snapshot_sizes.py --help\n"
            "python3 snapshot_size.py --help\n```\n",
        )
        findings = check_fenced_file_references(self.repo.root)
        self.assertFinding(findings, "snapshot_size.py")
        self.assertFalse(
            any("snapshot_sizes.py'" in f for f in findings),
            f"the live script should not be flagged: {findings}",
        )

    def test_abuse_checker_catches_dot_slash_invocation_of_a_missing_script(self):
        # README and the skill runbooks write `./snapshot_sizes.py`, not
        # `python3 snapshot_sizes.py`. That is a separate branch of the
        # invocation pattern, and if it quietly stopped matching, every
        # command in the runbooks would go unchecked while the rule kept
        # reporting green over the ones that still matched.
        self.repo.write("skills/es-snapshot-audit/SKILL.md", "```bash\n./snapshot_size.py\n```\n")
        findings = check_fenced_file_references(self.repo.root)
        self.assertFinding(findings, "snapshot_size.py")

    def test_abuse_checker_catches_kubectl_apply_on_a_missing_manifest(self):
        # The rig instructions are a run of `kubectl apply -f manifests/...`
        # lines. Rename or drop one manifest and the reader gets a step that
        # fails halfway through, with a namespace and a MinIO already up and
        # no obvious way to tell which half of the rig they now have.
        self.repo.write("manifests/minio.yaml", "")
        self.repo.write(
            "METHODOLOGY.md",
            "```bash\nkubectl $CTX apply -f manifests/minio.yaml\n"
            "kubectl --context rancher-desktop apply -f local-rig/\n```\n",
        )
        findings = check_fenced_file_references(self.repo.root)
        self.assertFinding(findings, "local-rig/")

    def test_use_stdin_apply_and_piped_urls_are_ignored(self):
        # `apply -f -` is live in manifests/README.md and in the methodology
        # write-up, where the ECK operator gets piped in from a release URL.
        # Read that `-` as a filename and both documents fail this rule
        # forever, for commands that are correct, which ends with somebody
        # deleting the rule instead of the bug.
        self.repo.write(
            "manifests/README.md",
            "```bash\ncurl -fsSL https://example.com/operator.yaml "
            "| kubectl $CTX apply -f -\n```\n",
        )
        self.assertEqual(check_fenced_file_references(self.repo.root), [])

    def test_use_doc_relative_and_root_relative_both_resolve(self):
        # The docs mix two conventions and both are correct.
        # manifests/README.md writes `minio.yaml`, meaning the file beside
        # it. The skills write `./snapshot_sizes.py`, meaning run it from the
        # repo root. Resolve against one base only and half the corpus turns
        # red for commands that work.
        self.repo.write("manifests/minio.yaml", "")
        self.repo.write("snapshot_sizes.py", "")
        self.repo.write("manifests/README.md", "```bash\nkubectl apply -f minio.yaml\n```\n")
        self.repo.write("skills/a/SKILL.md", "```bash\n./snapshot_sizes.py --help\n```\n")
        self.assertEqual(check_fenced_file_references(self.repo.root), [])


# --------------------------------------------------------------------------
# The retirement exemption: history is not an instruction
# --------------------------------------------------------------------------


class TestRetiredArtifacts(MiniRepoTestCase):
    """Three sweepers and their tests were removed. Their names were not.

    `evidence/` holds captured terminal output from runs that really
    happened, and those runs were of the tools that are now gone. Rewriting a
    transcript to take the command out of it would falsify the record, so
    rules 1, 4a and 5 let a retired name stand inside `evidence/`.

    An exemption is only as good as its edges, and this one has two that
    matter. It covers the named files and nothing else, so a genuinely stale
    path in a transcript is still found. It covers `evidence/` and nowhere
    else, so a runbook that tells an operator to run a retired sweeper is
    still found, which is the case that costs data rather than a reader's
    afternoon.
    """

    RETIRED_COMMAND = "```bash\npython3 s3_repo_sweeper.py --dry-run\n```\n"

    def test_use_a_retired_command_in_a_transcript_is_not_a_finding(self):
        # The captured run. This command was typed, it produced the output
        # underneath it, and the tool it names has since been removed. Flag
        # it and the only way to a green suite is editing the transcript,
        # which is the one thing nobody should do to a record of what
        # happened.
        self.repo.write("evidence/runbook-transcript.md", self.RETIRED_COMMAND)
        self.assertEqual(check_fenced_file_references(self.repo.root), [])

    def test_abuse_the_same_command_in_a_runbook_is_a_finding(self):
        # The half of the exemption that does work. A procedure telling an
        # operator to run a retired sweeper is a procedure to delete
        # production data with a tool that is not in the repository. This is
        # the check that stops one of those coming back, whether somebody
        # restores a deleted runbook or writes a new one from memory.
        self.repo.write("skills/es-orphan-sweep/SKILL.md", self.RETIRED_COMMAND)
        findings = check_fenced_file_references(self.repo.root)
        self.assertFinding(findings, "s3_repo_sweeper.py")

    def test_abuse_the_exemption_covers_only_the_names_on_the_list(self):
        # An exemption scoped to a directory rather than to a list would
        # switch rule 5 off for every transcript in the repository, and those
        # transcripts are full of `./snapshot_sizes.py` invocations against a
        # tool that is still here and still renameable. The list is what
        # keeps the rest of the corpus under the rule.
        self.repo.write(
            "evidence/runbook-transcript.md",
            "```bash\n./snapshot_size.py --repo r\n```\n",
        )
        findings = check_fenced_file_references(self.repo.root)
        self.assertFinding(findings, "snapshot_size.py")

    def test_use_a_retired_test_path_in_evidence_prose_is_left_alone(self):
        # The test-results write-up counts tests per file by name. Those
        # counts are what was measured on the day, and the file names are
        # part of the measurement. Rule 4a would otherwise report every one
        # of them as a stale path.
        self.repo.write(
            "evidence/test-results.md",
            "`tests/test_oci_stdlib_client.py` contributed 63 tests.\n",
        )
        self.repo.mkdir("tests")
        self.assertEqual(check_bare_repo_paths(self.repo.root), [])

    def test_abuse_the_same_path_in_the_readme_is_a_finding(self):
        # A README sentence is a claim about the repository as it stands, not
        # a record of a past run. Sending a contributor to a test file that
        # was deleted wastes exactly the time this whole file exists to save.
        self.repo.mkdir("tests")
        self.repo.write(
            "README.md",
            "The signing vectors live in `tests/test_oci_stdlib_client.py`.\n",
        )
        findings = check_bare_repo_paths(self.repo.root)
        self.assertFinding(findings, "tests/test_oci_stdlib_client.py")

    def test_use_a_link_to_a_retired_file_from_a_transcript_is_left_alone(self):
        # Same reasoning as the command, one link shape along. A write-up
        # that says "the decoder is in [oci_repo_sweeper.py](../oci_repo_sweeper.py)"
        # was true when it was written and is part of what the reader needs
        # to follow the run.
        self.repo.write(
            "evidence/campaign-data.md",
            "Classified by [the sweeper](../oci_repo_sweeper.py).\n",
        )
        self.assertEqual(check_relative_links(self.repo.root), [])

    def test_abuse_a_link_to_a_retired_file_from_the_readme_is_a_finding(self):
        # The README is where a reader goes to find out what this repository
        # contains. A clickable link to a file that is not here answers that
        # question wrongly, and it renders exactly like a working one.
        self.repo.write(
            "README.md",
            "Read [the sweeper](oci_repo_sweeper.py).\n",
        )
        findings = check_relative_links(self.repo.root)
        self.assertFinding(findings, "oci_repo_sweeper.py")



if __name__ == "__main__":
    unittest.main()
