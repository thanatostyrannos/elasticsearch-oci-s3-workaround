# Correction to the submitted service request

Paste the section below into the SR as an update. It identifies which parts of
what was submitted are not ours to stand behind, so that no one spends time
looking up identifiers that will not resolve.

---

## Correction: environment details and request IDs in the original submission

The text submitted for this SR was pasted verbatim from our public write-up of
the problem. That document is a template. It was published to describe the
defect, not to describe the affected environment, and the fields that identify a
tenancy were never filled in. Please disregard the following, all of it, before
anything is looked up.

**Live data from the affected tenancy to follow.** We will re-run the
reproduction in that tenancy and send the real namespace, tenancy OCID, bucket
and a fresh set of `opc-request-id` values as a follow-up update to this SR.

### 1. The Environment table is unfilled or belongs to our lab

| Field as submitted | Status |
|---|---|
| `Object Storage namespace: <namespace>` | Unsubstituted placeholder. Not a value. |
| `Endpoint: https://<namespace>.compat.objectstorage.us-ashburn-1.oraclecloud.com` | Unsubstituted placeholder. |
| `Tenancy OCID: <tenancy-ocid>` | Unsubstituted placeholder. Not a value. |
| `Bucket: esprobe-repo` | Real, but it is our lab bucket, not the affected one. |
| `Region: us-ashburn-1` | Correct for our lab. Confirm against the live data to follow. |
| `Elasticsearch 9.5.2`, `AWS SDK for Java 2.31.78` | The versions we reproduced on. Confirm against the live data to follow. |

### 2. Every `opc-request-id` in the submission is from our lab tenancy

All six are from a separate reproduction tenancy of ours, not from the affected
environment:

```
iad-1:rq7gfJv2hh8uOTl9GamelXx-LLbP6LdL4PWbWQI1hKnXhTNscm6yAQi4yHdB915F
iad-1:Q2AK_lGKXviu3sLP52hmKjRXgBfrohdZVQ1LzU6NXxcNnWlZsmn5GDr5qmgPabC8
iad-1:c5GkrGLPV2F6fPTR2IC2utnp3ztkj7xPBrR4kUCevrqOBz7xCkIyaIW4OOlxzaGj
iad-1:h35-GvOCtr5Uft5wy9lFXcMCXyt-zGgylUftfe6RdKC0SUEW9mIWLHJGpot-VNBD
iad-1:b5ImJOs_hrrp0KEm1P0ZMk8j_dBRo0e20SGD_zJQkbZaWygg-bkfigsGMjJwCdkF
iad-1:qwSgPPcYL3gxbzhhh0uJG6AI1LrPpAf411dcBGxHXbBIDG3ydu7ZwTsjIvTTbW4V
```

Two further points about them:

- The closing line of the submission reads "Every `opc-request-id` above is from
  this tenancy and this bucket." That sentence was true of our write-up and is
  not true of this SR. Please strike it.
- That lab tenancy has since been torn down. Its buckets, IAM user and
  credentials no longer exist. Depending on retention, these IDs may resolve to
  a deleted tenancy or not resolve at all. Either way they will not tell you
  anything about the affected environment.

### 3. The stack trace blob is not correct for this SR

This is the block submitted under "Stack trace from the Elasticsearch log",
beginning:

```
java.io.IOException: Failed to delete blobs [ObjectIdentifier(Key=ociprobe10/indices/J_zstkVNRJCUoNrBf_UxMQ/0/index-2ConvGUcSPq_EhZAT7A3-Q), ...
```

Please disregard that blob entirely. Four separate problems with it:

1. **The object keys are ours.** `ociprobe10` is the base path of our lab rig's
   repository, and `J_zstkVNRJCUoNrBf_UxMQ` is an index UUID from our cluster.
   Neither exists in the affected environment.
2. **It is a different event from the API response printed directly above it.**
   The submission introduces both as one reproduction, "Reproduced at
   2026-09-01T14:25:23.632Z by registering a snapshot repository." That
   describes the JSON response, which is a repository verification failure on
   repository `[srproof]` against keys under `snapshots/tests-KVNM8d9hRgOgFlV7PSERSg/`.
   The stack trace is not that. It comes from
   `BlobStoreRepository$SnapshotsDeletion.cleanupUnlinkedRootAndIndicesBlobs`,
   which is snapshot deletion, and its keys are under `ociprobe10/indices/`.
   Two different operations, two different runs, presented as one. Reading them
   as a single trace will mislead.
3. **Its request ID is a third, unrelated request.**
   `iad-1:qwSgPPcYL3gxbzhhh0uJG6AI1LrPpAf411dcBGxHXbBIDG3ydu7ZwTsjIvTTbW4V`
   is not the request in the JSON response above it, which is
   `iad-1:b5ImJOs_hrrp0KEm1P0ZMk8j_dBRo0e20SGD_zJQkbZaWygg-bkfigsGMjJwCdkF`.
4. **It is truncated mid-token.** The final line ends
   `...build(InvalidRequestException.jav` with the rest cut off. It is not a
   complete trace.

The one part of that section worth keeping is the innermost cause, which is the
same string the service returns and is not environment specific:

```
software.amazon.awssdk.services.s3.model.InvalidRequestException:
Missing required header for this request: Content-MD5 or x-amz-checksum-sha256
or x-amz-checksum-crc32c (Service: S3, Status Code: 400)
```

### 4. What is not affected by any of the above

The technical claim stands and does not depend on any identifier we got wrong:

- `DeleteObjects` with `x-amz-checksum-crc32` returns HTTP 400 `InvalidRequest`.
- The same request with `Content-MD5`, `x-amz-checksum-crc32c` or
  `x-amz-checksum-sha256` returns 200.
- CRC32C differs from CRC32 only in the polynomial, and CRC32C is accepted.
- The AWS SDK for Java has defaulted to CRC32 on checksum-required operations
  since v2.30.0, and Elasticsearch exposes no setting to change it.

This is reproducible on demand in any tenancy, which is what the next section is
for. It remains a parity request rather than a bug report: the accepted
algorithms are documented and CRC32 is not among them.

### 5. How we will give you correct request IDs

Attached is `oci-deleteobjects-checksum-repro.sh`. It sends the same
`DeleteObjects` body to the same bucket with the same credentials four times,
changing only the integrity header, and prints what the service returned each
time.

```
./oci-deleteobjects-checksum-repro.sh \
    --endpoint https://<namespace>.compat.objectstorage.<region>.oraclecloud.com \
    --region <region> --bucket <bucket> --credentials creds.json
```

Its output is exactly the data this SR needs and did not receive: the endpoint,
bucket and region actually used, the sha256 of the request body so you can
confirm all four requests carried identical bytes, and for each attempt the
status, the UTC send time and the `opc-request-id` the service returned.

Three properties worth noting:

- **It runs on a base RHEL host with nothing installed.** bash, coreutils,
  openssl, curl and gzip only. No Python, no `jq`, no AWS CLI. SigV4 is inlined.
  This matters because the hosts that can reach the affected tenancy are
  hardened and cannot have an interpreter added to them.
- **It deletes nothing.** The keys it names do not exist. `DeleteObjects` on an
  absent key succeeds on both S3 and Object Storage, so a run that reaches the
  store removes nothing, and the failing case is rejected before the keys are
  examined at all. It is safe to run against a production bucket.
- **Credentials come from a JSON file**, never from the command line, so they do
  not land in shell history or in a process listing.

A Python version producing byte-identical output is also available if that is
easier for you to run. We verified the two against each other: the four checksum
header values are identical, and both return 200/200/200/400 with the same
rejection message, differing only in the send timestamp and the per-request
`opc-request-id`.

### 6. What we are asking for, restated

Add `x-amz-checksum-crc32` to the algorithms accepted on `DeleteObjects`, for
parity with Amazon S3. If it is excluded deliberately, we would like the reason,
so it can be documented as a known incompatibility with SDK-default clients.

Please hold on lookups until the live data arrives. Everything identifying in
the original submission is either an unfilled placeholder or ours.
