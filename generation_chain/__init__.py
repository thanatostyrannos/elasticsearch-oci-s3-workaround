"""Reconstruct the delete operations a snapshot repository's history records.

The reachability sweepers in this project condemn a blob because it is ABSENT
from the live set they compute, so anything that shrinks that live set, a
parse bug, a format change, a truncated blob, produces MORE orphans. This
package condemns on PRESENCE instead: snapshot S was named by root generation
5 and not by generation 6, the shard documents of that era name blob X as
belonging to S, no surviving snapshot names X, so the delete that ran between
those two generations should have removed X and did not.

The safety invariant that follows, and the reason the package is worth having:

  EVERY FAILURE MODE PRODUCES A SMALLER LIST, NEVER A LARGER ONE.

A generation blob it cannot read is a delete it cannot explain. A shard
document it cannot parse is a file list it cannot attribute. A gap in the
chain is history it cannot see. Each of those removes keys from the output and
none of them can add one.

Version one has no delete path. It reads, and it writes manifests.

WHAT IT SUPPORTS AND WHAT IT REFUSES. A root generation that declares
`min_version` 7.12.0 or later, which is RepositoryData's own statement of the
minimum Elasticsearch version able to read it. A catalog below that floor, or
one that declares no version at all, is refused before anything is derived
from it. An older generation deeper in the chain is dropped from the
derivation rather than taking the run down. Nothing here asks Elasticsearch
anything. See `generation_chain/supported.py` for the reasoning in full.
"""

from .derivation.audit import run_audit
from .model import AuditResult, Condemnation, Coverage

__all__ = ["run_audit", "AuditResult", "Condemnation", "Coverage"]
