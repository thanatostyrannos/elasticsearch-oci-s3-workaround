# Cybersecurity evaluation report

**System:** Elasticsearch OCI S3 Workaround Toolkit
**Commit assessed:** `997170e`, branch `testing-protocol`
**Date:** 2026-08-27
**Scope:** `git archive` export of that commit. Tracked files only, 530 files,
135 Python modules, 20,246 lines scanned by Bandit.

## Overall risk: MODERATE

No real secret was found. The only secret-scanner hits are the synthetic
signing-vector fixture, which is pre-disclosed and authenticates nothing. There
is no SQL surface anywhere, because there is no database. The one genuinely
destructive capability, deleting objects from a snapshot repository that Oracle
offers no versioning-based recovery for, is gated by a control that was read at
source rather than assumed: `reclaim/approval.py` requires the operator to
supply both a SHA-256 digest of the exact manifest bytes and a matching row
count before any delete is built, and dry run is the unconditional default.

Against that, three findings sit at CAT I and all three share one root cause:
the S3 compatibility transport does not enforce TLS. It takes whatever scheme
the operator's `--endpoint` carries and never refuses `http://`. That needs an
operator or configuration error to bite, but it is real and it is cheap to fix.

**Recommendation: Interim Authority to Operate, 180 days, conditioned on
closing the CAT I items within 30 days.** Full authority should follow a
re-scan, plus organisational evidence this source-only review could not reach.

## Methodology

| Tool | Version | Invocation | Rules |
|---|---|---|---|
| Bandit | 1.9.4 | `bandit -r /src -f json -o bandit_raw.json --severity-level low` | full default plugin set |
| Semgrep OSS | 1.174.0 | `semgrep scan --config=auto /src --json` | `auto`, 431 rules over 480 files |
| Trivy | 0.74.0 | `trivy fs /src --format json` | default fs scanners |

Two adaptations, both recorded rather than quietly substituted.

`pyfound/bandit:latest` would not pull, `insufficient_scope: authorization
failed`, so Bandit ran from `python:3.12-slim` with `pip install bandit`. Then
`--level low` was rejected: 1.9.4 renamed it to `--severity-level`.

Trivy is the one worth reading carefully. The specified whole-tree invocation
exited 0 with **zero findings**, which is exactly what an empty `/src` looks
like. It was not accepted at face value. Four reproductions across two pods,
one with four times the resources and a fresh vulnerability database, plus a
two-file control proving the secret scanner does detect the fixture at small
scope, established that the scanner was silently skipping `tests/` at full
scope for a reason that was never root-caused. Re-running the same command
per top-level directory, eight invocations, recovered the two real findings.
Both results are kept. Treat the whole-tree zero as a live tool limitation in
this environment, not as a clean bill of health.

**Excluded by construction:** the five real Oracle credential files under
`terraform/oci-probe/`. They are gitignored, have never been committed, and
were verified absent from the export before it left the local filesystem. That
is a positive control, not a redaction after the fact.

Raw counts: Bandit 74 (7 high, 16 medium, 51 low), Semgrep 13, Trivy 2.

## Triaged, not findings

| Location | What | Why benign |
|---|---|---|
| `tests/fixtures/genchain-oci-signing-vector.json` | real-format RSA private key, Trivy's only two hits | committed to pin the OCI signing vector, authenticates nothing, never uploaded to a tenancy |
| `generation_chain/selftest.py`, `tests/test_generation_chain_signing.py` | `AKIDEXAMPLE`, `wJalrXUtnFEMI/...` | AWS's own published SigV4 documentation examples |
| `tests/test_no_credentials_committed.py` | PEM headers, OCID regexes | the file's own detection patterns; it is the credential scanner |
| `terraform/oci-probe/terraform.tfvars.example` | placeholder OCIDs, sequential-hex fingerprints | synthetic, and short enough to fail the scanner's own length rule |

A sixth entry was briefed to the scanning agent and carried into its report:
a test writing a key file whose body is the word `nope`. That file was deleted
from this repository and survives only in history, so it cannot be a finding
against this tree. It was caught by `tests/test_doc_paths_and_links.py`, which
refuses a document naming a path the repository does not have.

## CAT I

### 1. The S3 transport does not require TLS

`generation_chain/sources/s3.py:53-58` takes the scheme from the endpoint URL
and stores it. Nothing refuses `http://`.

An operator supplies `--endpoint`. A copy-paste slip, a stale test
configuration promoted to production, or a tampered config pipeline sends every
request in the clear. The secret key itself is never transmitted, since SigV4
sends a derived HMAC, so this is not direct key disclosure. What does travel is
the manifest content, which enumerates exactly which production objects are
about to be deleted.

Fix: refuse a non-`https` scheme. See the note on loopback below.

### 2. XML from the store is parsed without limits

`generation_chain/sources/s3.py:136` and `generation_chain/reclaim/batch.py:96`
parse store responses with `xml.etree.ElementTree.fromstring`.

**Tested rather than asserted, on the Python this runs under, 3.12.3:**

- Billion laughs: **expanded**, a short payload reaching 30,000 characters.
- External entity referencing `file:///etc/passwd`: **refused**,
  `ParseError: undefined entity`.

So the denial-of-service half is real and the file-disclosure half is not.
ElementTree does not resolve external entities. Any recommendation framing this
as XXE is overstated for this runtime.

The realistic delivery path is a malicious or intercepted endpoint, which is
finding 1. It matters because this parser feeds the enumeration that decides
what gets condemned.

Fix, without a dependency: reject a `DOCTYPE` before parsing. Entity expansion
attacks require an internal DTD, and a legitimate S3 listing response never
carries one, so the check costs nothing. This keeps the project standard
library only, which `defusedxml` would end.

## CAT II

### 3. Entity-expansion denial of service

Same root cause and same fix as finding 2.

### 4. MD5 without `usedforsecurity=False`

`generation_chain/reclaim/checksum.py:89` calls `hashlib.md5(body)`.

Context matters for severity. This is one of four checksum algorithms offered
because the S3 API itself accepts `Content-MD5`. It is a data-integrity choice,
not a security hash, and it plays no part in the delete authorisation digest,
which is SHA-256 in `approval.py`. It is still a real defect: on a host with
OpenSSL in FIPS mode this call raises at runtime.

Fix: `hashlib.md5(body, usedforsecurity=False)`.

## CAT III

- `urllib.request.urlopen` with no scheme allowlist across seven scripts. Each
  builds its URL from operator configuration, the same trust level as the
  tool's own settings, so realistic exploitability is low. Worth an allowlist
  as defence in depth.
- `snapshot_churn_rig.py:142` builds an unverified SSL context. Not on the
  delete path, but nothing should ship unverified TLS.
- `assert` used for structural checks in three modules. These guard invariants
  already established by preceding control flow rather than security decisions,
  but `assert` is stripped under `-O`.

## Reviewed and not exploitable

`tests/genchain_repo.py:452` trips Bandit's `tarfile_unsafe_members`, which
fires on any `extractall`. This one validates every member path for absolute
paths and `..` traversal first and raises before extracting. That is the
correct mitigation for the class the rule exists to catch.

## Controls verified by reading the source

- **The delete gate holds.** `reclaim/cli.py` requires `--approve-digest` and
  `--approve-rows`, both checked in `approval.py` against the exact manifest
  bytes, before a credential is loaded or a request built. `--execute` without
  them raises first. Dry run is the default and has no bypass.
- **No credential is accepted on a command line.** Only `--credentials FILE`, a
  path, and the file is refused if group or world readable.
- **Redaction is structural.** The `Secret` wrapper renders `<secret>`
  everywhere except two `reveal()` sites, both signing. Every call site read.
- **No third-party runtime dependency exists.** No dependency manifest of any
  kind. Every import under `generation_chain/` is standard library, so there is
  no supply-chain surface that a scanner could have missed.
- **The credential scanner was hardened in `f5d98c7`** to catch the OCI
  credential pair this project actually signs with, which it previously missed
  entirely.

## Plan of action and milestones

| # | Finding | CAT | Due | Action |
|---|---|---|---|---|
| 1 | S3 transport does not require TLS | I | 30 days | refuse a non-https scheme, with a loopback carve-out; regression test that `http://` to a remote host is refused |
| 2 | XML parsed without entity limits | I | 30 days | reject a `DOCTYPE` before parsing in `s3.py` and `reclaim/batch.py`; test with a billion-laughs body |
| 3 | MD5 without `usedforsecurity=False` | II | 90 days | one line in `checksum.py`; verify on a FIPS host if one is available |
| 4 | Unverified SSL context in the churn rig | III | 90 days | remove it, or gate it behind an explicit flag that warns |
| 5 | `urlopen` with no scheme allowlist | III | 90 days | allowlist at each of the seven call sites |

### A carve-out that items 1 and 2 both need

Enforcing `https` unconditionally would break local testing. The MinIO rig
serves plain HTTP on loopback, and so does the suite's own in-process S3 server
in `tests/s3rig.py`. A blanket refusal makes the whole offline suite
unrunnable, which trades a real capability for a threat that does not exist on
a loopback socket.

So the refusal should carve out loopback hosts and refuse plain HTTP to
anything else. That closes the footgun that actually bites, an endpoint typed
or configured wrongly against a real store, without ending local testing.

---

# Rescan, 2026-08-27, against the release archive

The first scan covered the repository. This one covers **what actually ships**,
unpacked from the release archive, which is the tree a user receives. It was
run after the three findings above were closed.

Bandit 1.9.4, 53 Python files, 8,524 lines.

| | First scan (repository) | This scan (release) |
|---|---|---|
| HIGH | 7 | **0** |
| MEDIUM | 16 | 8 |
| LOW | 51 | 13 |

The HIGH count going to zero is mostly not a code change. Every HIGH in the
first scan was in `tests/`, and `tests/` does not ship: the RSA key pinning the
OCI signing vector, the AWS documentation example key pair, and the credential
scanner's own detection patterns. Scanning the repository reported findings in
fixtures nobody receives.

## What remains, and why

**MEDIUM, B314 and LOW B405, three sites.** `xml.etree.ElementTree` parsing
store responses. The denial of service this rule exists for is now refused
before parsing: both readers reject a body carrying a DOCTYPE, which is what
entity expansion requires and what a real S3 response never contains. Bandit
flags the call site regardless of the guard in front of it. Recorded as
mitigated rather than open, and the guards are pinned by neuter cases.

**MEDIUM, B323, one site.** `snapshot_churn_rig.py:142` builds an unverified
SSL context. It sits behind `--insecure`, which is `store_true` and therefore
off unless asked for, and its help says it is for lab clusters with
self-signed certificates. This file now ships, which it did not at the first
scan, so the finding is disclosed rather than inherited quietly. The default
path uses `ssl.create_default_context`.

**MEDIUM, B310, four sites.** `urllib.request.urlopen` without a scheme
allowlist, in the harness, the rig and the restore check. Each builds its URL
from operator-supplied configuration, the same trust level as the tool's own
settings, so a scheme attack needs an attacker who already controls the
config. Still worth an explicit allowlist as defence in depth, and still open.

**LOW, B101, three sites.** `assert` used for structural checks. The one that
mattered is gone: the transport's method allowlist was an assert, `python3 -O`
strips assert, and a DELETE reached the transport under it. That is now a raise
of an error outside the tree that read-failure handling catches. The three
remaining guard invariants already established by preceding control flow.

**LOW, B105, four sites.** Environment variable names such as
`AWS_SECRET_ACCESS_KEY` and `GENCHAIN_ES_PASSWORD`, plus AWS's published
example key in the signing self-test. Names, not values.

Raw output:
[`scans/bandit_release_2026-08-27.json`](scans/bandit_release_2026-08-27.json),
redacted for key material and local paths.

## What this rescan did not cover

Semgrep and Trivy were not re-run. Neither is installed on this host and
installing them was not worth blocking the release for, given the first run's
Semgrep findings were in test fixtures that no longer ship and its Trivy
findings were the signing-vector key, also gone. Say so rather than imply a
three-tool rescan happened.

The GitHub Actions workflow in `.github/workflows/security-scan.yml` runs all
three against the built package on every push, which is the durable answer to
this rather than a scan done by hand.
