#!/usr/bin/env python3
"""Build the release archive: what an operator needs, and nothing else.

WHAT SHIPS, AND WHY THE LIST IS AN ALLOWLIST

Someone reclaiming a leaking snapshot repository needs five things: the audit
that decides what is unreferenced, the delete path with its approval gate, the
harness that exercises both against their own repository, the load generator
that builds a repository worth exercising them against, and the documentation.
They do not need this project's test suite, its captured evidence, the
Terraform that provisions a probe tenancy, or the Kubernetes manifests for a
lab cluster.

The load generator earns its place because the documentation tells the reader
to run it. It was left out once, which shipped a document instructing someone
to run a file the archive did not contain.

The list below names what goes in rather than what stays out. An exclusion list
fails open: a directory added next year ships by accident. An allowlist fails
closed, which is the direction this project resolves every other uncertainty.

That boundary is also where the security surface narrows. A real-format RSA
key pinning the OCI signing vector and the detection patterns belonging to the
committed-credential scanner live under `tests/` and do not ship. AWS's
published example key pair does ship, in `generation_chain/selftest.py`,
because the operator's self-test checks the SigV4 signer against the
signature AWS publishes for that pair.
`PRIVATE_KEY_LABEL` refuses the build if any shipped file carries private key
armour, rather than trusting the exclusions to stay true.

THE PAYLOAD DIGEST, AND THE ORDERING PROBLEM IT SOLVES

The security scan covers the release, and its report ships inside the release.
The report therefore cannot quote the archive's own hash without invalidating
itself the moment it is added.

So the archive carries `MANIFEST.sha256`, listing every member with its digest,
and a PAYLOAD DIGEST computed over the code members alone, with documentation
excluded. That number does not move when a report is written, so a report can
attest to the exact code it was produced from while living beside it. The
archive's own hash is written next to the archive, for whoever is checking that
the file they received is the file that was built.

THE ARCHIVE REFLECTS A COMMIT

Files come from `git ls-files`, filtered by the rules below, so an untracked
file beside shipped code (a local creds.json, a scratch note) cannot enter the
archive. The build refuses outside a git work tree and when any file that
would ship has uncommitted changes.

REPRODUCIBILITY IS NOT A FLOURISH

Timestamps are pinned and members are sorted, so two builds of one commit are
identical byte for byte. A hash nobody can reproduce attests to nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "elasticsearch-oci-s3-workaround"

# Every zip entry gets this stamp. Any fixed value works; this one is the
# earliest a zip can represent, so it is obviously deliberate rather than a
# build date someone might mistake for provenance.
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Directories shipped whole, by extension.
PACKAGED_TREES = (
    ("generation_chain", (".py",)),
    # .json so the raw scanner artifacts ship beside the reports built from
    # them, and .xml/.ckl so the STIG checklist ships in the format the STIG
    # tooling actually reads. A compliance claim an assessor cannot open in
    # their own tool is a claim they have to take on trust.
    ("docs", (".md", ".json", ".xml", ".ckl", ".cklb")),
    # The operator-facing loop runner and its example config. Someone testing
    # this has a shell, not necessarily anything else.
    ("scripts", (".sh", ".example")),
    # The GitLab pipelines and the Helm chart. Same reason as the load
    # generator: the documentation tells the reader to use them.
    ("gitlab", (".yml", ".yaml", ".md", ".tpl", ".txt")),
)

# Individual files shipped, each with the reason it earns its place.
PACKAGED_FILES = (
    "reclaim_test_protocol.py",   # exercises the audit against a live repository
    "verify_restorable.py",       # turns "we did not break it" into a number
    # Ships because docs/testing-guide.md tells the reader to run it.
    # It was excluded once as lab tooling, which left a shipped document
    # instructing someone to run a file the release did not contain.
    "snapshot_churn_rig.py",      # builds a leaking repository to test against
    # Ships because docs/testing-guide.md tells the reader
    # to use it. Same reason as the load generator above.
    ".gitlab-ci.yml",             # runs the audit on a schedule, the rig on demand
    "README.md",                  # how to run all of it
    "FACTS.md",                   # what was measured, and against what
    "LICENSE",                    # who may use this, and the warranty that is not given
)

# Deliberately absent, recorded so the omission reads as a decision:
#   tests/            this project's own suite, and every secret-shaped fixture
#   evidence/         captured measurement runs, large and of no operational use
#   terraform/        provisions a tenancy, a user and a customer secret key
#   manifests/        Kubernetes objects for a lab cluster
#   snapshot_sizes.py       a reporting side tool, not on the reclaim path
#   CONTRIBUTING.md         addressed to contributors, not operators

# A private key label of any kind: RSA, EC, OPENSSH, ENCRYPTED, DSA, PGP or
# none. Matched on the label alone, without the dashes, so armour that lost
# its exact punctuation on the way into a document is still caught. The build
# refuses rather than warning, because a warning in a build log is a warning
# nobody reads.
PRIVATE_KEY_LABEL = re.compile(rb"BEGIN [A-Z0-9 ]*PRIVATE KEY")

DOCUMENTATION_PREFIXES = ("docs/", "README.md", "FACTS.md")

# --version reaches the filesystem twice: in the archive's own name, and in
# the directory name every member unpacks into. Both are path components, not
# labels, so a value carrying a separator or a parent reference writes the
# archive somewhere the operator did not name and unpacks members outside the
# directory the archive promises. Constrained to the characters a version
# number actually uses, and required to start with an alphanumeric so a
# leading dot or dash cannot start one either.
SAFE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]*\Z")


class ReleaseRefused(Exception):
    """The build stopped rather than shipping something it should not."""


def _git(*args):
    """Run git in the repository root and return its stdout as bytes."""
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    except OSError as exc:
        raise ReleaseRefused(
            f"git could not be run ({exc}). A release is built from the "
            "files a commit tracks, so the build needs git.")
    if done.returncode != 0:
        raise ReleaseRefused(
            f"git {' '.join(args)} failed in {ROOT}: "
            f"{done.stderr.decode(errors='replace').strip()}. A release is "
            "built from a commit, so the build must run inside a git work "
            "tree.")
    return done.stdout


def tracked_files():
    """Every path git tracks, relative to the root, as the index lists them."""
    if _git("rev-parse", "--is-inside-work-tree").strip() != b"true":
        raise ReleaseRefused(
            f"{ROOT} is not inside a git work tree. A release is built from "
            "a commit, so the build must run inside a git checkout.")
    listing = _git("ls-files", "-z", "--full-name", "--", ".")
    return [name.decode() for name in listing.split(b"\0") if name]


def tree_members(tree, suffixes, tracked):
    """Every tracked file under one packaged tree, relative to the root."""
    return [name for name in tracked
            if name.startswith(tree + "/")
            and "__pycache__" not in name.split("/")
            and name.endswith(suffixes)]


def named_members(tracked):
    """The individually listed files, refusing the build if one has moved."""
    held = set(tracked)
    for name in PACKAGED_FILES:
        if name not in held:
            raise ReleaseRefused(
                f"{name} is named in PACKAGED_FILES and is not tracked by "
                "git. Either it moved and the list is stale, it was never "
                "committed, or the release is missing something an operator "
                "was promised.")
    return list(PACKAGED_FILES)


def refuse_uncommitted(shipped):
    """Refuse when any file that would ship differs from the commit.

    `git status --porcelain` reports staged and unstaged edits and deletions
    alike. Untracked files are not in `shipped`, so they never reach this
    check; they are left out earlier.
    """
    report = _git("status", "--porcelain", "-z", "--untracked-files=no",
                  "--", *shipped)
    changed = []
    entries = report.split(b"\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        changed.append(entry[3:].decode(errors="replace"))
        if entry[:1] in (b"R", b"C"):
            index += 1
    if changed:
        raise ReleaseRefused(
            "these files would ship with uncommitted changes: "
            f"{', '.join(sorted(changed))}. A release must reflect a "
            "commit. Commit or discard the changes and build again.")


def shippable():
    """Every tracked path the release rules select, relative to the root.

    The list comes from `git ls-files`, so a file that exists on disk and is
    not committed cannot appear in it. It does not check that the working
    tree matches the commit; `members()` does.
    """
    tracked = tracked_files()
    found = []
    for tree, suffixes in PACKAGED_TREES:
        found.extend(tree_members(tree, suffixes, tracked))
    return sorted(found + named_members(tracked))


def members():
    """Every path that ships, sorted, refusing uncommitted modifications."""
    shipped = shippable()
    refuse_uncommitted(shipped)
    return shipped


def checked_directory(path, purpose):
    """The absolute, symlink-resolved directory to write into, or a refusal.

    An empty or whitespace-only path names no directory, and a path holding a
    NUL byte makes `os.makedirs` raise a bare ValueError from underneath a
    build that has already said where it is writing, which reads as a crash
    rather than a decision. Both are refused here, by the flag that carried
    them.

    The same two refusals and the same resolve as
    `generation_chain.paths.checked_path`, written out here rather than
    imported. That helper also confines every path it returns to
    GENCHAIN_FILE_ROOT, which is the audit's knob for a run driven by
    something other than a person. This is the build tool and not the audit,
    so a root set to bound what the audit may read has no business deciding
    where a release archive lands, and honouring it here would refuse the
    ordinary build into a temporary directory.
    """
    if not path or not path.strip():
        raise ReleaseRefused(
            f"{purpose} was given an empty path. Nothing was written.")
    if "\0" in path:
        raise ReleaseRefused(
            f"{purpose} was given a path holding a NUL byte: {path!r}. "
            "Nothing was written.")
    return os.path.realpath(os.path.expanduser(path))


def archive_path(destination, stem):
    """Where the archive goes, refusing a name that lands outside --out."""
    directory = os.path.realpath(destination)
    archive = os.path.realpath(os.path.join(directory, stem + ".zip"))
    if os.path.dirname(archive) != directory:
        raise ReleaseRefused(
            f"the archive would be written to {archive!r}, which is not in "
            f"{directory!r}. The release goes where --out names it and "
            "nowhere else.")
    return archive


def release_stem(version):
    """The archive's name and the directory its members unpack into."""
    if version is None:
        return NAME
    if not SAFE_VERSION.match(version):
        raise ReleaseRefused(
            f"--version {version!r} is not a version. It names a directory "
            "inside the archive and part of the archive's own filename, so "
            "it may hold only letters, digits, dot, plus, underscore and "
            "dash, and must start with a letter or digit.")
    return f"{NAME}-{version}"


def _refuse_credentials(relative, body):
    found = PRIVATE_KEY_LABEL.search(body)
    if found:
        raise ReleaseRefused(
            f"{relative} carries {found.group().decode()!r} and would have "
            "been shipped. Nothing in the release set may contain key "
            "material. Fix the file or remove it from the release set; "
            "do not relax this check.")


def is_documentation(relative):
    return relative.startswith(DOCUMENTATION_PREFIXES)


def zip_entry(name, body):
    """The zip header for one member, fixed except for what the body decides.

    A body starting with `#!` is a script the documentation runs as
    `./name`, so it unpacks executable. Anything else is 0644. Deciding by
    content rather than by the file's mode on disk keeps two builds of one
    commit identical, including from a checkout that has lost its
    permission bits. `create_system` is pinned to Unix because unzip only
    restores permissions from an archive that says it was made on Unix,
    and zipfile's default depends on the OS doing the build.
    """
    info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if body.startswith(b"#!") else 0o644
    info.external_attr = (0o100000 | mode) << 16
    return info


def build(destination, version=None):
    """Write the archive and its checksum, and return the archive's path."""
    stem = release_stem(version)
    directory = checked_directory(destination, "--out")
    os.makedirs(directory, exist_ok=True)
    archive = archive_path(directory, stem)

    bodies = {}
    for relative in members():
        with open(os.path.join(ROOT, relative), "rb") as handle:
            body = handle.read()
        _refuse_credentials(relative, body)
        bodies[relative] = body

    lines = [f"{hashlib.sha256(b).hexdigest()}  {name}"
             for name, b in sorted(bodies.items())]
    payload = hashlib.sha256()
    for name, body in sorted(bodies.items()):
        if is_documentation(name):
            continue
        payload.update(name.encode() + b"\0" + hashlib.sha256(body).digest())
    lines.append("")
    lines.append(f"payload-sha256 (code only, documentation excluded): "
                 f"{payload.hexdigest()}")
    manifest = ("\n".join(lines) + "\n").encode()

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for name, body in sorted(bodies.items()):
            zf.writestr(zip_entry(f"{stem}/{name}", body), body)
        zf.writestr(zip_entry(f"{stem}/MANIFEST.sha256", manifest), manifest)

    with open(archive, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    with open(archive + ".sha256", "w") as handle:
        handle.write(f"{digest}  {os.path.basename(archive)}\n")
    return archive


def main():
    parser = argparse.ArgumentParser(
        description="Build the release archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"),
                        help="where to write the archive (default: dist/)")
    parser.add_argument("--version",
                        help="appended to the archive name and its root "
                             "directory, e.g. 1.0.0")
    args = parser.parse_args()
    try:
        archive = build(args.out, args.version)
    except ReleaseRefused as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 2
    with zipfile.ZipFile(archive) as zf:
        count = len(zf.namelist())
    print(f"{archive}  ({count} members)")
    with open(archive + ".sha256") as handle:
        print(handle.read().strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
