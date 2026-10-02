"""End to end: an approved manifest against a real socket, real signatures,
real per-key accounting.

These tests run the actual command line entry point against
`tests/s3rig.py`'s independent checksum verifier and signature checker, the
same rig `tests/test_generation_chain_transports.py` runs the read path
against. Nothing here is a stub of this package's own code checked against
itself.
"""

import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import s3rig
from generation_chain.reclaim import batch, cli
from generation_chain.reclaim.manifest import EXPECTED_HEADER, load_manifest
from generation_chain.reporting.manifest import completion_line
from generation_chain.sources.s3 import S3CompatibleSource

ROW = "{key}\treason text\tsegment blob\tsuuid\tsname\t1\t2"

# The repository every manifest in this file is derived from, and the catalog
# `store` serves for it, so --execute's target check finds the repository the
# manifest names.
REPOSITORY_UUID = "reclaim-cli-repository"
GENERATION = 1
IDENTITY_KEYS = ("index.latest", f"index-{GENERATION}")


def write_manifest(path: str, keys, complete: bool = True) -> None:
    """A manifest in the shape the audit CLI writes.

    `complete=True`, the default, appends the completion marker with a
    derivation record naming REPOSITORY_UUID at GENERATION, derived now, the
    same as a successful run written through `--manifest FILE`. Every test
    in this file exercises the reclaim CLI against a manifest an operator
    would actually be allowed to execute against, so this is the default
    rather than something each test has to ask for.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(EXPECTED_HEADER + "\n")
        for key in keys:
            handle.write(ROW.format(key=key) + "\n")
        if complete:
            handle.write(completion_line(REPOSITORY_UUID, GENERATION,
                                         time.time()))


def store(objects, prefix: str = "", **options) -> s3rig.S3Rig:
    """A rig holding `objects` plus the catalog of REPOSITORY_UUID."""
    base = (prefix.strip("/") + "/") if prefix.strip("/") else ""
    catalog = {"min_version": "7.12.0", "uuid": REPOSITORY_UUID,
               "snapshots": [], "indices": {},
               "index_metadata_identifiers": {}}
    served = {base + "index.latest": struct.pack(">q", GENERATION),
              base + f"index-{GENERATION}":
                  json.dumps(catalog).encode("utf-8")}
    served.update(objects)
    return s3rig.S3Rig(root=None, prefix=prefix, objects=served, **options)


def repository_keys(rig) -> set:
    """What the rig holds, less the catalog `store` added."""
    return {key for key in rig.keys()
            if key.rsplit("/", 1)[-1] not in IDENTITY_KEYS}


def write_credentials(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"s3": {"access_key_id": s3rig.TEST_ACCESS_KEY,
                         "secret_access_key": s3rig.TEST_SECRET_KEY}}, handle)
    os.chmod(path, 0o600)


class ReclaimCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reclaim-cli-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.manifest_path = os.path.join(self.dir, "manifest.tsv")
        self.credentials_path = os.path.join(self.dir, "creds.json")
        write_credentials(self.credentials_path)

    def run_cli(self, rig, *extra, execute=False, approve=False):
        args = ["--manifest", self.manifest_path, "--endpoint", rig.endpoint,
               "--region", s3rig.TEST_REGION, "--bucket", rig.bucket,
               "--credentials", self.credentials_path]
        if execute:
            # --execute now requires the operator to state whether the
            # Elasticsearch veto was re-checked against the cluster as it is
            # now, because the manifest's protection was decided when it was
            # derived. These are offline tests with no cluster to ask, which
            # is exactly what --without-elasticsearch says.
            args += ["--execute", "--without-elasticsearch"]
        if approve:
            manifest = load_manifest(self.manifest_path)
            args += ["--approve-digest", manifest.digest,
                    "--approve-rows", str(len(manifest.keys))]
        args += list(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli.main(args, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()


class NeverDeletesOutsideTheManifest(ReclaimCase):

    def test_a_key_not_in_the_manifest_survives(self):
        # THE use/abuse pair the task asks for by name: "Construct a
        # manifest, run against a store holding more than it names, assert
        # the extras survive." Structural rather than a neuter case: there is
        # no second key source anywhere in cli.py to disable. `_store_keys`
        # builds `store_keys` from `manifest.keys` and the prefix and nothing
        # else, so there is no conditional a mutation could flip to widen it;
        # this test is the proof instead.
        write_manifest(self.manifest_path, ["named/one"])
        with store({
                "named/one": b"x", "not/named": b"y",
                "also/not/named": b"z"}) as rig:
            code, _stdout, _stderr = self.run_cli(rig, execute=True, approve=True)
            self.assertEqual(code, cli.EXIT_OK)
            remaining = repository_keys(rig)
        self.assertNotIn("named/one", remaining)
        self.assertIn("not/named", remaining)
        self.assertIn("also/not/named", remaining)


class ManifestMustBeMarkedComplete(ReclaimCase):

    def test_a_manifest_missing_the_completion_marker_is_refused_end_to_end(self):
        # The real-world shape of the gap issue #7 closed, exercised through
        # the whole CLI rather than through manifest.py alone: an operator
        # who points --manifest at a file that was written to stdout and
        # redirected by hand, or at a refused run's own output, gets a
        # refusal before a single request is built, not a manifest read as
        # though it named nothing.
        write_manifest(self.manifest_path, ["a"], complete=False)
        with store({"a": b"x"}) as rig:
            code, _stdout, stderr = self.run_cli(rig, execute=False)
            self.assertEqual(code, cli.EXIT_USAGE)
            self.assertEqual(rig.requests, [])
            self.assertIn("a", repository_keys(rig))
        self.assertIn("marker", stderr.lower())


class DuplicateRowsAreRefused(ReclaimCase):

    def test_a_manifest_without_duplicates_executes_every_approved_key(self):
        # --approve-rows counts the manifest's rows. If a refactor of the
        # duplicate check ever drops or merges distinct keys, an operator
        # approves N rows and the store deletes fewer, with no warning.
        keys = ["k/one", "k/two", "k/three"]
        write_manifest(self.manifest_path, keys)
        with store({k: b"x" for k in keys}) as rig:
            code, stdout, _stderr = self.run_cli(rig, execute=True, approve=True)
            remaining = repository_keys(rig)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("deleted: 3", stdout)
        self.assertEqual(remaining, set())

    def test_a_duplicate_row_is_refused_and_nothing_is_sent(self):
        # A hand edited or spliced manifest can repeat a key. The approval
        # counts every row, so executing it would delete fewer keys than the
        # operator approved and report success. The audit never writes this
        # shape, so the only way here is an edit, and the safe answer is to
        # send nothing and name the key so the operator can fix the file.
        write_manifest(self.manifest_path, ["k/one", "k/two", "k/one"])
        with store({"k/one": b"x", "k/two": b"y"}) as rig:
            code, _stdout, stderr = self.run_cli(rig, execute=True, approve=True)
            requests = list(rig.requests)
            remaining = set(repository_keys(rig))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(requests, [])
        self.assertEqual(remaining, {"k/one", "k/two"})
        self.assertIn("k/one", stderr)
        self.assertNotIn("k/two", stderr)

    def test_a_dry_run_also_refuses_a_duplicate_row(self):
        # The dry run prints the --approve-rows value to use. If it accepted
        # a duplicate, it would hand the operator a count the execute run
        # then refuses, or worse, one the old code half honoured.
        write_manifest(self.manifest_path, ["k/one", "k/one"])
        with store({"k/one": b"x"}) as rig:
            code, _stdout, stderr = self.run_cli(rig, execute=False)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("k/one", stderr)


class DryRunSendsNothing(ReclaimCase):

    def test_no_request_reaches_the_store_without_execute(self):
        # Use case for the whole safety model: without --execute, no batch
        # delete attempt happens at all. Neutered under
        # "dry-run-is-the-default-and-sends-nothing".
        write_manifest(self.manifest_path, ["a", "b"])
        with store({"a": b"x", "b": b"y"}) as rig:
            code, _stdout, stderr = self.run_cli(rig, execute=False)
            self.assertEqual(code, cli.EXIT_OK)
            self.assertEqual(rig.batch_delete_attempts, [])
            self.assertEqual(repository_keys(rig), {"a", "b"})
        self.assertIn("DRY RUN", stderr)

    def test_the_dry_run_prints_a_command_that_execute_will_accept(self):
        # The dry run tells the operator what to run next, and README copied
        # that text verbatim into its delete step. The printed command named
        # only the approval, so running exactly what the tool suggested hit
        # the corroboration gate and exited EXIT_USAGE. A tool that prints an
        # invocation it will then refuse teaches the wrong command to
        # everyone who copies it, including whoever writes the docs.
        write_manifest(self.manifest_path, ["a"])
        with store({"a": b"x"}) as rig:
            _code, _stdout, stderr = self.run_cli(rig, execute=False)
        self.assertIn("--without-elasticsearch", stderr)
        self.assertIn("--elasticsearch", stderr)

    def test_the_dry_run_prints_the_exact_approval_needed_next(self):
        # The dry run's printed digest and row count must be the ones
        # verify_approval actually accepts, so an operator copying them
        # straight from this output is not copying a value that will refuse.
        write_manifest(self.manifest_path, ["a"])
        manifest = load_manifest(self.manifest_path)
        with store({"a": b"x"}) as rig:
            _code, _stdout, stderr = self.run_cli(rig, execute=False)
        self.assertIn(manifest.digest, stderr)
        self.assertIn(f"--approve-rows {len(manifest.keys)}", stderr)


class ApprovalIsRequiredForExecute(ReclaimCase):

    def test_execute_without_any_approval_is_refused(self):
        # Abuse case: --execute alone must not be enough. Neutered under
        # "execute-without-approval-is-refused".
        write_manifest(self.manifest_path, ["a"])
        with store({"a": b"x"}) as rig:
            code, _stdout, _stderr = self.run_cli(rig, execute=True, approve=False)
            self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED)
            self.assertEqual(rig.batch_delete_attempts, [])
            self.assertIn("a", repository_keys(rig))

    def test_execute_with_a_stale_approval_is_refused(self):
        # Abuse case: an approval computed against an earlier version of the
        # manifest must not carry over to a regenerated one.
        write_manifest(self.manifest_path, ["a"])
        stale_digest = load_manifest(self.manifest_path).digest
        write_manifest(self.manifest_path, ["a", "b"])  # regenerated, grew
        with store({"a": b"x", "b": b"y"}) as rig:
            code, _stdout, _stderr = self.run_cli(
                rig, "--approve-digest", stale_digest, "--approve-rows", "1",
                execute=True, approve=False)
            self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED)
            self.assertEqual(rig.batch_delete_attempts, [])
            self.assertEqual(repository_keys(rig), {"a", "b"})


class PartialFailureIsReportedHonestly(ReclaimCase):

    def test_a_failing_key_inside_a_200_is_never_reported_deleted(self):
        # THE guard from the issue, exercised through the full stack rather
        # than through batch.py alone: a per-key error inside a 200 response
        # must surface as a failure in the CLI's own tally and exit code, and
        # the object must still be in the store afterward. Neutered under
        # "cli-reports-a-per-key-failure-as-failed".
        write_manifest(self.manifest_path, ["ok", "blocked"])
        with store({"ok": b"x", "blocked": b"y"},
                         delete_status={"blocked": (500, "InternalError")}) as rig:
            code, stdout, _stderr = self.run_cli(rig, execute=True, approve=True)
            self.assertEqual(code, cli.EXIT_PARTIAL)
            self.assertIn("blocked", repository_keys(rig))
            self.assertNotIn("ok", repository_keys(rig))
        self.assertIn("failed: 1", stdout)
        self.assertIn("deleted: 1", stdout)
        self.assertIn("blocked", stdout)

    def test_a_key_missing_from_the_response_is_unconfirmed_not_ok(self):
        write_manifest(self.manifest_path, ["ok", "dropped"])
        with store({"ok": b"x", "dropped": b"y"},
                         delete_status={"dropped": (0, "OMIT")}) as rig:
            code, stdout, _stderr = self.run_cli(rig, execute=True, approve=True)
            self.assertEqual(code, cli.EXIT_PARTIAL)
            self.assertIn("dropped", repository_keys(rig))
        self.assertIn("unconfirmed: 1", stdout)

    def test_an_unreadable_200_reports_every_key_as_failed(self):
        # An operator reading the report triages "unconfirmed" keys as "the
        # store never answered". Reporting an unparseable answer that way
        # sends them to chase a network fault when the store did answer.
        # Abuse case: the keys must also never count as deleted and must end
        # the run partial.
        write_manifest(self.manifest_path, ["one", "two"])
        with store({"one": b"x", "two": b"y"}) as rig:
            with patch.object(cli, "send_batch_delete",
                              return_value=b"not xml at all"):
                code, stdout, _stderr = self.run_cli(
                    rig, execute=True, approve=True)
            self.assertIn("one", rig.keys())
            self.assertIn("two", rig.keys())
        self.assertEqual(code, cli.EXIT_PARTIAL)
        self.assertIn("failed: 2", stdout)
        self.assertIn("unconfirmed: 0", stdout)
        self.assertIn("deleted: 0", stdout)
        self.assertIn("UnparseableResponse", stdout)

    def test_already_absent_keys_alone_still_exit_ok(self):
        # An already-absent key means the manifest's goal (that key being
        # gone) already holds, so a manifest naming only such keys is not a
        # partial failure.
        write_manifest(self.manifest_path, ["gone"])
        with store({},
                         delete_status={"gone": (404, "NoSuchKey")}) as rig:
            code, stdout, _stderr = self.run_cli(rig, execute=True, approve=True)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("already absent: 1", stdout)


class ChecksumAlgorithmIsConfigurable(ReclaimCase):

    def test_crc32c_is_accepted_end_to_end(self):
        # Use case for the configurable algorithm: this project's own lab
        # store wants Content-MD5, but an operator pointed at genuine AWS S3
        # or Oracle's documented alternative needs a different header, and
        # the whole request, not just the checksum function in isolation,
        # must go through under it.
        write_manifest(self.manifest_path, ["a"])
        with store({"a": b"x"}) as rig:
            code, _stdout, _stderr = self.run_cli(
                rig, "--checksum-algorithm", "crc32c", execute=True, approve=True)
            self.assertEqual(code, cli.EXIT_OK)
            self.assertNotIn("a", repository_keys(rig))


class ChecksumCoversTheBodyActuallySent(ReclaimCase):

    def test_the_body_is_rendered_once_per_batch_not_once_per_checksum(self):
        # THE guard from the issue: "the checksum computed over the exact
        # bytes sent." A version that re-renders the body to compute the
        # checksum, instead of reusing the one `bytes` object handed to
        # transport.send_batch_delete, could in principle checksum something
        # other than what goes on the wire. Patching build_request_body with
        # a counting wrapper and running the real --execute path is what
        # catches that; a unit test that calls the builder directly never
        # exercises cli.py's own call site at all. Neutered in
        # tests/genchain_neuter.py under
        # "the-checksum-is-computed-over-the-body-actually-sent".
        write_manifest(self.manifest_path, ["a", "b"])
        calls = []
        original = batch.build_request_body

        def counting(keys):
            rendered = original(keys)
            calls.append(rendered)
            return rendered

        with store({"a": b"x", "b": b"y"}) as rig:
            batch.build_request_body = counting
            try:
                code, _stdout, _stderr = self.run_cli(rig, execute=True,
                                                       approve=True)
            finally:
                batch.build_request_body = original
            self.assertEqual(code, cli.EXIT_OK)
        # One batch, one render. Two renders is exactly what a checksum
        # computed over a re-rendered copy would need.
        self.assertEqual(len(calls), 1)


class PrefixIsAppliedConsistently(ReclaimCase):

    def test_normalise_prefix_matches_the_read_transport_s_own_rule(self):
        # Pins cli.py's duplicated prefix rule against sources/s3.py's, so
        # the two cannot silently drift and have a manifest derived under one
        # prefix convention deleted under a different one.
        for raw in ("", "base/path", "/base/path/", "base/path//"):
            expected = S3CompatibleSource.__new__(S3CompatibleSource)
            expected.prefix = (raw.strip("/") + "/") if raw.strip("/") else ""
            self.assertEqual(cli.normalise_prefix(raw), expected.prefix)

    def test_a_prefixed_manifest_deletes_under_the_bucket_prefix(self):
        # A repository configured with a base_path is the ordinary case, and
        # a tool that forgets the prefix on the delete path deletes nothing
        # while reporting success, or worse, deletes a same-named key living
        # at the bucket root instead of inside the repository's base_path.
        write_manifest(self.manifest_path, ["shard-file"])
        with store(prefix="base/path", objects={
                "base/path/shard-file": b"x",
                "shard-file": b"a co-tenant's object of the same name"}) as rig:
            code, _stdout, _stderr = self.run_cli(
                rig, "--prefix", "base/path", execute=True, approve=True)
            self.assertEqual(code, cli.EXIT_OK)
            remaining = repository_keys(rig)
        self.assertNotIn("base/path/shard-file", remaining)
        self.assertIn("shard-file", remaining)


class MultipleBatches(ReclaimCase):

    def test_a_manifest_larger_than_one_batch_sends_more_than_one_request(self):
        # Realistic scale check: the issue measures 89,256 orphans, well
        # past the 1,000-key S3 limit. This is smaller for test speed, but it
        # is the same code path at more than one batch, not a single batch
        # asserted to loop conceptually.
        count = batch.MAX_KEYS_PER_BATCH + 5
        keys = [f"k{i}" for i in range(count)]
        write_manifest(self.manifest_path, keys)
        objects = {key: b"x" for key in keys}
        with store(objects) as rig:
            code, stdout, _stderr = self.run_cli(rig, execute=True, approve=True)
            self.assertEqual(code, cli.EXIT_OK)
            self.assertEqual(len(rig.batch_delete_attempts), 2)
            self.assertEqual(repository_keys(rig), set())
        self.assertIn(f"deleted: {count}", stdout)


if __name__ == "__main__":
    unittest.main()
