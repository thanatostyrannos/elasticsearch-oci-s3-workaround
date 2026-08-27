#!/usr/bin/env python3
"""Disable one guard, run the suite, and see whether anything notices.

A guard nobody's test can see the removal of is not a guard, it is a comment.
Reviewers neutered seventeen guards against the retired package and thirteen of
them changed nothing at all, including the pair that was the entire defence
against naming live data.

HOW TO READ THE OUTPUT. Every guard below must come back RED. A guard that
comes back GREEN is unpinned: the suite would not notice if a future change
deleted it, so either it needs a test or it needs deleting.

BYTECODE CACHING IS OFF, AND THAT IS NOT PARANOIA. The developer's own neuter
run produced FALSE GREENS until it was disabled. Two edits of the same size
inside one second left a `.pyc` whose size and mtime still matched, so Python
loaded the ORIGINAL bytecode and reported a guard as proved with nothing
rebuilt. Three things prevent it here: every case runs against a FRESH copy of
the tree, `__pycache__` is deleted before the run, and the child runs with
PYTHONDONTWRITEBYTECODE set.

STRUCTURAL PROTECTIONS ARE NOT IN THIS LIST, and the difference matters. A
conditional guard is an `if` a refactor can delete. A structural one cannot be
expressed away: `ShardHistory` requires a current document, so "treat a missing
index as an empty live set" is not a line to remove, it is a record that will
not construct. Those are stronger than anything this harness can measure, and
listing them here as green would be reporting a strength as a weakness.

    python3 tests/genchain_neuter.py          run every case
    python3 tests/genchain_neuter.py <name>   run one case
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The suite, fastest first, so a pinned guard fails early and costs seconds.
# The property search runs last because it is the slow one and a guard it alone
# pins is exactly the guard worth waiting for.
MODULES = [
    "tests.test_generation_chain_live_data",
    "tests.test_generation_chain_read_identity",
    "tests.test_generation_chain_anchor",
    "tests.test_generation_chain_completeness",
    "tests.test_generation_chain_classification",
    "tests.test_generation_chain_catalog",
    "tests.test_generation_chain_safety",
    "tests.test_generation_chain_liveness",
    "tests.test_generation_chain_identity",
    "tests.test_generation_chain_guards",
    "tests.test_generation_chain_real_repository",
    "tests.test_generation_chain_corroboration",
    "tests.test_generation_chain_cli",
    "tests.test_generation_chain_transports",
    "tests.test_generation_chain_monotonicity",
    "tests.test_lucene_segments",
    "tests.test_generation_chain_lucene_commit",
    "tests.test_reclaim_manifest",
    "tests.test_reclaim_approval",
    "tests.test_reclaim_batch",
    "tests.test_reclaim_checksum",
    "tests.test_reclaim_safety",
    "tests.test_reclaim_cli",
    "tests.test_reclaim_recheck",
    "tests.test_security_findings",
]

# (name, file, exact text to find, what to replace it with).
# Each replacement disables ONE decision and leaves the program runnable, so a
# red result means a test saw the behaviour change rather than saw a crash.
CASES = [
    # -- the live set, and the single subtraction -------------------------
    ("the-shard-local-set-difference", "derivation/shards.py",
     "return frozenset(self.present_blobs - self.live_blobs)",
     "return frozenset(self.present_blobs)"),
    ("segments-measured-against-the-live-set", "derivation/garbage.py",
     "for blob in sorted(named & history.collectable):",
     "for blob in sorted(named & history.present_blobs):"),
    ("the-contradiction-tripwire", "derivation/classification.py",
     "            contradicted.append(key)",
     "            pass"),
    ("refuse-a-run-that-contradicts-itself", "derivation/classification.py",
     "    if contradicted:\n        raise RunRefused(",
     "    if False:\n        raise RunRefused("),

    # -- anchoring ---------------------------------------------------------
    ("anchor-on-the-highest-generation-listed", "derivation/chain.py",
     "    for number in sorted((n for n in present if n > latest), reverse=True):",
     "    for number in []:"),
    ("refuse-when-a-generation-above-the-anchor-will-not-read",
     "derivation/chain.py",
     "        except GenerationChainError as exc:\n"
     "            raise RunRefused(\n"
     "                f\"{key} is listed above the generation {INDEX_LATEST_KEY} \"\n"
     "                f\"names and could not be read ({exc}), so this run cannot tell \"\n"
     "                \"whether it is this repository's current generation\") from exc",
     "        except GenerationChainError:\n            continue"),
    ("a-generation-above-the-anchor-must-carry-our-uuid",
     "derivation/chain.py",
     "        if parsed.repository_uuid != repository_uuid:\n"
     "            rejected[number] = (\n"
     "                f\"belongs to repository {parsed.repository_uuid}, not \"\n"
     "                f\"{repository_uuid}\")\n"
     "            continue",
     "        if False:\n            pass"),
    ("an-anchor-naming-no-snapshots-refuses", "derivation/chain.py",
     "    if not anchor.snapshots:", "    if False:"),
    ("an-older-generation-must-carry-our-uuid", "derivation/chain.py",
     "            elif parsed.repository_uuid != repository_uuid:",
     "            elif False:"),
    ("a-mixed-transition-is-not-one-operation", "derivation/chain.py",
     "        return [pair for pair, mixed in self._steps() if not mixed]",
     "        return [pair for pair, mixed in self._steps()]"),

    # -- read identity -----------------------------------------------------
    ("a-document-naming-no-blob-must-raise", "derivation/identity.py",
     "    if not document.blob_names:\n        raise ShapeGateError(",
     "    if False:\n        raise ShapeGateError("),
    ("the-raise-is-wired-into-every-read", "derivation/shards.py",
     "        require_blob_names(document, key)", "        pass"),
    ("a-document-may-not-name-blobs-from-elsewhere", "derivation/identity.py",
     "    stray = document.blob_names - set(listed_stems)\n    if stray:",
     "    stray = document.blob_names - set(listed_stems)\n    if False:"),
    ("a-document-needs-a-witness-unique-to-this-directory",
     "derivation/identity.py",
     "    if not witnesses:\n        return Doubt(",
     "    if False:\n        return Doubt("),
    ("the-store-confirms-the-witness-the-listing-suggested",
     "derivation/identity.py",
     "    if not any(keys.objects_for(f\"{directory}/{stem}\") for stem in witnesses):",
     "    if False:"),
    ("the-snapshot-name-set-must-match-the-catalog",
     "derivation/identity.py",
     "    found = set(document.by_snapshot_name)\n    if found == expected:",
     "    found = set(document.by_snapshot_name)\n    if True:"),
    ("a-writer-uuid-under-two-directories-drops-both",
     "derivation/identity.py",
     "        if len(directories) > 1:", "        if False:"),

    # -- the shard survey --------------------------------------------------
    ("a-live-snapshot-here-must-be-in-the-current-file-list",
     "derivation/shards.py",
     "    if unaccounted:", "    if False:"),
    ("the-catalog-s-two-halves-must-agree", "formats/repository_data.py",
     "            if index_uuid not in indices:", "            if False:"),
    # -- traversal completeness -------------------------------------------
    ("a-snapshot-must-account-for-its-declared-indices",
     "derivation/shards.py",
     "    absent = set(extent.index_names) - read_names\n    if absent:",
     "    absent = set(extent.index_names) - read_names\n    if False:"),
    ("a-snapshot-must-account-for-its-per-index-shard-count",
     "derivation/shards.py",
     "        if len(read) != declared.shard_count:", "        if False:"),
    ("a-snapshot-must-account-for-its-total-shard-count",
     "derivation/shards.py",
     "    if extent.total_shards is not None and total_read != extent.total_shards:",
     "    if False:"),
    ("a-snapshot-must-account-for-its-declared-size",
     "derivation/shards.py",
     "            if total != declared.size_in_bytes:", "            if False:"),
    ("an-undeclared-shard-count-is-not-a-complete-traversal",
     "derivation/shards.py",
     "        if declared is None:\n            # No shard count declared for this index, so there is nothing to\n"
     "            # check the traversal against. An absent declaration is not a\n"
     "            # statement that the traversal was complete.\n"
     "            _drop_indices(histories, dropped, {index_uuid}, Doubt(",
     "        if declared is None:\n            continue\n        if False:\n"
     "            _drop_indices(histories, dropped, {index_uuid}, Doubt("),
    ("an-unverified-extent-drops-the-snapshots-shards",
     "derivation/shards.py",
     "            _drop_indices(histories, dropped, touched, Doubt(\n"
     "                EXTENT_UNREADABLE,",
     "            _keep = (histories, dropped, touched, Doubt(\n"
     "                EXTENT_UNREADABLE,"),
    ("a-partial-snapshot-is-not-measured-against-its-extent",
     "derivation/shards.py",
     "        if not extent.is_complete:", "        if False:"),

    # -- attribution -------------------------------------------------------
    ("index-metadata-needs-a-complete-live-set", "derivation/garbage.py",
     "                    f\"{lookup!r} that the current generation does not map, so \"\n"
     "                    \"no index metadata blob was considered\")\n                return None",
     "                    f\"{lookup!r} that the current generation does not map, so \"\n"
     "                    \"no index metadata blob was considered\")\n                continue"),
    ("index-metadata-is-skipped-when-the-live-set-is-void",
     "derivation/garbage.py",
     "    if live is None:\n        return", "    if live is None:\n        live = {}"),
    ("a-reused-snapshot-name-attributes-no-file-list",
     "derivation/garbage.py",
     "        if operation.snapshot_name in surviving_names:",
     "        if False:"),
    ("a-snapshot-still-in-the-catalog-is-never-condemned",
     "derivation/garbage.py",
     "        if operation.snapshot_uuid in final.snapshots:\n            continue",
     "        if False:\n            continue"),
    ("a-dropped-shard-contributes-no-index-metadata",
     "derivation/garbage.py",
     "        if index_uuid in unread_indices:\n            continue",
     "        if False:\n            continue"),

    # -- the listing and the store's second opinion ------------------------
    ("a-key-is-confirmed-before-the-manifest-names-it",
     "derivation/keys.py",
     "        return sorted(c for c in candidates if self.confirm(c) == CONFIRMED)",
     "        return sorted(candidates)"),
    ("a-document-key-is-confirmed-before-it-is-named",
     "derivation/keys.py",
     "        return key in self._keys and self.confirm(key) == CONFIRMED",
     "        return key in self._keys"),
    ("a-store-that-could-not-answer-did-not-say-no",
     "derivation/keys.py",
     "            return UNANSWERED", "            return DENIED"),

    # -- the security findings, each checked against this runtime ----------
    ("entity-expansion-is-refused-before-parsing", "sources/s3.py",
     '    if _DOCTYPE in body[:2048].lstrip():', "    if False:"),
    ("the-delete-response-refuses-a-doctype-too", "reclaim/batch.py",
     '    if b"<!DOCTYPE" in body[:2048].lstrip():', "    if False:"),
    ("plain-http-off-loopback-is-refused", "sources/s3.py",
     "    if parsed.scheme == \"https\" or allowed:\n        return",
     "    if True:\n        return"),

    # -- the veto has to hold at the moment of deletion --------------------
    ("a-stale-manifest-is-refused", "reclaim/recheck.py",
     "    if age_seconds <= maximum:", "    if True:"),
    ("a-newly-mounted-index-stops-the-run", "reclaim/recheck.py",
     "    return tuple(key for key in keys if key.startswith(prefixes))",
     "    return ()"),
    ("execute-must-say-whether-it-re-checked", "reclaim/recheck.py",
     "    if not elasticsearch and not without:", "    if False:"),

    # -- the promise that this package cannot delete ----------------------
    ("the-transport-refuses-a-write-method", "sources/http_reads.py",
     "        if method not in ALLOWED_METHODS:", "        if False:"),

    # -- the Elasticsearch veto -------------------------------------------
    ("the-veto-subtracts-from-the-manifest",
     "derivation/classification.py",
     "    protected = {key for key in orphans\n"
     "                 if veto is not None and veto.covers(orphans[key])}",
     "    protected = set()"),

    # -- the Lucene commit cross-check (issue #1) -------------------------
    ("a-file-list-must-cover-what-the-commit-requires",
     "formats/shard_snapshots.py",
     "    if missing:", "    if False:"),
    ("the-commit-oracle-tally-is-recorded-per-document",
     "derivation/shards.py",
     "        tally.record(document)", "        pass"),

    # -- reclaim: a manifest cut off part way through a write is refused ---
    ("a-manifest-without-a-trailing-newline-is-refused",
     "reclaim/manifest.py",
     "    if not raw.endswith(b\"\\n\"):",
     "    if False:"),
    ("a-manifest-row-with-the-wrong-field-count-is-refused",
     "reclaim/manifest.py",
     "        if len(fields) != len(MANIFEST_COLUMNS):",
     "        if False:"),
    ("a-manifest-without-the-completion-marker-is-refused",
     "reclaim/manifest.py",
     "    if not rest or rest[-1] != _MARKER_LINE:",
     "    if False:"),

    # -- reclaim: approval is about this exact manifest, not a category ---
    ("approval-checks-the-manifest-digest",
     "reclaim/approval.py",
     "    if len(given) != MIN_DIGEST_LENGTH or given != manifest.digest:",
     "    if False:"),
    ("approval-checks-the-row-count",
     "reclaim/approval.py",
     "    if approve_rows != actual_rows:",
     "    if False:"),

    # -- reclaim: a batch answering 200 is read one key at a time ---------
    ("a-per-key-error-is-never-read-as-deleted",
     "reclaim/batch.py",
     "                failed.append((key, code, message))",
     "                deleted.append(key)"),
    ("a-key-absent-from-the-response-is-never-deleted",
     "reclaim/batch.py",
     "    unconfirmed = tuple(key for key in requested if key not in accounted)",
     "    unconfirmed = ()"),

    # -- reclaim: the command line itself, not just the modules under it --
    ("dry-run-is-the-default-and-sends-nothing",
     "reclaim/cli.py",
     "    if not args.execute:",
     "    if False:"),
    ("execute-without-approval-is-refused",
     "reclaim/cli.py",
     "        if args.approve_digest is None or args.approve_rows is None:",
     "        if False:"),
    ("cli-reports-a-per-key-failure-as-failed",
     "reclaim/cli.py",
     "            failed.extend(outcome.failed)",
     "            deleted.extend(outcome.failed)"),
    ("the-checksum-is-computed-over-the-body-actually-sent",
     "reclaim/cli.py",
     "            body = batch.build_request_body(keys)\n"
     "            try:\n"
     "                header = checksum_header(args.checksum_algorithm, body)",
     "            body = batch.build_request_body(keys)\n"
     "            try:\n"
     "                header = checksum_header(\n"
     "                    args.checksum_algorithm, batch.build_request_body(keys))"),

    # -- reclaim: the audit path still cannot see the deleter --------------
    ("the-audit-path-never-imports-reclaim",
     "derivation/audit.py",
     "from .keys import KeyIndex",
     "from .keys import KeyIndex\n"
     "from ..reclaim import cli as _reclaim_cli_leak_check"),
]


def run_case(name: str, relative: str, find: str, replace: str) -> tuple:
    tree = tempfile.mkdtemp(prefix="genchain-neuter-")
    try:
        shutil.copytree(os.path.join(ROOT, "generation_chain"),
                        os.path.join(tree, "generation_chain"))
        shutil.copytree(os.path.join(ROOT, "tests"),
                        os.path.join(tree, "tests"))
        target = os.path.join(tree, "generation_chain", relative)
        with open(target, encoding="utf-8") as handle:
            source = handle.read()
        if find not in source:
            return "STALE", f"the snippet is no longer in {relative}"
        if source.count(find) != 1:
            return "STALE", f"the snippet appears {source.count(find)} times"
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(source.replace(find, replace, 1))
        _drop_bytecode(tree)

        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = tree + os.pathsep + os.path.join(tree, "tests")
        environment["HOME"] = os.path.join(tree, "home")
        os.makedirs(environment["HOME"], exist_ok=True)
        finished = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "-f", "-q"] + MODULES,
            cwd=tree, env=environment, capture_output=True, text=True,
            timeout=2400)
        if finished.returncode == 0:
            return "GREEN", "no test noticed"
        return "RED", _first_failure(finished.stderr + finished.stdout)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def _first_failure(output: str) -> str:
    for line in output.splitlines():
        if line.startswith(("FAIL:", "ERROR:")):
            return line.split(" ", 1)[1].split(" ")[0]
    return "the suite failed"


def _drop_bytecode(tree: str) -> None:
    for directory, names, _ in os.walk(tree):
        for name in list(names):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(directory, name), ignore_errors=True)


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [c for c in CASES if wanted is None or c[0] == wanted]
    if not cases:
        print(f"no case named {wanted}")
        return 2
    unpinned, stale = [], []
    for name, relative, find, replace in cases:
        verdict, detail = run_case(name, relative, find, replace)
        print(f"{verdict:6s} {name:58s} {detail}", flush=True)
        if verdict == "GREEN":
            unpinned.append(name)
        elif verdict == "STALE":
            stale.append(name)
    print()
    print(f"{len(cases)} guard(s) neutered, {len(unpinned)} unpinned, "
          f"{len(stale)} stale")
    for name in unpinned:
        print(f"  UNPINNED: {name}")
    for name in stale:
        print(f"  STALE: {name}")
    return 1 if unpinned or stale else 0


if __name__ == "__main__":
    sys.exit(main())
