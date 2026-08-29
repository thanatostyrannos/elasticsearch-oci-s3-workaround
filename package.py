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

That boundary is also where the security surface narrows. Every
credential-shaped string a scanner finds in this repository lives under
`tests/`: a real-format RSA key pinning the OCI signing vector, AWS's published
example key pair, and the detection patterns belonging to the committed-
credential scanner itself. None of it ships, and `PACKAGED_MUST_NOT_CONTAIN`
refuses the build rather than trusting that to stay true.

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

REPRODUCIBILITY IS NOT A FLOURISH

Timestamps are pinned and members are sorted, so two builds of one commit are
identical byte for byte. A hash nobody can reproduce attests to nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
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
    # Ships because docs/quickstart-test-rig.md tells the reader to run it.
    # It was excluded once as lab tooling, which left a shipped document
    # instructing someone to run a file the release did not contain.
    "snapshot_churn_rig.py",      # builds a leaking repository to test against
    # Ships because docs/testing-in-your-oci-environment.md tells the reader
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
#   skills/           methodology notes for people working ON this project
#   snapshot_sizes.py       a reporting side tool, not on the reclaim path
#   CONTRIBUTING.md         addressed to contributors, not operators

# Shapes that must never appear in a shipped file. The build refuses rather
# than warning, because a warning in a build log is a warning nobody reads.
PACKAGED_MUST_NOT_CONTAIN = (
    b"BEGIN RSA PRIVATE KEY",
    b"BEGIN PRIVATE KEY",
    b"BEGIN EC PRIVATE KEY",
    b"BEGIN OPENSSH PRIVATE KEY",
)

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


def tree_members(tree, suffixes):
    """Every shipped file under one packaged tree, relative to the root."""
    found = []
    for directory, _, names in os.walk(os.path.join(ROOT, tree)):
        if "__pycache__" in directory:
            continue
        for name in names:
            if name.endswith(suffixes):
                absolute = os.path.join(directory, name)
                found.append(os.path.relpath(absolute, ROOT))
    return found


def named_members():
    """The individually listed files, refusing the build if one has moved."""
    for name in PACKAGED_FILES:
        if not os.path.exists(os.path.join(ROOT, name)):
            raise ReleaseRefused(
                f"{name} is named in PACKAGED_FILES and is not in the tree. "
                "Either it moved and the list is stale, or the release is "
                "missing something an operator was promised.")
    return list(PACKAGED_FILES)


def members():
    """Every path that ships, relative to the repository root, sorted."""
    found = []
    for tree, suffixes in PACKAGED_TREES:
        found.extend(tree_members(tree, suffixes))
    return sorted(found + named_members())


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
    for shape in PACKAGED_MUST_NOT_CONTAIN:
        if shape in body:
            raise ReleaseRefused(
                f"{relative} carries {shape.decode()!r} and would have been "
                "shipped. Nothing in the release set may contain key "
                "material. Fix the file or remove it from the release set; "
                "do not relax this check.")


def is_documentation(relative):
    return relative.startswith(DOCUMENTATION_PREFIXES)


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
            info = zipfile.ZipInfo(f"{stem}/{name}", date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, body)
        info = zipfile.ZipInfo(f"{stem}/MANIFEST.sha256",
                               date_time=FIXED_TIMESTAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, manifest)

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
