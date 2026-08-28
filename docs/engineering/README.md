# Engineering documentation

Everything written down about how this works, and where to start depending on
what you came here to do.

The tool finds objects that a failed `DeleteObjects` left behind in an
Elasticsearch snapshot repository, and removes them when told to. Elasticsearch
8.19.17 and 9.5.0 send an `x-amz-checksum-crc32` header that Oracle's Amazon S3
Compatibility API rejects, so the delete reports success and reclaims nothing.

## Start here, by what you need

**"How is this put together?"**
[architecture.md](architecture.md), 12 diagrams. The system in context, the
component structure of `generation_chain` with every edge traced from real
imports rather than from directory names, deployment three ways, and every
network edge with its protocol, its auth and whether it can change anything at
the far end. Section 4a is the ports and protocols table in the shape a PPSM
registration needs.

**"What does it actually do, step by step?"**
[algorithms.md](algorithms.md), 14 diagrams. The real data shapes, the
derivation end to end with every refusal and the exit code it produces, the
decision for a single key, the shape-gate cascade, one loop cycle as a
sequence, the manifest state machine, read-ahead and the serial critical path,
and the memory model.

**"What happens if it gets something wrong?"**
[../blast-radius.md](../blast-radius.md). What every key is and what it is
worth, why one object belongs to many snapshots, what a wrong delete costs, and
the recovery position: the S3 Compatibility API has no `ListObjectVersions`, so
versioning gives you nothing here even when it is switched on.

**"Is it safe to run near my data?"**
[../security/threat-model.md](../security/threat-model.md), 14 diagrams. Trust
boundaries, the credential lifecycle, the attack surface with STRIDE
annotations, and the four ways this gets used, each with its own exposure. It
also draws the boundary between the audit and the delete tool as modules on
disk rather than as an assertion.

**"I want to run it."**
[../quickstart-read-only.md](../quickstart-read-only.md) first. It reports what
is orphaned and cannot delete, because the read path allows `GET` and `HEAD`
and refuses anything else at the transport.

## The rest, by subject

| Document | What is in it |
|---|---|
| [../repository-layout-and-reachability.md](../repository-layout-and-reachability.md) | How a snapshot repository is laid out on the store, and what reaches what |
| [../oci-s3-compatibility.md](../oci-s3-compatibility.md) | What Oracle's endpoint accepts and rejects, measured against a real bucket |
| [../testing-in-your-oci-environment.md](../testing-in-your-oci-environment.md) | The full procedure for qualifying this against your own cluster and a bucket you do not care about |
| [../quickstart-test-rig.md](../quickstart-test-rig.md) | The shorter version of the same thing |
| [../generating-load.md](../generating-load.md) | The load generator, and how to make a repository leak on purpose |
| [../churn-rig-methodology.md](../churn-rig-methodology.md) | Why the rig is built the way it is |
| [../../FACTS.md](../../FACTS.md) | What was measured, against what, on which day |
| [../../README.md](../../README.md) | The project itself: is this your bug, what the fix is, and how to run it |

The package carries its own README in the repository, covering its layout, its
safety condition, its exit codes and what it cannot see. It is not in the
release, because only Python files ship from that directory.

## Security and compliance

| Document | What is in it |
|---|---|
| [../security/README.md](../security/README.md) | What is in the security folder and how it was produced |
| [../security/threat-model.md](../security/threat-model.md) | Trust boundaries, credentials, attack surface, the four ways this is used |
| [../security/evaluation-report.md](../security/evaluation-report.md) | The findings, including this commit's scanner results and what was judged a false positive |
| [../security/asd-stig-assessment.md](../security/asd-stig-assessment.md) | The Application Security and Development STIG, control by control |
| [../security/what-we-need-from-you.md](../security/what-we-need-from-you.md) | The questions only the operator can answer, and why each one matters |

The machine-readable checklist is `security/elasticsearch-oci-s3-workaround.cklb`,
built from DISA's V6R4 benchmark so every rule carries DISA's own text. It opens
in STIG Viewer. Raw scanner output is in `security/scans/`, one file per
scanner, overwritten on each run rather than accumulating dated copies.

## Two things worth knowing before you read any of it

**The audit and the delete tool are separate programs.** `generation_chain`
reads and cannot delete: its transport permits `GET` and `HEAD` and raises on
anything else, and it never imports the package that deletes.
`generation_chain.reclaim` is the one that removes objects, and it refuses to
run without an approval matching the exact bytes of the manifest it was given.
Conflating them is the most common way to misread everything else here.

**A failed read makes the tool condemn less, never more.** A blob is named as
orphaned only when every shard directory that could reference it was read
successfully and none of them do. That is why a run that could not finish
refuses rather than handing you a shorter list, and why an empty manifest is
never evidence that a repository is clean.

## Keeping these honest

The diagrams are Mermaid, rendered by GitHub and GitLab. The ones in
[algorithms.md](algorithms.md) were machine-rendered through mermaid-cli rather
than eyeballed, which caught two that would not have parsed.

The test suite checks every relative link and heading anchor in this tree, so a
link that rots here fails a test rather than waiting for a reader to find it.
It also refuses to let shipped documentation point at files the release does
not contain.
