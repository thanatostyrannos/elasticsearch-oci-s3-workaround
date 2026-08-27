"""The explicit, per-manifest approval gate.

A flag that means "delete whatever the manifest says" is automation, and this
project's standing rule is that deletion stays a human decision about one
named set of keys. So approval here is not a flag; it is two values an
operator must produce from the manifest they are about to act on:

  --approve-digest   the sha256 of the manifest's exact bytes
  --approve-rows     how many keys it names

`--approve-digest` is what makes approving one manifest unable to approve a
different one. sha256 changes on any edit, including a regenerated run that
condemns the same repository and produces a byte-for-byte different file, so
an operator who approved yesterday's manifest cannot accidentally execute
against today's. `--approve-rows` is redundant with the digest in the
adversarial sense, since two different byte sequences sharing a sha256 digest
is not a real threat here, and it is deliberately asked for anyway: a hash is
not a number a human notices is wrong, and a row count is. An operator who
means to approve 89,256 keys and mistypes or copies a stale 45,000 has a
second, human-legible value that must also match, independent of whichever
file the digest happens to name.
"""

from __future__ import annotations

from ..errors import GenerationChainError
from .manifest import ManifestData

MIN_DIGEST_LENGTH = 64  # sha256 in hex


class ApprovalError(GenerationChainError):
    """The approval given does not settle on this manifest, so nothing runs."""


def verify_approval(manifest: ManifestData, approve_digest: str,
                    approve_rows: int) -> None:
    """Raise `ApprovalError` unless both values match this exact manifest.

    Checked independently and both reported when both are wrong, so an
    operator fixing one does not have to re-run to discover the other.
    """
    problems = []
    given = approve_digest.strip().lower()
    if len(given) != MIN_DIGEST_LENGTH or given != manifest.digest:
        problems.append(
            f"--approve-digest {approve_digest!r} does not match this "
            f"manifest's sha256 ({manifest.digest}). Recompute it from "
            f"{manifest.path} as it stands right now; an approval for a "
            "different file, or an edited or regenerated copy of this one, "
            "must not carry over")
    actual_rows = len(manifest.keys)
    if approve_rows != actual_rows:
        problems.append(
            f"--approve-rows {approve_rows} does not match the "
            f"{actual_rows} key(s) {manifest.path} names right now")
    if problems:
        raise ApprovalError(
            "the approval given does not settle on this manifest, so "
            "nothing was deleted. " + " Also, ".join(problems))
