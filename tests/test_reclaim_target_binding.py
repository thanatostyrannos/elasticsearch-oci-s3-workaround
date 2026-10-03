"""An approved manifest deletes only from the repository it was derived from.

An approval binds the manifest's bytes. Before this, nothing tied those bytes
to a store: `--endpoint`, `--bucket` and `--prefix` came from the command line,
and reclaim never read the target before the first delete. A DR replica, a
migration copy or an `rclone` backup shares every key with the original, so a
manifest approved against one and executed against the other deleted the
copy's older restore points and exited 0.

The age guard had the same hole. It read the file's mtime, so a plain `cp` of
a week-old manifest looked a second old.

These tests run the real audit command to derive a manifest, then the real
reclaim command against `tests/s3rig.py` serving a repository built by
`tests/genchain_repo.py`, so the derivation record they check is the one the
audit actually writes.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
import s3rig
from generation_chain import cli as audit_cli
from generation_chain.reclaim import cli
from generation_chain.reclaim.manifest import load_manifest
from test_refuse_redirects import _Server

# s1 is deleted by the step into generation 2, so a manifest derived at
# generation 2 condemns s1's segment `__i1` and its root documents.
HISTORY = [
    {"s1": {"i": {0: ["__i1"]}}},
    {"s1": {"i": {0: ["__i1"]}}, "s2": {"i": {0: ["__i2"]}}},
    {"s2": {"i": {0: ["__i2"]}}},
]
TWO_HOURS = 2 * 3600


class TargetBindingCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reclaim-binding-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.original = os.path.join(self.dir, "original")
        repo.build(self.original, HISTORY)
        self.manifest_path = os.path.join(self.dir, "orphans.tsv")
        self.credentials_path = os.path.join(self.dir, "creds.json")
        with open(self.credentials_path, "w", encoding="utf-8") as handle:
            json.dump({"s3": {"access_key_id": s3rig.TEST_ACCESS_KEY,
                              "secret_access_key": s3rig.TEST_SECRET_KEY}},
                      handle)
        os.chmod(self.credentials_path, 0o600)

    def derive(self, clock=None):
        """Run the audit on the original repository, as an operator would."""
        argv = ["--local-repo", self.original, "--manifest",
                self.manifest_path, "--quiet"]
        if clock is None:
            code = audit_cli.main(argv, stdout=io.StringIO(),
                                  stderr=io.StringIO())
        else:
            with mock.patch("time.time", return_value=clock):
                code = audit_cli.main(argv, stdout=io.StringIO(),
                                      stderr=io.StringIO())
        self.assertEqual(code, audit_cli.EXIT_OK)
        self.assertGreater(len(load_manifest(self.manifest_path).keys), 0)

    def reclaim(self, rig, path=None, execute=True, *extra):
        path = path or self.manifest_path
        args = ["--manifest", path, "--endpoint", rig.endpoint,
                "--region", s3rig.TEST_REGION, "--bucket", rig.bucket,
                "--credentials", self.credentials_path]
        if execute:
            manifest = load_manifest(path)
            args += ["--execute", "--without-elasticsearch",
                     "--approve-digest", manifest.digest,
                     "--approve-rows", str(len(manifest.keys))]
        args += list(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli.main(args, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def copy_of(self, history, repository_uuid="repo-uuid-aaaa"):
        root = os.path.join(self.dir, "copy")
        return repo.build(root, history, repository_uuid=repository_uuid)


class AManifestExecutesAgainstItsOwnRepository(TargetBindingCase):

    def test_an_untouched_manifest_deletes_what_it_names(self):
        # Use case. If the target check ever refused the repository a
        # manifest came from, reclaim could delete nothing anywhere, and the
        # operator's only way forward would be a hand-rolled delete with no
        # gate at all.
        self.derive()
        names = load_manifest(self.manifest_path).keys
        with s3rig.S3Rig(self.original) as rig:
            code, stdout, stderr = self.reclaim(rig)
            remaining = rig.keys()
        self.assertEqual(code, cli.EXIT_OK, stderr)
        self.assertIn(f"deleted: {len(names)}", stdout)
        self.assertEqual(set(names) & remaining, set())

    def test_the_target_is_read_but_never_written_before_the_delete(self):
        # The binding check reads index.latest and one catalog. If it ever
        # sent anything other than GET, a refusal could still have changed
        # the store, and the refusal would be lying about "nothing sent".
        self.derive()
        with s3rig.S3Rig(self.original) as rig:
            self.reclaim(rig)
            methods = [request.method for request in rig.requests]
        first_post = methods.index("POST")
        self.assertEqual(set(methods[:first_post]), {"GET"})
        self.assertEqual(len(methods[:first_post]), 2)


class AManifestRefusesAnotherRepository(TargetBindingCase):

    def test_an_older_copy_with_the_same_uuid_is_refused(self):
        # Abuse case, reproduced in the issue: a DR copy taken while s1 was
        # still live shares the original's uuid and every key. Executing the
        # original's manifest there deleted s1's segment, a restore point the
        # copy exists to keep. The copy is at a lower generation than the
        # manifest's anchor, and that is what has to stop it.
        self.derive()
        copy = self.copy_of(HISTORY[:2])
        with s3rig.S3Rig(copy.root, bucket="dr-copy-bucket") as rig:
            code, _stdout, stderr = self.reclaim(rig)
            attempts = list(rig.batch_delete_attempts)
            remaining = rig.keys()
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED, stderr)
        self.assertEqual(attempts, [])
        self.assertTrue(copy.live_blob_keys <= remaining)
        self.assertIn("generation", stderr)

    def test_a_copy_with_a_different_uuid_is_refused(self):
        # Abuse case: a store at the same or a higher generation whose
        # catalog names a different repository. The keys still match, since
        # they are the same layout, and s1 is live there, so the only thing
        # standing between this manifest and live data is the uuid.
        self.derive()
        copy = self.copy_of(HISTORY[:2] + [HISTORY[1]],
                            repository_uuid="repo-uuid-bbbb")
        with s3rig.S3Rig(copy.root, bucket="other-bucket") as rig:
            code, _stdout, stderr = self.reclaim(rig)
            attempts = list(rig.batch_delete_attempts)
            remaining = rig.keys()
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED, stderr)
        self.assertEqual(attempts, [])
        self.assertTrue(copy.live_blob_keys <= remaining)
        self.assertIn("repo-uuid-bbbb", stderr)

    def test_an_unreadable_index_latest_is_refused(self):
        # Abuse case: a target that cannot say which repository it is must
        # not be assumed to be the right one. A wrong --prefix produces
        # exactly this, a 404 on index.latest, and it is the commonest way
        # an operator aims a manifest at the wrong place.
        self.derive()
        os.unlink(os.path.join(self.original, "index.latest"))
        with s3rig.S3Rig(self.original) as rig:
            code, _stdout, stderr = self.reclaim(rig)
            attempts = list(rig.batch_delete_attempts)
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED, stderr)
        self.assertEqual(attempts, [])
        self.assertIn("index.latest", stderr)


class ARedirectedTargetIsRefused(TargetBindingCase):

    def test_a_302_on_index_latest_refuses_and_is_not_followed(self):
        # Abuse case: a proxy or a hostile endpoint answers the identity read
        # with a redirect to a store that would pass the check. Following it
        # would let another host vouch for this target and would hand it the
        # signed request. Nothing may be deleted and the redirect target
        # must see nothing.
        self.derive()
        second = _Server()
        first = _Server(redirect_to=second.url + "/es-snapshots/index.latest")
        for server in (first, second):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        target = types.SimpleNamespace(endpoint=first.url,
                                       bucket=s3rig.TEST_BUCKET)
        code, _stdout, stderr = self.reclaim(target)
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED, stderr)
        self.assertEqual([seen[0] for seen in first.seen], ["GET"])
        self.assertEqual(second.seen, [])
        self.assertIn("302", stderr)


class AgeComesFromTheDerivationRecord(TargetBindingCase):

    def test_a_copied_old_manifest_is_refused(self):
        # Abuse case, reproduced in the issue: `cp` gives an old manifest a
        # fresh mtime, and an mtime-based age check then calls it a second
        # old. Under --without-elasticsearch the age check is the only guard
        # against a searchable snapshot mounted since the derivation.
        self.derive(clock=time.time() - TWO_HOURS)
        copied = os.path.join(self.dir, "copied.tsv")
        shutil.copy(self.manifest_path, copied)
        with s3rig.S3Rig(self.original) as rig:
            code, _stdout, stderr = self.reclaim(rig, copied)
            attempts = list(rig.batch_delete_attempts)
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED, stderr)
        self.assertEqual(attempts, [])
        self.assertIn("derive", stderr.lower())

    def test_an_old_mtime_on_a_fresh_manifest_does_not_refuse(self):
        # Use case paired with the one above. A manifest moved with
        # `cp -p`, `rsync -t` or an artifact download keeps or gains an old
        # mtime. If mtime still decided the age, those fresh manifests would
        # refuse and the copied stale one above would pass.
        self.derive()
        two_days_ago = time.time() - 2 * 86400
        os.utime(self.manifest_path, (two_days_ago, two_days_ago))
        with s3rig.S3Rig(self.original) as rig:
            code, _stdout, stderr = self.reclaim(rig)
        self.assertEqual(code, cli.EXIT_OK, stderr)


class AManifestWithoutADerivationRecordIsRefused(TargetBindingCase):

    def strip_record(self):
        """Rewrite the last line as the bare marker older audits wrote."""
        with open(self.manifest_path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        lines[-1] = "# derivation complete"
        with open(self.manifest_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_execute_refuses_a_bare_marker(self):
        # Abuse case: a manifest written before the derivation record
        # existed names no repository and no time, so neither the target nor
        # the age can be checked. Executing it would reopen both holes for
        # every manifest already on disk.
        self.derive()
        self.strip_record()
        with s3rig.S3Rig(self.original) as rig:
            code, _stdout, stderr = self.reclaim(rig)
            attempts = list(rig.batch_delete_attempts)
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED, stderr)
        self.assertEqual(attempts, [])
        self.assertIn("derive it again", stderr.lower())

    def test_the_dry_run_says_execute_will_refuse_it(self):
        # An operator reads the dry run before approving. If it printed an
        # approval for a manifest --execute then refuses, without saying
        # why, they would chase the approval values instead of re-deriving.
        self.derive()
        self.strip_record()
        with s3rig.S3Rig(self.original) as rig:
            code, _stdout, stderr = self.reclaim(rig, None, False)
            requests = list(rig.requests)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(requests, [])
        self.assertIn("derive it again", stderr.lower())


if __name__ == "__main__":
    unittest.main()
