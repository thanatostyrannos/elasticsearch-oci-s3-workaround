"""Readers for the four Elasticsearch formats this derivation needs.

Written from the format rather than imported from `s3_repo_sweeper.py`, and
that duplication is the point. Two derivations that share a parser fail
together and then agree with each other, which is worse evidence than one
derivation on its own.
"""
