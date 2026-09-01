# Service Request: DeleteObjects rejects x-amz-checksum-crc32

## Summary

The Object Storage S3 Compatibility API rejects a `DeleteObjects` request whose
integrity header is `x-amz-checksum-crc32`, returning HTTP 400 `InvalidRequest`
with the message:

    Missing required header for this request: Content-MD5 or
    x-amz-checksum-sha256 or x-amz-checksum-crc32c

Amazon S3 accepts `crc32`. `crc32c` differs from it only in the polynomial, and
Object Storage accepts `crc32c`. The accepted set is documented and `crc32` is
not in it, so the product is behaving as specified. We are asking for it to be
added, because the AWS SDK for Java sends `crc32` by default and the clients
built on it cannot be configured to send anything else.

## Impact

Elasticsearch 9.5.2 sends `x-amz-checksum-crc32` on `DeleteObjects` by default.
Every batch delete a snapshot repository issues against OCI therefore fails.
Elasticsearch reports the snapshot deletion as acknowledged, so the objects are
never reclaimed and storage grows without bound. There is no setting in
Elasticsearch to change the checksum algorithm.

## Environment

| | |
|---|---|
| Region | us-ashburn-1 |
| Object Storage namespace | <namespace> |
| Bucket | esprobe-repo |
| Endpoint | https://<namespace>.compat.objectstorage.us-ashburn-1.oraclecloud.com |
| Tenancy OCID | <tenancy-ocid> |
| Elasticsearch | 9.5.2 |
| AWS SDK for Java | 2.31.78 |

## Minimal reproduction

`oci-deleteobjects-checksum-repro.py` is attached. It is standard library only
and independent of Elasticsearch. It sends the SAME `DeleteObjects` body to the
SAME bucket with the SAME credentials four times, varying only the integrity
header. The keys named do not exist, so nothing is deleted: the request is
rejected before the keys are examined.

```
endpoint : https://<namespace>.compat.objectstorage.us-ashburn-1.oraclecloud.com
bucket   : esprobe-repo
region   : us-ashburn-1
body     : 223 bytes, sha256 efedec44f57cef5387752ce6311da99207b25704d03043901b7306066488e6b6

Content-MD5              200 ACCEPTED
  sent (UTC)      : 2026-09-01T14:27:37.939999+00:00
  opc-request-id  : iad-1:rq7gfJv2hh8uOTl9GamelXx-LLbP6LdL4PWbWQI1hKnXhTNscm6yAQi4yHdB915F

x-amz-checksum-crc32c    200 ACCEPTED
  sent (UTC)      : 2026-09-01T14:27:38.379435+00:00
  opc-request-id  : iad-1:Q2AK_lGKXviu3sLP52hmKjRXgBfrohdZVQ1LzU6NXxcNnWlZsmn5GDr5qmgPabC8

x-amz-checksum-sha256    200 ACCEPTED
  sent (UTC)      : 2026-09-01T14:27:38.743726+00:00
  opc-request-id  : iad-1:c5GkrGLPV2F6fPTR2IC2utnp3ztkj7xPBrR4kUCevrqOBz7xCkIyaIW4OOlxzaGj

x-amz-checksum-crc32     400 REJECTED
  sent (UTC)      : 2026-09-01T14:27:39.133071+00:00
  opc-request-id  : iad-1:h35-GvOCtr5Uft5wy9lFXcMCXyt-zGgylUftfe6RdKC0SUEW9mIWLHJGpot-VNBD
  response        : <?xml version="1.0" encoding="UTF-8"?><Error><Message>Missing required header for this request: Content-MD5 or x-amz-checksum-sha256 or x-amz-checksum-crc32c</Message><Code>InvalidRequest</Code></Error>
```

Three accepted, one rejected, within 1.2 seconds of each other.

## The failure inside Elasticsearch

Reproduced at 2026-09-01T14:25:23.632Z by registering a snapshot repository, which runs a batch
delete during verification.

API response:

```json
{"error":{"root_cause":[{"type":"repository_verification_exception","reason":"[srproof] cannot delete test data at [snapshots]"}],"type":"repository_verification_exception","reason":"[srproof] cannot delete test data at [snapshots]","caused_by":{"type":"i_o_exception","reason":"Failed to delete blobs [ObjectIdentifier(Key=snapshots/tests-KVNM8d9hRgOgFlV7PSERSg/data-XhEnAPKoTnaBR1-rt3GBYw.dat), ObjectIdentifier(Key=snapshots/tests-KVNM8d9hRgOgFlV7PSERSg/master.dat), ObjectIdentifier(Key=snapshots/tests-KVNM8d9hRgOgFlV7PSERSg/)]","caused_by":{"type":"invalid_request_exception","reason":"Missing required header for this request: Content-MD5 or x-amz-checksum-sha256 or x-amz-checksum-crc32c (Service: S3, Status Code: 400, Request ID: iad-1:b5ImJOs_hrrp0KEm1P0ZMk8j_dBRo0e20SGD_zJQkbZaWygg-bkfigsGMjJwCdkF) (SDK Attempt Count: 1)"}}},"status":500}
```

Stack trace from the Elasticsearch log:

```
java.io.IOException: Failed to delete blobs [ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-2ConvGUcSPq_EhZAT7A3-Q), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-2GMqGFHFSiOxGQxSJFYGcQ), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-3r-tYpV7Q0aemTZLpXs4rw), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-9IJhpHRVQ3SLMtfoXfPWUg), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-9Z8Y71UeT6O7R0yYzi5fdQ), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-C42VKkB6QGuZMY49VDu6-Q), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-H0iL21rVSNehfW_0p9iTuQ), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-H4EDsFZQTBeXGYfJ9ZmRwA), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-HLWlsH9JTDei4fDe6Ivjmg), ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-J_UK3eR5TE-CaK4wpKSn_g)]
	at org.elasticsearch.repositories.s3.S3BlobStore.deleteBlobs(S3BlobStore.java:382)
	at org.elasticsearch.repositories.s3.S3BlobContainer.delete(S3BlobContainer.java:445)
	at org.elasticsearch.server@9.5.2/org.elasticsearch.repositories.blobstore.BlobStoreRepository$SnapshotsDeletion.lambda$cleanupUnlinkedRootAndIndicesBlobs$17(BlobStoreRepository.java:1607)
	at org.elasticsearch.server@9.5.2/org.elasticsearch.action.support.RefCountingListener$2.onResponse(RefCountingListener.java:203)
	at org.elasticsearch.server@9.5.2/org.elasticsearch.common.util.concurrent.AbstractThrottledTaskRunner$1.doRun(AbstractThrottledTaskRunner.java:141)
	at org.elasticsearch.server@9.5.2/org.elasticsearch.common.util.concurrent.ThreadContext$ContextPreservingAbstractRunnable.doRun(ThreadContext.java:1114)
	at org.elasticsearch.server@9.5.2/org.elasticsearch.common.util.concurrent.AbstractRunnable.run(AbstractRunnable.java:27)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1090)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:614)
	at java.base/java.lang.Thread.run(Thread.java:1516)
Caused by: software.amazon.awssdk.services.s3.model.InvalidRequestException: Missing required header for this request: Content-MD5 or x-amz-checksum-sha256 or x-amz-checksum-crc32c (Service: S3, Status Code: 400, Request ID: iad-1:qwSgPPcYL3gxbzhhh0uJG6AI1LrPpAf411dcBGxHXbBIDG3ydu7ZwTsjIvTTbW4V) (SDK Attempt Count: 1)
	at software.amazon.awssdk.services.s3.model.InvalidRequestException$BuilderImpl.build(InvalidRequestException.jav
```

## This is not a documentation gap

Object Storage documents the checksum algorithms it supports: CRC32C, SHA256
and SHA384, alongside MD5, which it has always used for integrity. CRC32 is not
in that list, and the 400 response names the same set the documentation does.
The product is behaving as documented.

So this is a parity request, not a bug report about undocumented behaviour.

Amazon S3 accepts CRC32, and CRC32C differs from it only in the polynomial. The
gap is narrow and its consequence is not: the AWS SDK for Java defaults to
CRC32 on checksum-required operations, so any client built on that SDK which
issues a batch delete fails against Object Storage. Elasticsearch is one such
client and offers no setting to change the algorithm.

## What we are asking for

Add `x-amz-checksum-crc32` to the algorithms accepted on `DeleteObjects`, for
parity with Amazon S3.

Every client that reaches the S3 Compatibility API through the AWS SDK for Java
v2.30.0 or later sends CRC32 by default. Supporting CRC32C but not CRC32 means
those clients fail on an operation they cannot configure their way out of, and
in the Elasticsearch case the failure is silent: the delete is reported as
successful and the objects remain.

If CRC32 is excluded deliberately, we would like to know the reason, so it can
be documented as a known incompatibility with SDK-default clients rather than
found the way we found it.

## Request IDs

Every `opc-request-id` above is from this tenancy and this bucket. The rejected
one is the request to examine.
