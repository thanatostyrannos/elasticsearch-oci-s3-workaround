# The service request raised with Oracle

Three files. `SERVICE-REQUEST.md` is the write-up,
`oci-deleteobjects-checksum-repro.py` is the evidence it rests on, and
`oci-deleteobjects-checksum-repro.sh` is the same reproduction in bash for
sites that have a RHEL shell and no Python they are permitted to run.

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

## The bash port

`oci-deleteobjects-checksum-repro.sh` exists because the reproduction is worth
nothing if the person able to run it cannot. A hardened RHEL host frequently
has no interpreter the operator is cleared to run against a production tenancy,
and asking Oracle's engineer to install one to see the defect is a request they
can decline.

It uses bash, coreutils, openssl, curl and gzip, all of which are in a base
RHEL install. No jq, no python, no awscli. SigV4 is inlined the same way, with
the HMAC chain done through `openssl dgst -mac HMAC -macopt hexkey:`, since the
intermediate keys are binary and cannot be passed as strings.

Two checksums have no tool in a base install:

- **CRC-32** is read back out of a gzip trailer. gzip already computes it and
  stores it little-endian in the last eight bytes of its own output, so this is
  exact rather than a reimplementation.
- **CRC-32C** is computed bit by bit in bash. Nothing in base RHEL knows the
  Castagnoli polynomial, and the body is 223 bytes, so the loop costs nothing.

Run it exactly as the Python one:

```
./oci-deleteobjects-checksum-repro.sh \
    --endpoint https://<namespace>.compat.objectstorage.<region>.oraclecloud.com \
    --region <region> --bucket <bucket> --credentials creds.json
```

### It was checked against the Python, not assumed equal to it

The four integrity header values are byte-identical between the two
implementations, which is the part that decides whether the request is the same
request:

| Header | Value both produce |
|---|---|
| `Content-MD5` | `IOs0m/cZIJxpX2hlh3K67w==` |
| `x-amz-checksum-crc32c` | `jihPpg==` |
| `x-amz-checksum-sha256` | `7+3sRPV871OHdSzmMR2pkgeyVwTQMEOQG3MGBmSI5rY=` |
| `x-amz-checksum-crc32` | `MVGIRg==` |

Both were then run back to back against the tenancy and both returned
200/200/200/400 with the same rejection message. Their transcripts differ only
in the two fields that cannot be equal, the send timestamp and the
`opc-request-id` OCI mints per request; masking those two lines makes the
output identical, sha256
`5d23eab80619262732589294a572e3c4d673fb961cc8455c1e6d06c02decbf78` for both.
The captured pair is in
[../../evidence/service-request-repro/](../../evidence/service-request-repro/).

## Reading it

The problem in service terms is [../problem-record.md](../problem-record.md).
What the endpoint accepts and rejects, measured, is
[../oci-s3-compatibility.md](../oci-s3-compatibility.md). The version boundary
and the SDK mechanism are in [../../FACTS.md](../../FACTS.md).
