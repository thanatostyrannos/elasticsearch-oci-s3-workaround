# Security scan artifacts

Raw output from static application security testing, and the reports built
from it. Committed so a finding can be rechecked against the exact tree it was
found in, rather than taken on trust from a summary.

Each scan records the commit it ran against. A report without a SHA is not
evidence, because this repository changes and a line number moves.

## What belongs here

- Raw scanner output, one file per tool, machine readable.
- The evaluation report built from that output.
- The control-by-control assessment, where one was produced.

## Redaction, and why it is not optional

Scanners quote the string they matched. The tracked tree deliberately contains
credential-shaped strings that are synthetic, including a throwaway RSA key in
`tests/fixtures/genchain-oci-signing-vector.json` that exists only to pin the
OCI request-signing vector, and AWS's own published example key pair in the
SigV4 tests. Raw output copied here verbatim would carry those values into a
second file that carries no explanation of why they are harmless.

So artifacts here keep the finding and drop the value. Rule, file, line,
severity and message stay. The matched secret is replaced with
`<<<Redacted>>>`. Anyone who needs the value can read the file the finding
points at.

This is also a practical requirement rather than a preference:
`tests/test_no_credentials_committed.py` scans every tracked file, and it
exempts the signing-vector fixture by path. A raw artifact reproducing that
key under a different path is not exempt and turns the suite red.

## Scanning the right tree

Scan a `git archive` export, never the working directory. The working
directory holds real Oracle credentials under `terraform/oci-probe/`
(`terraform.tfstate`, `creds.json`, `oci_api_key.pem`, `terraform.tfvars`).
They are gitignored and have never been committed, which is the correct
posture, but a filesystem scan pointed at the working directory would copy a
live private key into an artifact destined for this folder.

    git archive --format=tar <ref> | (mkdir -p /tmp/scan/src && tar -x -C /tmp/scan/src)
