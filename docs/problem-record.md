# Problem record: snapshot deletion silently reclaims nothing

For whoever is holding the ticket. What the symptom is, why it does not look
like a fault, what it costs, and what to do this week rather than eventually.

The evidence behind every claim here is in [FACTS.md](../FACTS.md), which is
written for engineers and cites Elasticsearch source and released version
numbers. This page is the summary a problem manager needs.

## Statement

Elasticsearch reports snapshot deletions as successful. The objects are not
deleted. Storage grows without limit and nothing in the platform raises an
alarm.

## Status

Open with the storage vendor. Oracle accepts `Content-MD5`,
`x-amz-checksum-sha256` and `x-amz-checksum-crc32c` on batch delete, and
rejects `x-amz-checksum-crc32`, which is what the AWS SDK for Java sends by
default and what Elasticsearch therefore sends. A service
request carries a reproduction that isolates the difference: the same request,
same bucket, same credentials, four integrity headers, three accepted and one
rejected within two seconds.

Elastic declined to change this and consider it the storage vendor's to fix, so
there is no release to wait for on that side either.

## What you will see

Retention runs. Snapshots leave the catalogue on schedule. The API returns
`acknowledged: true`. Dashboards built on Snapshot Lifecycle Management stay
green.

Meanwhile the bucket only grows. In one measured run the tool found 2,453
expired snapshot documents still present after 2,092 snapshots had expired.

The only signal is a `WARN` line per failed batch in the Elasticsearch log,
naming keys it could not delete. It is easy to miss, and on a busy cluster the
volume trains people to filter it out.

## Why monitoring does not catch it

The delete is asynchronous and best-effort. Elasticsearch removes the snapshot from its catalogue, asks the
store to remove the blobs, and reports success on the catalogue change. The
store's refusal arrives afterwards and does not travel back to the caller.

So every green light is telling the truth about the thing it measures. The
snapshot really was deleted, from the catalogue. Nobody is monitoring the
bucket, because until now there was no reason to.

Expect the first report to arrive as a cost or capacity ticket rather than a
backup one.

## Who is affected

Elasticsearch **8.19.17 and later, or 9.5.0 and later**, with a snapshot
repository on an S3-compatible store that requires `Content-MD5` on batch
delete. Oracle Cloud Infrastructure Object Storage is one such store.

Earlier releases are not affected: 8.19.0 through 8.19.16, 9.1 through 9.4, and
anything before the AWS SDK v2 migration including 9.0.x and 8.18.x. **An
upgrade is usually the moment this appears**, which is why it often gets
attributed to the upgrade rather than to the store.

Amazon S3 itself is unaffected. It accepts the checksum the SDK sends.

## Root cause

Since AWS SDK for Java v2.30.0 the SDK sends flexible checksums,
`x-amz-checksum-crc32`, in place of `Content-MD5`, including on operations that
require a checksum such as `DeleteObjects`. It defaults to CRC32. Elasticsearch
picked this up through its SDK v2 migration.

CRC32C differs from CRC32 only in the polynomial. Oracle accepts CRC32C and
rejects CRC32, so the request fails before the store looks at the keys.

There is no Elasticsearch setting that changes the algorithm.

Measured request and response detail:
[docs/oci-s3-compatibility.md](oci-s3-compatibility.md).

## Impact

### Cost

Storage grows monotonically. Retention reclaims nothing, so there is no
ceiling.

### Deletion stops meaning destruction

A snapshot leaves the catalogue while its data stays in the bucket. Anyone
relying on deletion for records retention, data minimisation or spillage
remediation no longer has that guarantee. This is usually the finding that
matters to an auditor, not the cost.

### Monitoring is misleading

Alerting built on Snapshot Lifecycle Management reports success. Ambient `WARN`
noise trains operators to ignore the log lines that would carry the next real
failure.

What a wrong delete would cost, if a cleanup tool got it wrong:
[docs/blast-radius.md](blast-radius.md).

## Telling whether you are affected

Two checks, both read-only.

Check the version boundary above. Then run the audit in
[docs/quickstart-read-only.md](quickstart-read-only.md). It reads the
repository and reports what is present that no live snapshot references. It
permits `GET` and `HEAD` only and has no delete path, so it is safe to run
against production.

A number close to zero means you are not affected or not affected yet.

## What to do

### First, keep the repository in service

Re-register with `?verify=false`, settings otherwise unchanged. Verification
itself performs a batch delete, so on an affected store registration fails and
the repository becomes unusable. This takes a minute and stops the bleeding.

### Then move the backups

A filesystem repository makes retention an ordinary unlink with no tooling in
the loop. This is the fix rather than the mitigation, and it ends the problem
for whatever moves. The frozen tier usually stays behind at much lower volume.

### Reclaim what already leaked

The tool in this repository does it in two halves: an audit that reads and
cannot delete, and a separate tool that removes only what a person approved
from a written manifest. See [Using it](../README.md#using-it).

Reclaiming is a manual loop, not a fix. Somebody reads the manifest every time,
and the leak resumes when the loop stops.

## Reproducing it

[docs/testing-in-your-oci-environment.md](testing-in-your-oci-environment.md)
walks through standing up a separate bucket and confirming the fault in your
own tenancy before trusting anything here.

One setting there is easy to miss and blocks everything before you reach this
problem: Elasticsearch cannot write to OCI at all without
`disable_chunked_encoding`, because the SDK sends `aws-chunked` content
encoding and Oracle answers 501.

## Where the tool may run

Four modes, each with its own boundary and its own risks:
[docs/security/threat-model.md](security/threat-model.md). If you are
approving this for use rather than triaging the symptom, that is the document
to read, along with
[what we need from you](security/what-we-need-from-you.md).
