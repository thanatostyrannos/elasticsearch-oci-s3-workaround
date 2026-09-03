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

### 6. Reproducing it in your own sandbox

You do not need our tenancy, our bucket, or any of the identifiers this
correction just disowned. The attached script reproduces the defect standalone
in any tenancy, in any region, in about five seconds. It is four HTTPS requests.

You also do not need Elasticsearch. Elasticsearch is how we hit this, not what
causes it. The script talks to the S3 Compatibility API directly, so the
reproduction is entirely inside Object Storage.

**What you need**

1. Any bucket you can write to. An empty scratch bucket is ideal, but any bucket
   works, because nothing is written to it and nothing is deleted from it.
2. A Customer Secret Key for a user with access to that bucket. In the Console:
   Identity, Users, the user, Customer Secret Keys, Generate Secret Key. That
   gives you an access key and a secret. The secret is shown once.
3. Your Object Storage namespace, from the Object Storage page or
   `oci os ns get`.

**Credentials file**

Put the pair in a JSON file. The script reads it from disk and never accepts
credentials on the command line, so they do not reach shell history or a process
listing.

```json
{
  "s3": {
    "access_key_id": "<the access key>",
    "secret_access_key": "<the secret>"
  }
}
```

**Run it**

```
chmod +x oci-deleteobjects-checksum-repro.sh

./oci-deleteobjects-checksum-repro.sh \
    --endpoint https://<namespace>.compat.objectstorage.<region>.oraclecloud.com \
    --region <region> --bucket <your-bucket> --credentials creds.json
```

Use the standard domain shown above. It works in every realm. The dedicated
domain, `<namespace>.compat.objectstorage.<region>.oci.customer-oci.com`, also
works but only in the commercial realm OC1. `--region` must be the region the
bucket is in, because it is what SigV4 signs with.

**What you should see**

Three accepted and one rejected, within about a second of each other:

```
Content-MD5              200 ACCEPTED
x-amz-checksum-crc32c    200 ACCEPTED
x-amz-checksum-sha256    200 ACCEPTED
x-amz-checksum-crc32     400 REJECTED
  response        : ...<Message>Missing required header for this request:
                    Content-MD5 or x-amz-checksum-sha256 or
                    x-amz-checksum-crc32c</Message><Code>InvalidRequest</Code>...
```

Each attempt also prints its `opc-request-id`, so the run gives you four handles
in your own tenancy that you can look up directly.

**Why this framing is the whole argument**

The four requests are byte-identical apart from the integrity header. The script
prints the sha256 of the request body once, at the top, and every attempt sends
that same body to the same bucket with the same credentials. So the three
successes rule out a malformed request, a bad credential, a permissions problem
and a wrong endpoint before any of them can be proposed. The only variable left
is the algorithm named in the header, and CRC32C, which is accepted, differs
from CRC32, which is not, only in the polynomial.

**Safety**

The keys it names, `does-not-exist/probe-a` and `does-not-exist/probe-b`, do not
exist. `DeleteObjects` against an absent key is a success on both Amazon S3 and
Object Storage, so the three accepted requests delete nothing, and the rejected
one is refused before the keys are examined at all. Nothing in the bucket is
read, written or removed. It is safe against a bucket holding real data, though
a scratch bucket keeps it obviously so.

**Dependencies**

bash, coreutils, openssl, curl and gzip. All are in a base RHEL install. There
is no Python, no `jq` and no AWS CLI. SigV4 is inlined in the script, the HMAC
chain running through `openssl dgst -mac HMAC -macopt hexkey:`. CRC-32 is read
out of a gzip trailer, since gzip computes it already, and CRC-32C is computed
in the script, since no base RHEL tool knows the Castagnoli polynomial. If you
would rather run Python, an equivalent is available on request; we verified the
two produce identical checksum values and identical output.

### 7. What we are asking for, restated

Add `x-amz-checksum-crc32` to the algorithms accepted on `DeleteObjects`, for
parity with Amazon S3. If it is excluded deliberately, we would like the reason,
so it can be documented as a known incompatibility with SDK-default clients.

Please hold on lookups until the live data arrives. Everything identifying in
the original submission is either an unfilled placeholder or ours.
