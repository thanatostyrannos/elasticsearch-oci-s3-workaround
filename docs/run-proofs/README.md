# Run proofs

One file per release, recording what the test cycle did against both stores.

The known-state test cycle writes `docs/run-proofs/<version>.md` after a
qualification run. The point of running against two stores is the
comparison, so the proof is one file covering both rather than one file
each.

The directory is here rather than created on demand because git does not track
an empty one, and a document that tells you to write into a path that does not
exist is a document that fails the moment somebody follows it. That is exactly
how it was found: the reference passed every local check, because the directory
existed on the machine where the skill was written, and failed on the first
clean checkout.

Nothing is in here yet. The runs so far predate the format.
