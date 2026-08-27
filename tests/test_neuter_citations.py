"""Every "Neutered under" citation in this suite names a real case.

A comment claiming a guard was neutered and watched go red is part of the
safety argument for whatever it sits next to: a reader trusts that the case
exists and was run. A comment naming a case that was never added, renamed, or
mistyped is worse than no comment, because it makes an unpinned guard look
pinned. This test is the sweep that would have caught it: it extracts every
quoted case name cited after the word "Neutered" anywhere in tests/*.py and
checks it against genchain_neuter.py's own CASES list, the one source of
truth for what has actually been proven to go red.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_neuter

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# A case name is a lowercase, hyphenated, multi-word token. Three or more
# segments keeps this from matching a short quoted word that has nothing to
# do with a neuter case, such as an error code or a header name quoted for
# some other reason in the same file.
CITATION = re.compile(r'Neutered\b.*?"([a-z][a-z0-9]*(?:-[a-z0-9]+){2,})"')


def _comment_blocks(path: str):
    """Contiguous runs of `#` comment lines, each joined into one string.

    Joining is what lets "Neutered under" on one line and the quoted case
    name on the next be read as a single citation, the shape most of these
    comments actually take once a line-length limit wraps them.
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    current = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            current.append(stripped[1:].strip())
        else:
            if current:
                yield " ".join(current)
            current = []
    if current:
        yield " ".join(current)


def cited_case_names():
    """(file, case name) for every "Neutered ... "name"" citation found."""
    for name in sorted(os.listdir(TESTS_DIR)):
        if not name.startswith("test_") or not name.endswith(".py"):
            continue
        if name == os.path.basename(__file__):
            continue
        path = os.path.join(TESTS_DIR, name)
        for block in _comment_blocks(path):
            match = CITATION.search(block)
            if match:
                yield name, match.group(1)


class NeuterCitationsAreReal(unittest.TestCase):

    def test_every_cited_case_name_exists_in_genchain_neuter(self):
        # THE guard this test exists for: a citation naming a case that was
        # never added, was renamed, or was mistyped. Found by hand once
        # already, in a review that caught five comments across two files
        # citing case names with no matching entry in CASES, none of which
        # any other test in this suite would have noticed.
        real_names = {case[0] for case in genchain_neuter.CASES}
        cited = list(cited_case_names())
        self.assertTrue(cited, "the extraction found nothing to check, which "
                              "means either every citation vanished or the "
                              "pattern stopped matching; either way this "
                              "test is no longer checking anything")
        missing = [(path, name) for path, name in cited if name not in real_names]
        self.assertEqual(missing, [])

    def test_the_extraction_pattern_ignores_a_mention_with_no_quoted_name(self):
        # Abuse case for the extractor itself: "Neutered in
        # tests/genchain_neuter.py's existing case set would be redundant"
        # names no case at all, and must not be misread as citing one.
        block = ('Neutered in tests/genchain_neuter.py\'s existing case set '
                 "would be redundant with the assertion inside http_reads.py")
        self.assertIsNone(CITATION.search(block))

    def test_the_extraction_pattern_finds_a_real_citation(self):
        # Use case for the extractor: the exact shape a citation actually
        # takes in this suite, split across two comment lines and joined.
        block = ('Neutered under "a-key-absent-from-the-response-is-never-'
                 'deleted".')
        match = CITATION.search(block)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1),
                         "a-key-absent-from-the-response-is-never-deleted")


if __name__ == "__main__":
    unittest.main()
