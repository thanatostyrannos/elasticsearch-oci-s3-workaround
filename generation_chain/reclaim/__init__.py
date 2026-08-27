"""Delete an approved manifest. Nothing else, ever.

This package is what the audit path is not. `generation_chain/derivation`,
`generation_chain/sources` and `generation_chain/reporting` compute what a
delete operation should have removed and write a manifest naming it; nothing
in any of them may build a request that deletes anything, and
`sources/http_reads.ALLOWED_METHODS` stays `{"GET", "HEAD"}` to make that
structural. This package is the other half: it reads a manifest one of those
tools already wrote, and it turns the keys literally named in it into a
batched S3 `DeleteObjects` call.

THE ONE JOB. `manifest.load_manifest` returns the exact list of keys a
manifest names, in the order it names them, with duplicates intact. Nothing
downstream of that call derives, re-derives, expands, globs, or infers a key.
If a key is not a literal line in the file this package was given, this
package does not know it exists.

WHY THE CALL WAS UNREACHABLE UNTIL NOW. Every `DeleteObjects` request needs a
content checksum, `Content-MD5` or a `x-amz-checksum-*` header, and Amazon's
own SDK stopped sending one on the path Elasticsearch 8.19.17+ and 9.5.0+ take
(see the top-level README). A client that builds the request itself is not
subject to that gap. `checksum.py` computes the header the operator's store
requires, `batch.py` builds the request body once and reads the header off
that exact object, and `transport.py` is the only place in this project
authorised to send a method other than GET or HEAD.

WHY APPROVAL IS A DIGEST, NOT A FLAG. A flag meaning "delete whatever the
manifest says" would still be automation once the manifest can be regenerated
or hand-edited after an operator looked at it. `approval.py` ties an
operator's sign-off to the exact bytes of one manifest: the sha256 of a
different file, or an edited copy of the same one, does not match, and the
run refuses rather than substituting a manifest nobody looked at.

WHY A MANIFEST CAN BE REFUSED FOR LOOKING FINE. `reporting/manifest.py` writes
a header and one row per key with nothing that marks the last row as the last
one, so a process killed midway leaves a file that reads exactly like a
complete one. `manifest.py` in this package requires the file to end on a
newline and every row to carry the full column count, which is what a write
stopped mid-row fails to do, and it is belt-and-suspenders rather than the
whole of the defence: the actual proof that a manifest is complete is the
operator's own approval, computed after they have satisfied themselves the run
that produced it finished.

DRY RUN IS THE DEFAULT. Nothing here sends a request without `--execute`, and
`--execute` refuses without an approval that matches the manifest given on
this invocation.
"""

from __future__ import annotations
