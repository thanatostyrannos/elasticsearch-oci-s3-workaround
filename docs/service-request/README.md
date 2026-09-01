# The service request raised with Oracle

Two files. `SERVICE-REQUEST.md` is the write-up, and
`oci-deleteobjects-checksum-repro.py` is the evidence it rests on.

## Why the reproduction matters more than the write-up

The script sends the same `DeleteObjects` body to the same bucket with the same
credentials four times, changing only the integrity header:

| Header | Result |
|---|---|
| `Content-MD5` | 200 |
| `x-amz-checksum-crc32c` | 200 |
| `x-amz-checksum-sha256` | 200 |
| `x-amz-checksum-crc32` | **400 InvalidRequest** |

Three accepted and one rejected, seconds apart. CRC32C differs from CRC32 only
in the polynomial. That rules out a malformed request, a bad credential and a
permissions problem before anyone can propose them, which is most of what a
first-line response would otherwise ask you to check.

Object Storage documents the algorithms it accepts, CRC32C, SHA256 and SHA384
alongside MD5, and CRC32 is not among them, so the product is behaving as
specified. The request is for parity with Amazon S3, which does accept CRC32.
The argument is that the AWS SDK for Java sends CRC32 by default and the
clients built on it cannot be configured to send anything else.

It targets keys that do not exist. `DeleteObjects` on an absent key succeeds on
both S3 and OCI, so a run deletes nothing: the request is rejected before the
store looks at the keys at all.

Standard library only, with the SigV4 signing inlined, so it runs against any
tenancy with nothing installed:

```
./oci-deleteobjects-checksum-repro.py \
    --endpoint https://<namespace>.compat.objectstorage.<region>.oraclecloud.com \
    --region <region> --bucket <bucket> --credentials creds.json
```

Credentials come from a JSON file holding `s3.access_key_id` and
`s3.secret_access_key`, never from the command line.

## What was removed from the copy kept here

The tenancy OCID and the Object Storage namespace are replaced with
placeholders. Oracle needs both and the version sent to them carries both; a
public repository does not.

The `opc-request-id` values are kept. They are opaque server-side handles
rather than credentials, and they are the thing Oracle looks up.

An Elasticsearch log capture was also collected and is deliberately not here.
It carried the cluster uuid, node id and node name on every line, none of which
Oracle needs, and the stack trace it contained is quoted in the write-up.

## Reading it

The problem in service terms is [../problem-record.md](../problem-record.md).
What the endpoint accepts and rejects, measured, is
[../oci-s3-compatibility.md](../oci-s3-compatibility.md). The version boundary
and the SDK mechanism are in [../../FACTS.md](../../FACTS.md).
