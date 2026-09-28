# Documentation

The tool finds objects a failed `DeleteObjects` left behind in an Elasticsearch
snapshot repository, and removes them when told to. Elasticsearch 8.19.17 and
9.5.0 send an `x-amz-checksum-crc32` header that Oracle's Amazon S3
Compatibility API rejects, so the delete reports success and reclaims nothing.

## Where to start

**You are holding a ticket and need to explain this to someone.**
[problem-record.md](problem-record.md). What the symptom is, why monitoring
stays green, who is affected, what it costs and what to do this week.

**You think you have this and want to know what is in your bucket.**
[running-it.md](running-it.md), step one. It counts the orphaned objects, sizes
them, and names every one. Nothing in that step can delete.

**You want the space back.** Read [blast-radius.md](blast-radius.md) first,
then [testing-guide.md](testing-guide.md). Do not start with the delete path.

**You need the repository working again today.**
[The README's step 1](../README.md#1-keep-the-repository-in-service), then
[operating-the-repository.md](operating-the-repository.md) for what
`?verify=false` does and does not change and what to check after any deletion
traffic. On Oracle, nothing can be written until the client setting in
[step 3 of the testing guide](testing-guide.md#step-3-settings-on-the-cluster)
is in place.

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
| [running-it.md](running-it.md) | Audit, reclaim, verify. Only step two deletes, and only what an approved manifest names. Also the read-only Elasticsearch API key |
| [testing-guide.md](testing-guide.md) | Qualify it against your own cluster and a throwaway bucket, on Oracle or MinIO. The settings behind the published numbers, what they cost in storage, and how the rig works |
| [operating-the-repository.md](operating-the-repository.md) | Keeping a repository whose deletes fail in service: `?verify=false` measured operation by operation, `delete_objects_max_size`, checking after deletion traffic, and `base_path` |
| [generating-load.md](generating-load.md) | The load generator, and how to make a repository leak on purpose |
| [run-proofs/README.md](run-proofs/README.md) | Where the proof for a release lives, and what one file has to cover |

## How it works

| | |
|---|---|
| [engineering/architecture.md](engineering/architecture.md) | The system in context, the component structure, deployment three ways, and every network edge with its protocol, its auth and whether it can change anything. Section 4a is the ports and protocols table a PPSM registration needs |
| [engineering/algorithms.md](engineering/algorithms.md) | The data shapes, the derivation end to end with every refusal and its exit code, the condemnation decision, the shape-gate cascade, the manifest states, read-ahead, and the memory model |
| [repository-layout-and-reachability.md](repository-layout-and-reachability.md) | How a snapshot repository is laid out on the store, and what reaches what |
| [blast-radius.md](blast-radius.md) | What every key is worth, why one object belongs to many snapshots, and what a wrong delete costs |
| [oci-s3-compatibility.md](oci-s3-compatibility.md) | What Oracle's endpoint accepts and rejects, measured against a real bucket |
| [problem-record.md](problem-record.md) | The problem record: symptom, root cause, blast radius, detection, workaround and vendor status, for problem management. Then the failure, the root cause and the upstream history in full, for engineers |
| [service-request/](service-request/README.md) | The service request raised with Oracle, and the standalone reproduction it rests on |

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
