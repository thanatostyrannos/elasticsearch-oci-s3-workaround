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
[docs/running-it.md](running-it.md). It reads the
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
from a written manifest. See [Using it](running-it.md).

Reclaiming is a manual loop, not a fix. Somebody reads the manifest every time,
and the leak resumes when the loop stops.

## Reproducing it

[docs/testing-guide.md](testing-guide.md)
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

# The detail, for engineers

## The failure in detail

The same failure is reported against NetApp StorageGRID, Hitachi Content
Platform (HCP) (reported fixed in later releases), Ceph RADOS Gateway, and MinIO
before its January 2025 fix. In testing, `RELEASE.2025-01-18T00-31-37Z` rejects
and `RELEASE.2025-01-20T14-49-07Z` accepts. AWS S3 itself is unaffected; it
treats `Content-MD5` as optional. What matters is the storage endpoint, not how
Elasticsearch is deployed: any self-managed, ECK or ECE cluster pointing a
repository at an affected store will hit this.

Since AWS SDK for Java v2.30.0 the SDK sends flexible checksums
(`x-amz-checksum-crc32`) instead of `Content-MD5`, including on checksum-required
operations like S3 Multi-Object Delete (`DeleteObjects`). Elasticsearch picked
this up when it removed a legacy signer override, which moved the SDK onto its
post-SRA path. Then the algorithms collide. The SDK defaults to CRC32. The
Amazon S3 Compatibility API accepts only `x-amz-checksum-sha256` and
`x-amz-checksum-crc32c` as alternatives to `Content-MD5`, so the out-of-the-box
default is the one algorithm such stores reject, and the whole batch comes back
HTTP 400. Measured against a real Oracle bucket on 2026-08-26, in
`us-ashburn-1`: `crc32` fails with `0 deleted, 2 failed, 400 InvalidRequest`,
while `crc32c`, `sha256` and `Content-MD5` all succeed against the same bucket
with the same tool (see [the full capture](../FACTS.md#the-fault-this-repository-exists-for)
in FACTS.md). The same collision explains why `WHEN_REQUIRED` cannot help:
that setting narrows *which operations* get a checksum, not *which
algorithm*.

The affected releases are **Elasticsearch 8.19.17+ and 9.5.0+**. Releases
8.19.0 through 8.19.16, 9.1 through 9.4, and everything predating the AWS SDK v2 migration
(including 9.0.x and 8.18.x) are unaffected, which is why an upgrade is usually
the moment the problem appears. The workaround Elastic support provides is
registering the repository with `?verify=false`. That restores registration and
snapshots. It does **not** make deletes work, so the leak continues.

No upstream fix has been offered to date. Elastic declined a proposal to expose
the S3 checksum algorithm as a repository setting, and declined a request to
document the change as breaking. Its published position is that the storage
vendor should fix this. See
[Root cause and upstream status](#root-cause-and-upstream-status) for the
detail and the sources. Check your vendor's accepted-checksum list. If it
excludes CRC32 and the vendor's published remedy is client-side (Oracle's
Amazon S3 Compatibility API documentation lists sha256 and crc32c as the
alternatives and points at `LegacyMd5Plugin` rather than announcing a
server-side change), then neither side has published a fix, and you should plan
as though the leak persists.

What this repository gives you:

1. An audit and reclaim pair that measures what already leaked and removes
   only what a human approved from a written manifest (see [Using it](running-it.md)).
2. [A validated runbook for getting off the broken path](https://gist.github.com/thanatostyrannos/cb7ccafece8d74be125edc9b7fa77f14),
   moving backups to block/NFS storage while the frozen tier stays mounted
   where it is. Its two cleanup steps are marked withdrawn in place: they drove
   an earlier tool that no longer exists, and the pair above replaces them.

Everything here was reproduced and validated end to end against a real
Elasticsearch 9.5.2 cluster and a fault-reproducing object store, including a
campaign against a real Oracle bucket: 58 reclaim cycles, 888 objects deleted,
zero failed and zero unconfirmed (see [Campaign results](../FACTS.md#campaign-results-2026-08-27-against-a-real-oracle-bucket)
in FACTS.md). Reproduce it against your own cluster with
[Testing in your own OCI environment](testing-guide.md).

## Root cause and upstream status

The AWS SDK v2 migration ([#126843](https://github.com/elastic/elasticsearch/pull/126843), in 8.19.0 and
9.1.0) carried a request-signer override that kept the SDK on its pre-SRA code
path, where checksum-required operations still receive `Content-MD5`. That is
why 8.19.0 through 8.19.16 and every 9.1 through 9.4 release interoperate with these stores
unchanged. [#150194](https://github.com/elastic/elasticsearch/pull/150194) removed the override. That was a reasonable change on its
own terms, since it restored the SDK's intended signing for
`PutObject`/`UploadPart`, but it also moved `DeleteObjects` onto the SRA path:
`x-amz-checksum-crc32`, no `Content-MD5`. The removal was backported to `8.19`
the same day as
[#150237](https://github.com/elastic/elasticsearch/pull/150237). By release
date users met it first in 8.19.17 (2026-06-23), a patch release, and only six
weeks later in 9.5.0 (2026-08-04).

A retroactive changelog PR seven weeks after the merge
([#153937](https://github.com/elastic/elasticsearch/pull/153937), "Add
changelog for #150194") added a one-line entry: "Update `repository-s3` to use
the default request signer from AWS SDK for Java." The candid version stayed in
that PR's own description, "Turns out that some S3-compatible storage behaves
differently with this change so it's worth mentioning in the changelog after
all", and never reached users. That entry was backported to `9.5` only, so
8.19.17 shipped the change with no release-note entry of any kind. The labels
are worth comparing too: the SDK v2 migration
([#126843](https://github.com/elastic/elasticsearch/pull/126843)) carried a `>breaking` label, while
[#150194](https://github.com/elastic/elasticsearch/pull/150194), the change that altered the wire format for these
stores, carried only `>bug`.

On an existing repository the failure is silent. Elasticsearch completes
the snapshot-delete API response *before* blob cleanup runs, and the cleanup
path catches and logs every deletion failure without rethrowing
([`BlobStoreRepository.java:1237-1243`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L1237),
[`:1581-1590`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L1581), [`:1611-1615`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L1611)). So snapshot delete,
SLM retention, and repository `_cleanup` all report success while leaving the
blobs behind. You get unbounded repository growth and WARN-level log noise. Only
repository `_analyze` and a fresh registration surface the 400 directly.
Elasticsearch has an open issue about tightening this leniency
([#100569](https://github.com/elastic/elasticsearch/issues/100569), "Less
lenience during snapshot deletion", open since 2023), referenced from a TODO in
the delete path itself.

No Elasticsearch configuration reaches it.

- `repository-s3` exposes no checksum-related setting at repository, client, or
  node level.
- The SDK's opt-out does not apply. `WHEN_REQUIRED` means checksum calculation
  happens "only when required by the API operation", and `DeleteObjects` *is*
  checksum-required (`requestChecksumRequired: true` in the SDK's S3 model;
  AWS: "The Content-MD5 request header is required for all Multi-Object Delete
  requests"). Setting `aws.requestChecksumCalculation=WHEN_REQUIRED` still
  leaves CRC32 on the delete.
- AWS's designated remedy, `LegacyMd5Plugin` (SDK ≥ 2.31.32), is a client
  plugin. Elasticsearch bundles the SDK version containing it (2.31.78) with no
  way to enable it.
- Peer projects ship this knob. aws-cli exposes `--checksum-algorithm` on
  `s3api delete-objects` itself, with `MD5` among its accepted values, the
  closest precedent there is. OpenSearch 3.3.0 added a legacy-MD5 *repository*
  setting ([opensearch-project/OpenSearch#19220](https://github.com/opensearch-project/OpenSearch/pull/19220));
  its client-settings form was unusable until 3.4.0. Hadoop S3A has
  `fs.s3a.create.checksum.algorithm`, though that governs the upload path only,
  not multi-object delete.

`Content-MD5` is optional on `DeleteObjects` per the S3 API, genuine AWS S3
accepts the SDK's current request, and a store that rejects it is the
non-conforming party. Elastic's reasoning follows from that and is largely
correct: the header is optional, AWS treats it as legacy, the SDK stopped
sending it, and a store requiring it is not fully S3-compatible. A vendor-side
fix is the clean resolution. So what follows is a request for a compatibility
accommodation with an unchanged default, an enhancement rather than a bug
report. Its argument is that Elastic's reasoning addresses only the server side
of the connection, while AWS itself ships a client-side mechanism for this case.

The proposal upstream was an optional per-repository setting. Leave it absent
and you get today's behavior, with not one byte on the wire changed:

```
checksum_algorithm: crc32c | sha256 | crc32 | md5
```

`crc32c` and `sha256` pass through the official S3 request member
(`DeleteObjectsRequest.Builder#checksumAlgorithm`), and they are what Oracle
documents as the Amazon S3 Compatibility API's accepted alternatives. `md5`
restores `Content-MD5` via `LegacyMd5Plugin`, AWS's own published compatibility
mechanism. Every value is a server-verified integrity header genuine AWS
accepts on this operation, so no
setting disables a protection, unlike
`unsafely_incompatible_with_s3_conditional_writes` ([#137185](https://github.com/elastic/elasticsearch/pull/137185)). The closest
precedent is `disable_chunked_encoding`
([#44052](https://github.com/elastic/elasticsearch/pull/44052)), a compatibility pass-through accepted once no alternative was
demonstrated.

Status: declined. The proposal went to Elastic through a support case. Elastic
declined it, and declined a separate request to document the change as
breaking. It reached users on both branches without that label: as
[#150194](https://github.com/elastic/elasticsearch/pull/150194) in 9.5.0, a
minor release, and as
[#150237](https://github.com/elastic/elasticsearch/pull/150237) in 8.19.17,
a patch release. Operators upgrade minors and patches expecting working behavior to hold. On
both branches, a cluster snapshotting to an unchanged bucket stops reclaiming
space with no change on the operator's side.

That outcome is not personal to one case. It follows Elastic's published
policy on this class of report, which the S3 repository documentation states
directly: operators should ensure their storage supplier offers a full
compatibility guarantee, and should not report Elasticsearch issues involving
storage that claims S3 compatibility unless the same issue can be demonstrated
against genuine AWS S3
([docs](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/s3-repository#repository-s3-compatible-services)).
A public report of the adjacent upload-path symptom from this same SDK change
([#156269](https://github.com/elastic/elasticsearch/issues/156269), a
`Content-SHA256` mismatch on upload rather than the delete failure described
here) was closed `not_planned` eight minutes and forty-three seconds after it
was filed, citing that policy.

Elastic later issued a support knowledge-base article on the delete
failure. Without reproducing its wording, it confirms the affected version
boundary, states that no workaround exists within Elasticsearch, confirms that
`WHEN_REQUIRED`, `disable_chunked_encoding` and `always_sign_requests` are all
ineffective against this error, notes that downgrading is not viable for most
deployments, and acknowledges that the `?verify=false` workaround leaves
unreachable objects behind and inflates storage usage. It also notes that
Elasticsearch cleans those leaked objects up automatically once the storage
service accepts the deletes again. That self-healing is real, and worth
knowing: the leak is recoverable, not permanent corruption. It also depends on
a vendor-side change, and it would hold just as well after a client-side fix in
Elasticsearch. The article gives one resolution, vendor-side: reconfigure or
upgrade the storage. It does not mention
`checksumAlgorithm`, `LegacyMd5Plugin`, aws-cli's `--checksum-algorithm`, or
OpenSearch's shipped setting, which is the client-side half of the solution space.

If your storage vendor ships a fix, take it. That is the clean resolution and it
makes this repository unnecessary. If your vendor treats its checksum list as a
design decision, and Oracle documents the Amazon S3 Compatibility API's that
way and points back at client-side `LegacyMd5Plugin` as the remedy, then no fix
is coming from either
side, and the leak is permanent until you change the architecture. That is what
this repository is for.

Sources:

- AWS SDK for Java 2.x, [S3 checksums](https://docs.aws.amazon.com/sdk-for-java/latest/developer-guide/s3-checksums.html).
  The 2.30.0 behavior change and `LegacyMd5Plugin`.
- AWS, [Data Integrity Protections for Amazon S3](https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html).
  Defines `WHEN_REQUIRED`. The consequence that matters here is stated on the
  Java SDK checksums page above: "Some S3 operations, however, require a
  checksum calculation; you cannot disable checksum calculation for these
  operations."
- AWS S3 API Reference, [DeleteObjects](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObjects.html).
  `Content-MD5` required for Multi-Object Delete.
- Oracle, [Amazon S3 Compatibility API Support](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi_topic-Amazon_S3_Compatibility_API_Support.htm).
  The Amazon S3 Compatibility API accepts `x-amz-checksum-sha256` and
  `x-amz-checksum-crc32c` as alternatives to `Content-MD5`, and recommends
  `LegacyMd5Plugin` client-side.
- NetApp, [multi-object delete fails on StorageGRID after AWS SDK v2.30.x](https://kb.netapp.com/hybrid/SGRID/Object_Mgmt/Object_Mgmt_KBs/After_upgrading_AWS_SDK_to_v2_30_x_multi_object_delete_fails_on_StorageGRID)
- Dell, [KB 000299507, ECS: AWS CLI fails with Missing Content-MD5](https://www.dell.com/support/kbdoc/en-us/000299507/awscli-fails-with-missing-content-md5).
  The same class of failure on Dell ECS, but from AWS CLI 2.23.0+ sending
  `CRC64NVME` on bucket-config operations. Different client and operation;
  cited as corroboration of the pattern, not of the `DeleteObjects` path.
- GitLab, [19.0 upgrade notes](https://docs.gitlab.com/update/versions/gitlab_19_changes/).
  The same `DeleteObjects` CRC32 collision hitting S3-compatible backends in
  another project, with no configuration workaround available there either.
- [`LegacyMd5Plugin` javadoc](https://docs.aws.amazon.com/java/api/latest/software/amazon/awssdk/services/s3/LegacyMd5Plugin.html)
- Elastic, [S3-compatible services](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/s3-repository#repository-s3-compatible-services).
  The published policy on reports involving non-AWS S3 endpoints.
- Elasticsearch PRs and issues: [#126843](https://github.com/elastic/elasticsearch/pull/126843) (SDK v2 migration), [#150194](https://github.com/elastic/elasticsearch/pull/150194) / [#150237](https://github.com/elastic/elasticsearch/pull/150237) (signer
  override removed, the trigger), [#153937](https://github.com/elastic/elasticsearch/pull/153937) (follow-up changelog note), [#100569](https://github.com/elastic/elasticsearch/issues/100569)
  (snapshot deletion tolerates blob-deletion failures), [#44052](https://github.com/elastic/elasticsearch/pull/44052)
  (`disable_chunked_encoding` precedent), [#137185](https://github.com/elastic/elasticsearch/pull/137185) (the disanalogy),
  [#156269](https://github.com/elastic/elasticsearch/issues/156269) (adjacent
  upload-path report, closed under the S3-compatibility policy).
- OpenSearch: [opensearch-project/OpenSearch#18240](https://github.com/opensearch-project/OpenSearch/issues/18240), [#19220](https://github.com/opensearch-project/OpenSearch/pull/19220) (the shipped
  client-side setting).
- AWS SDK: [aws/aws-sdk-java-v2#5805](https://github.com/aws/aws-sdk-java-v2/issues/5805), [#6055](https://github.com/aws/aws-sdk-java-v2/pull/6055).
