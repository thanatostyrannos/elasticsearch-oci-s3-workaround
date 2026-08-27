"""Building one batch's request, and reading its response one key at a time.

The two halves of the issue's central claim live here: the checksum must
cover the exact bytes sent, and a batch answering 200 can still carry a
per-key failure that must never be read as a success.
"""

import base64
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.reclaim import batch, checksum


def delete_result(deleted=(), errors=()):
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
    for key in deleted:
        body.append(f"<Deleted><Key>{key}</Key></Deleted>")
    for key, code, message in errors:
        body.append(f"<Error><Key>{key}</Key><Code>{code}</Code>"
                    f"<Message>{message}</Message></Error>")
    body.append("</DeleteResult>")
    return "".join(body).encode("utf-8")


class Chunks(unittest.TestCase):

    def test_splits_at_the_s3_batch_limit(self):
        # Use case. 2,500 keys must become three requests of 1000, 1000, 500,
        # matching the S3 DeleteObjects limit named in the issue.
        keys = [f"k{i}" for i in range(2500)]
        pieces = list(batch.chunks(keys))
        self.assertEqual([len(p) for p in pieces], [1000, 1000, 500])
        self.assertEqual([k for piece in pieces for k in piece], keys)

    def test_a_manifest_smaller_than_one_batch_is_one_batch(self):
        keys = ["a", "b", "c"]
        self.assertEqual(list(batch.chunks(keys)), [["a", "b", "c"]])

    def test_zero_keys_produce_zero_batches(self):
        # Abuse case: a clean-repository manifest must not manufacture an
        # empty batch and send an empty-but-real request for nothing.
        self.assertEqual(list(batch.chunks([])), [])


class BuildRequestBody(unittest.TestCase):

    def test_contains_exactly_the_keys_given_and_nothing_else(self):
        # Use case, and the guard the task states outright: "it must not
        # derive, re-derive, expand, glob, or infer a single key." A batch
        # built from three keys must name those three keys and no others.
        body = batch.build_request_body(["a/b", "c/d", "e/f"])
        outcome = batch.parse_response(
            delete_result(deleted=["a/b", "c/d", "e/f"]), ["a/b", "c/d", "e/f"])
        self.assertEqual(set(outcome.deleted), {"a/b", "c/d", "e/f"})
        self.assertIn(b"<Key>a/b</Key>", body)
        self.assertIn(b"<Key>c/d</Key>", body)
        self.assertIn(b"<Key>e/f</Key>", body)
        self.assertEqual(body.count(b"<Object>"), 3)

    def test_is_never_quiet(self):
        # Use case: a quiet response omits Deleted entries, and this package
        # exists to read every key's own outcome, so the request it sends
        # must always ask for the full accounting.
        body = batch.build_request_body(["a"])
        self.assertIn(b"<Quiet>false</Quiet>", body)

    def test_a_key_with_xml_special_characters_round_trips(self):
        # Abuse case for the render step: a key containing `&`, `<` or `>`
        # must be escaped on the way out or the request itself is malformed
        # XML, which some stores parse permissively and others simply reject,
        # neither of which names the key that was actually asked for.
        key = "weird&key<with>chars"
        body = batch.build_request_body([key])
        self.assertNotIn(b"<with>", body)  # would mean it was not escaped
        self.assertIn(b"weird&amp;key&lt;with&gt;chars", body)

    def test_the_checksum_covers_the_exact_bytes_the_body_builder_returned(self):
        # Unit-level sanity check on build_request_body alone: calling it
        # once and checksumming what it returned matches hashlib over that
        # same object. This does NOT exercise cli.py's own call site, so it
        # cannot notice a future change there that re-renders the body to
        # compute the checksum instead of reusing the one `bytes` object
        # handed to the transport; that guard is pinned end to end in
        # tests/test_reclaim_cli.py's
        # ChecksumCoversTheBodyActuallySent, which patches this exact
        # function and counts calls through the real --execute path.
        calls = []
        original = batch.build_request_body

        def counting(keys):
            rendered = original(keys)
            calls.append(rendered)
            return rendered

        body = counting(["a", "b", "c"])
        self.assertEqual(len(calls), 1)
        header, value = checksum.checksum_header(checksum.MD5, body)
        self.assertEqual(base64.b64decode(value), hashlib.md5(body).digest())
        # A second render of the SAME keys, standing in for "what a re-render
        # bug would checksum instead": ElementTree attribute order and text
        # content are deterministic here, so this asserts the two are equal
        # bytes, which is what makes reusing one object versus re-rendering
        # unobservable by accident, and why the call-count assertion above,
        # not a bytes comparison, is the guard that actually pins this.
        again = original(["a", "b", "c"])
        self.assertEqual(body, again)


class ParseResponse(unittest.TestCase):

    def test_a_per_key_error_inside_a_200_is_reported_as_failed(self):
        # THE central guard from the issue: "a key reported as an error has
        # NOT been deleted whatever the HTTP status code was." Neutered under
        # "a-per-key-error-is-never-read-as-deleted".
        outcome = batch.parse_response(
            delete_result(deleted=["ok"],
                         errors=[("bad", "AccessDenied", "no")]),
            ["ok", "bad"])
        self.assertEqual(outcome.deleted, ("ok",))
        self.assertEqual(outcome.failed, (("bad", "AccessDenied", "no"),))
        self.assertNotIn("bad", outcome.deleted)

    def test_a_not_found_error_is_already_absent_not_failed(self):
        # Use case for the third category the task asks for by name.
        outcome = batch.parse_response(
            delete_result(errors=[("gone", "NoSuchKey", "no such key")]),
            ["gone"])
        self.assertEqual(outcome.already_absent,
                         (("gone", "NoSuchKey", "no such key"),))
        self.assertEqual(outcome.failed, ())
        self.assertEqual(outcome.deleted, ())

    def test_a_key_missing_from_the_response_is_unconfirmed_not_deleted(self):
        # Abuse case for a store that answers a batch it did not fully
        # honour: a requested key absent from both <Deleted> and <Error> must
        # never be treated as a success. Neutered under
        # "a-key-absent-from-the-response-is-never-deleted".
        outcome = batch.parse_response(
            delete_result(deleted=["present"]), ["present", "silently-dropped"])
        self.assertEqual(outcome.deleted, ("present",))
        self.assertEqual(outcome.unconfirmed, ("silently-dropped",))

    def test_unparseable_xml_raises_rather_than_reporting_partial_success(self):
        with self.assertRaises(batch.BatchDeleteError):
            batch.parse_response(b"not xml at all", ["a"])

    def test_the_wrong_root_element_raises(self):
        with self.assertRaises(batch.BatchDeleteError):
            batch.parse_response(b"<SomethingElse/>", ["a"])


if __name__ == "__main__":
    unittest.main()
