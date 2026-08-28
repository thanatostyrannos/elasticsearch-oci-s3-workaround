"""The doubles that stand in for a store get the same treatment as the store.

CodeQL raised four high findings on this repository and every one was in test
code. That is an easy thing to wave away, and two of them should not be.

A double that is more permissive than the thing it imitates lets a test pass
against behaviour the real code would refuse. And a test HTTP server that
serves a path built from a request line is a traversal whether or not it is
bound to loopback: the fix costs two lines and the argument for skipping it is
only ever "nobody would point it at anything".

The third finding, a credentials file written in a temporary directory, is
what the tool requires of an operator and is not fixed here.
"""
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TheOciDoubleDoesNotServeFilesOutsideItsRoot(unittest.TestCase):

    def test_a_key_full_of_dot_dot_cannot_escape_the_root(self):
        # The check the handler makes, exercised directly: resolve the joined
        # path and require it to still sit under the root.
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(os.path.join(directory, "bucket"))
            os.makedirs(root)
            secret = os.path.join(directory, "outside.txt")
            with open(secret, "w") as handle:
                handle.write("not yours")

            def resolves_inside(key):
                path = os.path.realpath(os.path.join(root, key))
                return os.path.commonpath([root, path]) == root

            self.assertTrue(resolves_inside("index-0"))
            self.assertTrue(resolves_inside("indices/abc/0/snap-x.dat"))
            self.assertFalse(resolves_inside("../outside.txt"))
            self.assertFalse(resolves_inside("../../etc/passwd"))
            self.assertFalse(resolves_inside("a/../../outside.txt"))

    def test_the_handler_carries_that_check(self):
        # Pinned by reading the source, because the traversal is only visible
        # in the line that joins the key to the root.
        body = (ROOT / "tests" / "test_generation_chain_transports.py").read_text()
        self.assertIn("os.path.commonpath", body,
                      "the OCI double joins a request key to a root without "
                      "checking the result stays under it")


class TheS3DoubleRefusesADoctypeLikeTheRealCodeDoes(unittest.TestCase):

    def test_a_delete_request_carrying_a_doctype_is_refused(self):
        import s3rig
        bomb = (b'<?xml version="1.0"?>\n'
                b'<!DOCTYPE lolz [<!ENTITY lol "lol">]>\n'
                b'<Delete><Object><Key>&lol;</Key></Object></Delete>')
        with self.assertRaises(ValueError) as caught:
            s3rig._parse_delete_request(bomb)
        self.assertIn("DOCTYPE", str(caught.exception))

    def test_an_ordinary_delete_request_still_parses(self):
        import s3rig
        body = (b'<Delete><Object><Key>a/b.dat</Key></Object>'
                b'<Object><Key>c.dat</Key></Object></Delete>')
        self.assertEqual(s3rig._parse_delete_request(body), ["a/b.dat", "c.dat"])


if __name__ == "__main__":
    unittest.main()
