"""Every neuter case must still find the guard it claims to pin.

The neuter harness disables one guard by replacing an exact snippet of source,
then checks that a named test goes red. It is not part of this suite, so when a
refactor moves a guard the harness quietly stops finding it and reports a clean
run. That is the worst failure available to it: it says the guards are pinned
while measuring nothing.

One anchor drifted that way and nobody noticed. This test costs a second and
makes the next one fail out loud.
"""

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "tests", "genchain_neuter.py")


def _cases():
    tree = ast.parse(open(HARNESS, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "CASES" in names:
                return ast.literal_eval(node.value)
    raise AssertionError("genchain_neuter.py no longer defines CASES")


class TestNeuterAnchors(unittest.TestCase):

    def test_every_anchor_is_found_in_the_file_it_names(self):
        missing = []
        for name, relative, find, _ in _cases():
            path = os.path.join(ROOT, "generation_chain", relative)
            if find not in open(path, encoding="utf-8").read():
                missing.append(f"{name}: no longer in {relative}")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_every_anchor_matches_exactly_one_place(self):
        # The harness refuses an ambiguous snippet, so an anchor that matches
        # twice is reported as stale rather than run. Same silence, same cost.
        ambiguous = []
        for name, relative, find, _ in _cases():
            path = os.path.join(ROOT, "generation_chain", relative)
            found = open(path, encoding="utf-8").read().count(find)
            if found > 1:
                ambiguous.append(f"{name}: matches {found} places in {relative}")
        self.assertEqual(ambiguous, [], "\n".join(ambiguous))

    def test_the_replacement_actually_changes_the_source(self):
        pointless = [
            name for name, _, find, replace in _cases() if find == replace
        ]
        self.assertEqual(pointless, [], "\n".join(pointless))


if __name__ == "__main__":
    unittest.main()
