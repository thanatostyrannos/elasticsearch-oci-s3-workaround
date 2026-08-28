# Documentation

The tool finds objects a failed `DeleteObjects` left behind in an Elasticsearch
snapshot repository, and removes them when told to. Elasticsearch 8.19.17 and
9.5.0 send an `x-amz-checksum-crc32` header that Oracle's Amazon S3
Compatibility API rejects, so the delete reports success and reclaims nothing.

## Where to start

**You think you have this and want to know what is in your bucket.**
[quickstart-read-only.md](quickstart-read-only.md). It counts the orphaned
objects, sizes them, and names every one. It cannot delete.

**You want the space back.** Read [blast-radius.md](blast-radius.md) first,
then [testing-in-your-oci-environment.md](testing-in-your-oci-environment.md).
Do not start with the delete path.

**You are reviewing this before it goes near production.**
[security/threat-model.md](security/threat-model.md), then
[security/evaluation-report.md](security/evaluation-report.md).

**You are about to change the code.**
[engineering/algorithms.md](engineering/algorithms.md) for what it does, and
[engineering/architecture.md](engineering/architecture.md) for how the pieces
fit.

## Running it

| | |
|---|---|
| [quickstart-read-only.md](quickstart-read-only.md) | Report what is orphaned. Deletes nothing |
| [testing-in-your-oci-environment.md](testing-in-your-oci-environment.md) | Qualify it against your own cluster and a throwaway bucket. The settings behind the published numbers, and what they cost in storage |
| [quickstart-test-rig.md](quickstart-test-rig.md) | The short version of the same thing |
| [generating-load.md](generating-load.md) | The load generator, and how to make a repository leak on purpose |
| [churn-rig-methodology.md](churn-rig-methodology.md) | Why the rig is built the way it is, and which of its numbers transfer |

## How it works

| | |
|---|---|
| [engineering/architecture.md](engineering/architecture.md) | The system in context, the component structure, deployment three ways, and every network edge with its protocol, its auth and whether it can change anything. Section 4a is the ports and protocols table a PPSM registration needs |
| [engineering/algorithms.md](engineering/algorithms.md) | The data shapes, the derivation end to end with every refusal and its exit code, the condemnation decision, the shape-gate cascade, the manifest states, read-ahead, and the memory model |
| [repository-layout-and-reachability.md](repository-layout-and-reachability.md) | How a snapshot repository is laid out on the store, and what reaches what |
| [blast-radius.md](blast-radius.md) | What every key is worth, why one object belongs to many snapshots, and what a wrong delete costs |
| [oci-s3-compatibility.md](oci-s3-compatibility.md) | What Oracle's endpoint accepts and rejects, measured against a real bucket |

## Security and compliance

| | |
|---|---|
| [security/README.md](security/README.md) | What is in the security folder and how it was produced |
| [security/threat-model.md](security/threat-model.md) | Trust boundaries, credentials, attack surface, and the four ways this gets used |
| [security/evaluation-report.md](security/evaluation-report.md) | The findings, the scanner results, and what was judged a false positive |
| [security/asd-stig-assessment.md](security/asd-stig-assessment.md) | The Application Security and Development STIG, control by control |
| [security/what-we-need-from-you.md](security/what-we-need-from-you.md) | The questions only you can answer |

`security/elasticsearch-oci-s3-workaround.cklb` opens in STIG Viewer. It is
built from DISA's V6R4 benchmark, so every rule carries DISA's own text. The
scanner output it cites is beside it in `security/scans/`: `bandit.json`,
`semgrep.json` and `trivy.json`, one file per scanner.

## Elsewhere

[../README.md](../README.md) is the project itself: whether this is your bug,
what the fix is, and how to run it. [../FACTS.md](../FACTS.md) is what was
measured, against what, on which day.

The package has its own README in the repository covering its layout, its exit
codes and what it cannot see. It is not in the release, because only Python
files ship from that directory. Contributors have their own guide there too.

## Two things that make the rest readable

**The audit and the delete tool are separate programs.** `generation_chain`
reads and cannot delete: its transport allows `GET` and `HEAD` and raises on
anything else, and it never imports the package that deletes.
`generation_chain.reclaim` removes objects, and refuses to run without an
approval matching the exact bytes of the manifest it was handed. Conflating
them is the most common way to misread everything else here.

**A failed read makes the tool condemn less, never more.** A blob is named as
orphaned only when every shard directory that could reference it was read
successfully and none of them do. So a run that could not finish refuses
instead of handing you a shorter list, and an empty manifest is never evidence
a repository is clean.
