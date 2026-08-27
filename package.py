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
    # .json so the raw security scan artifacts ship beside the reports they
    # were built from. A report nobody can check is an assertion.
    ("docs", (".md", ".json")),
    # The operator-facing loop runner and its example config. Someone testing
    # this has a shell, not necessarily anything else.
    ("scripts", (".sh", ".example")),
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


class ReleaseRefused(Exception):
    """The build stopped rather than shipping something it should not."""


def members():
    """Every path that ships, relative to the repository root, sorted."""
    found = []
    for tree, suffixes in PACKAGED_TREES:
        base = os.path.join(ROOT, tree)
        for directory, _, names in os.walk(base):
            if "__pycache__" in directory:
                continue
            for name in names:
                if not name.endswith(suffixes):
                    continue
                absolute = os.path.join(directory, name)
                found.append(os.path.relpath(absolute, ROOT))
    for name in PACKAGED_FILES:
        if not os.path.exists(os.path.join(ROOT, name)):
            raise ReleaseRefused(
                f"{name} is named in PACKAGED_FILES and is not in the tree. "
                "Either it moved and the list is stale, or the release is "
                "missing something an operator was promised.")
        found.append(name)
    return sorted(found)


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
    os.makedirs(destination, exist_ok=True)
    stem = f"{NAME}-{version}" if version else NAME
    archive = os.path.join(destination, stem + ".zip")

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
    print(open(archive + ".sha256").read().strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
