"""The supported Python floor is one number, and everything must agree on it.

It was not. Nine documents promised 3.9 while every workflow ran 3.11 or 3.12,
and nothing proved 3.9 worked. It did not: four tests errored there. A claim no
job exercises is not support, and the way that happens is a document and a
workflow drifting apart with nobody reading both.

So the floor is declared once, in sonar-project.properties, and this checks
that the matrix, the documents and the running interpreter all say the same.
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONAR = os.path.join(ROOT, "sonar-project.properties")
MATRIX = os.path.join(ROOT, ".github", "workflows", "python-matrix.yml")

# Every document that states a minimum. Adding one without adding it here is
# how the last drift started, so the shipped-docs test below catches the case
# where a file mentions a floor this list does not know about.
DOCUMENTS = (
    "README.md",
    os.path.join("docs", "generating-load.md"),
    os.path.join("docs", "quickstart-read-only.md"),
    os.path.join("docs", "quickstart-test-rig.md"),
    os.path.join("docs", "testing-in-your-oci-environment.md"),
    os.path.join("gitlab", "README.md"),
)

STATES_A_FLOOR = re.compile(r"Python (\d+\.\d+)(?:\+| or newer| or later)")


def declared_floor():
    with open(SONAR, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("sonar.python.version="):
                return line.split("=", 1)[1].strip()
    raise AssertionError("sonar-project.properties declares no python version")


def matrix_versions():
    with open(MATRIX, encoding="utf-8") as handle:
        body = handle.read()
    row = re.search(r'python:\s*\[([^\]]+)\]', body)
    assert row, "the matrix workflow lists no interpreters"
    return [v.strip().strip('"\'') for v in row.group(1).split(",")]


def as_tuple(version):
    return tuple(int(part) for part in version.split(".")[:2])


class TestTheFloorIsOneNumber(unittest.TestCase):

    def test_the_matrix_starts_at_the_declared_floor(self):
        floor = declared_floor()
        lowest = min(matrix_versions(), key=as_tuple)
        self.assertEqual(
            as_tuple(lowest), as_tuple(floor),
            f"sonar-project.properties says {floor} but the lowest interpreter "
            f"the suite runs on is {lowest}. One of them is wrong, and the "
            f"documents follow whichever is written down.")

    def test_every_document_states_the_declared_floor(self):
        floor = as_tuple(declared_floor())
        wrong = []
        for relative in DOCUMENTS:
            path = os.path.join(ROOT, relative)
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    for stated in STATES_A_FLOOR.findall(line):
                        if as_tuple(stated) != floor:
                            wrong.append(f"{relative}:{number} says {stated}")
        self.assertEqual(wrong, [], "\n".join(wrong))

    def test_no_other_shipped_document_states_a_different_floor(self):
        # Catches a new document that names a floor nobody added to the list.
        known = {os.path.join(ROOT, d) for d in DOCUMENTS}
        floor = as_tuple(declared_floor())
        wrong = []
        for folder, _, names in os.walk(ROOT):
            if any(part in folder for part in (".git", "evidence", "dist")):
                continue
            for name in names:
                if not name.endswith(".md"):
                    continue
                path = os.path.join(folder, name)
                if path in known:
                    continue
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, 1):
                        for stated in STATES_A_FLOOR.findall(line):
                            if as_tuple(stated) != floor:
                                rel = os.path.relpath(path, ROOT)
                                wrong.append(f"{rel}:{number} says {stated}")
        self.assertEqual(wrong, [], "\n".join(wrong))

    def test_this_interpreter_is_at_or_above_the_floor(self):
        floor = as_tuple(declared_floor())
        self.assertGreaterEqual(
            sys.version_info[:2], floor,
            f"running on {sys.version_info.major}.{sys.version_info.minor}, "
            f"below the declared floor {floor[0]}.{floor[1]}")


if __name__ == "__main__":
    unittest.main()
