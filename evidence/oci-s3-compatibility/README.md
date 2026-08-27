# What Oracle's S3 Compatibility API actually does

Measured against Oracle Cloud Infrastructure Object Storage, S3 Compatibility
API, region `us-ashburn-1`, on 2026-08-26. Bucket created by
[the Terraform in this repository](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/terraform/oci-probe/README.md),
versioning Disabled, and driven by `generation_chain.reclaim` rather than by
hand-built requests, so what is measured is the code that ships.

Everything below is a capture from a real request. The tenancy namespace and
local paths are replaced with placeholders; nothing else is edited.

## The fault, in Oracle's own words

`DeleteObjects` with the checksum the AWS SDK sends by default:

```
TransportError 400 from https://<namespace>.compat.objectstorage.us-ashburn-1.oraclecloud.com/esprobe-probe?delete=
<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Message>Missing required header for this request: Content-MD5 or x-amz-checksum-sha256 or x-amz-checksum-crc32c</Message>
  <Code>InvalidRequest</Code>
</Error>
```

Oracle names the three it accepts. **CRC32 is not among them, and CRC32 is what
the SDK sends.** That single line is the whole fault, and it is now measured on
Oracle rather than inferred from a MinIO release that reproduces it.

Full capture: [`delete-crc32.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/delete-crc32.txt)

## Every algorithm, same tool, same bucket

| `--checksum-algorithm` | result | capture |
|---|---|---|
| `crc32` | **0 deleted, 2 failed, 400 InvalidRequest** | [`delete-crc32.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/delete-crc32.txt) |
| `crc32c` | 2 deleted, 0 failed | [`delete-crc32c.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/delete-crc32c.txt) |
| `sha256` | 2 deleted, 0 failed | [`delete-sha256.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/delete-sha256.txt) |
| `md5` | 2 deleted, 0 failed | [`delete-md5.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/delete-md5.txt) |

**The batch delete is not the fault.** Three of four algorithms work. The client
picks the fourth.

### What this means upstream

`.checksumAlgorithm(ChecksumAlgorithm.CRC32_C)` in `S3BlobStore.bulkDelete`
emits an algorithm Oracle accepts, and needs no new dependency. That was a
proposal resting on Oracle's documentation. It now rests on a measurement.

Elastic's published position is that the storage vendor should fix this. The
storage vendor accepts three algorithms. The client sends a fourth.

## The whole thing, end to end, through Elasticsearch

The measurements above drive the transport directly. This one is Elasticsearch
9.5.2 registering a snapshot repository against the same bucket, which runs the
same batch delete inside its own verification:

```
[oci-repo] cannot delete test data at [snapshots]
  Failed to delete blobs [ObjectIdentifier(Key=snapshots/tests-<uuid>/data-<uuid>.dat),
                          ObjectIdentifier(Key=snapshots/tests-<uuid>/master.dat)]
    Missing required header for this request:
    Content-MD5 or x-amz-checksum-sha256 or x-amz-checksum-crc32c
    (Service: S3, Status Code: 400, Request ID: iad-1:...)
```

That is the failure this repository was written about, produced by Elasticsearch
itself against Oracle. Registration fails, and the two test objects it wrote
stay in the bucket because the call that removes them is the one that breaks:

```
snapshots/tests-<uuid>/data-<uuid>.dat    22 bytes
snapshots/tests-<uuid>/master.dat         22 bytes
```

Registering again with `?verify=false` returns `{"acknowledged":true}`, and the
leak begins.

Capture: [`register-with-verify.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/register-with-verify.json)

## Elasticsearch cannot write to OCI at all without one setting

Before reaching the delete, the first attempt failed earlier still:

```
Unable to upload object [snapshots/tests-<uuid>/master.dat] using a single upload
  AWS chunked encoding not supported. (Service: S3, Status Code: 501)
```

Not a delete. An upload. Elasticsearch's bundled AWS SDK sends chunked encoding
on `PutObject` and Oracle answers 501. Nothing can be written, so the repository
is unusable before the delete bug is even reachable.

The fix is one client setting, confirmed present in 9.5.2 by unpacking
`S3ClientSettings.class` rather than by trusting documentation:

```yaml
s3.client.<name>.disable_chunked_encoding: true
s3.client.<name>.path_style_access: true
s3.client.<name>.endpoint: <namespace>.compat.objectstorage.<region>.oraclecloud.com
s3.client.<name>.protocol: https
```

**Anyone pointing Elasticsearch at OCI hits this before they hit the delete
bug**, and it is the same root cause: AWS tooling adding a payload feature to
every request that an S3-compatible store refuses. The AWS CLI needs
`AWS_REQUEST_CHECKSUM_CALCULATION=when_required` for the same reason.

## ListObjectsV2 is genuinely supported

Oracle's published operations list names only `ListObjects`, so this was worth
measuring. Three objects, `--max-keys 2`:

```json
{
    "IsTruncated": true,
    "Contents": [ ... 2 objects ... ],
    "KeyCount": 2,
    "MaxKeys": 2,
    "NextContinuationToken": "..."
}
```

`KeyCount` and `NextContinuationToken` are V2 fields. Their presence, with
`IsTruncated`, means V2 rather than V1 answering to a V2 name. Paginated
listing works.

Full capture: [`listv2-maxkeys2.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/listv2-maxkeys2.json)

## Both Oracle APIs reach the same repository

Everything above uses the S3 Compatibility API, because that is the only surface
Elasticsearch can speak: its `repository-s3` plugin talks S3 and nothing else.
The audit also has a native transport, and it was worth confirming the two agree
about what a repository is.

```
transport: oci, OCI native Object Storage at
https://objectstorage.us-ashburn-1.oraclecloud.com,
namespace <namespace>, bucket esprobe-repo, prefix ocirig/
```

No `.compat.` in that hostname. That is Oracle's own API, authenticated with RSA
request signing against a `keyId` of tenancy, user and fingerprint, rather than
SigV4 against a Customer Secret Key. It read the same repository, built the same
chain, applied the Elasticsearch veto and produced a report of the same shape.

Capture: [`native-transport.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/native-transport.txt)

**The two have not been compared under controlled conditions.** Runs minutes
apart against a repository being written to disagree about how many objects are
orphaned, which says the repository moved rather than that the transports
differ. A real comparison needs both run back to back against a quiet
repository, and that has not been done.

## Three things that cost time and appear in no documentation

### The AWS CLI cannot write to OCI with its current defaults

```
An error occurred (NotImplemented) when calling the PutObject operation:
AWS chunked encoding not supported.
```

Recent AWS SDKs and CLI v2 add streaming checksums to every request by default.
Oracle rejects the chunked encoding that comes with them.

```bash
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
```

That is the same shape of problem as the fault this repository documents: AWS
tooling adding a payload feature to every request, and an S3-compatible store
refusing it. Two separate defaults, one root cause.

Full capture: [`aws-cli-default.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/aws-cli-default.txt)

### A new Customer Secret Key is not usable immediately, and says the wrong thing

```
403 SignatureDoesNotMatch
The secret key required to complete authentication could not be found.
The region must be specified if this is not the home region for the tenancy.
```

The message blames the region. The region was correct. The key was about
thirty seconds old, and the same request succeeded roughly twenty-five seconds
later.

This one is worth naming because it also appeared **mid-run**, once, on a key
that had been working. Retry once before believing a signing failure. A
credential that is right and a credential that has not propagated produce the
same error.

### Identity Domains refuses to create a user without an email

```
400-IdcsConversionError
"detail": "The primary email must be specified."
"messageId": "error.identity.user.primaryEmailNotSpecified"
```

New tenancies use Identity Domains. The classic IAM API never asked for an
email, so a configuration written against the older behaviour fails here, and
it fails **after** the buckets already exist. Terraform resumes cleanly on a
re-run.

The same tenancy shape shows up elsewhere: `oci iam user get` returns 401 and
`oci iam tenancy get` returns 503, while every Object Storage call succeeds with
the same credentials. The legacy identity endpoints will not answer for a user
that lives in a domain. Object Storage is unaffected.

## Reproducing this

```bash
cd terraform/oci-probe
cp terraform.tfvars.example terraform.tfvars   # fill it in
terraform apply
```

Then drive the reclaim tool at the probe bucket with each
`--checksum-algorithm` in turn. The whole matrix costs a few dozen requests,
well inside the 50,000 a month that Always Free covers.
