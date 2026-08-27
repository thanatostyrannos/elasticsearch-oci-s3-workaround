"""The documentation may not describe the audit as sending a POST.

This claim was written wrong in eight files, corrected, found again in two
more, corrected, and found again in four more. Each round it was fixed by
grepping for the exact phrasing that happened to be in front of me, and each
round a different wording survived: "the POST that lists a bucket", "the one
POST that lists a bucket", "the one bucket-listing POST", "one listing POST".

The claim matters because it is about the safety property the whole tool rests
on, and it was wrong in the direction that understates it. The transport
permits GET and HEAD. Listing is a GET with list-type=2 in the query string.
POST and DELETE are unreachable, refused by a raised exception.

So the rule is enforced here rather than remembered: no document may put the
words POST and listing near each other while talking about the audit. The
delete tool genuinely does send one POST per batch, and saying so is correct,
which is why this looks for the listing claim rather than for POST alone.
"""
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The four wordings this claim has appeared in, and nothing else. POST is
# matched case sensitively so "post-sweep" and "post-install" do not count,
# and the shapes are explicit rather than proximity based so that a correct
# sentence like "listing a bucket is a GET, so POST and DELETE are
# unreachable" does not trip it.
CLAIM = re.compile(
    r"\bPOST\b\s+(?:that\s+)?lists?\b"          # "POST that lists a bucket"
    r"|\b(?:bucket[- ])?listing\s+POST\b"         # "bucket-listing POST", "listing POST"
    r"|\bPOST\b[^.\n]{0,20}\blists\s+a\s+bucket\b")


def tracked(*patterns):
    out = subprocess.run(["git", "ls-files", *patterns],
                         cwd=ROOT, capture_output=True, text=True).stdout
    return [ROOT / line for line in out.split() if line]


class TheAuditIsNeverDescribedAsSendingAPost(unittest.TestCase):

    def test_no_document_says_the_audit_lists_with_a_post(self):
        offenders = []
        for path in tracked("*.md", "*.yml", "*.yaml"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.split("\n"), 1):
                # The test's own explanation quotes the wrong wording on
                # purpose, and so does the architecture note that records the
                # correction. Both name themselves here rather than being
                # matched loosely.
                rel = path.relative_to(ROOT).as_posix()
                if rel in ("docs/engineering/architecture.md",):
                    continue
                if CLAIM.search(line):
                    offenders.append(f"{rel}:{number}: {line.strip()[:90]}")
        self.assertEqual(offenders, [], "\n".join(
            ["the audit sends GET and HEAD only; listing is a GET:"] + offenders))

    def test_the_transport_still_permits_only_get_and_head(self):
        # If this ever changes, the documentation rule above changes with it,
        # and this test is where someone will find that out.
        source = (ROOT / "generation_chain" / "sources" / "http_reads.py").read_text()
        self.assertIn('ALLOWED_METHODS = frozenset({"GET", "HEAD"})', source)


if __name__ == "__main__":
    unittest.main()
