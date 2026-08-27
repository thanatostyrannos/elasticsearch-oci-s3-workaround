"""Parsing `--max-ram`, and refusing to guess a unit for it.

`--memory-mb 4096` and `--memory-mb 4` differ by a typo and by three orders of
magnitude, and the old flag refused neither. These tests are what stands
against `--max-ram` growing the same hole.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.sizes import InvalidSize, parse_byte_size


class ParsingASize(unittest.TestCase):

    def test_gib_is_a_binary_gigabyte(self):
        # The use case an operator actually types. Getting the multiplier
        # wrong here would size every batch in this run off a number that is
        # quietly off by a factor operators would not notice for months.
        self.assertEqual(4 * (1 << 30), parse_byte_size("4GiB"))

    def test_mib_is_a_binary_megabyte(self):
        self.assertEqual(512 * (1 << 20), parse_byte_size("512MiB"))

    def test_the_unit_is_not_case_sensitive(self):
        # An operator's shell history has both spellings in it. Refusing one
        # case would be a usability trap, not a safety property.
        self.assertEqual(parse_byte_size("4gib"), parse_byte_size("4GiB"))

    def test_a_fractional_size_is_accepted(self):
        self.assertEqual(int(1.5 * (1 << 30)), parse_byte_size("1.5GiB"))

    def test_whitespace_between_the_number_and_the_unit_is_accepted(self):
        self.assertEqual(4 * (1 << 30), parse_byte_size("4 GiB"))

    def test_a_bare_number_is_refused_rather_than_guessed(self):
        # The defect this module exists to close. `--memory-mb 4096` and
        # `--memory-mb 4` were both accepted and meant wildly different
        # things; a bare number here must never be silently read as bytes.
        with self.assertRaises(InvalidSize) as caught:
            parse_byte_size("4096")
        self.assertIn("no unit", str(caught.exception))

    def test_an_unknown_suffix_is_refused_rather_than_rounded_to_the_nearest(self):
        # Abuse case. Accepting "4GB" as though it meant "4GiB" would trade a
        # ten percent error for a flag that looks like it worked.
        with self.assertRaises(InvalidSize):
            parse_byte_size("4GB")

    def test_garbage_text_is_refused(self):
        with self.assertRaises(InvalidSize):
            parse_byte_size("plenty")

    def test_an_empty_string_is_refused(self):
        with self.assertRaises(InvalidSize):
            parse_byte_size("")


if __name__ == "__main__":
    unittest.main()
