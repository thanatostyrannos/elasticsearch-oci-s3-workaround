"""Readers for the four Elasticsearch formats this derivation needs.

Written from the format rather than imported from one of this project's
retired sweepers, and that deliberate duplication is the point. Two
derivations that share a parser fail together and then agree with each
other, which is worse evidence than one derivation on its own.
"""
