# Application Security and Development STIG assessment

**Baseline:** DISA Application Security and Development STIG, version 6
release 4, published 2025-09-09, 286 requirements.
**System:** Elasticsearch OCI S3 Workaround Toolkit.
**Commit assessed:** `997170e`, branch `testing-protocol`.
**Date:** 2026-08-27.

Rule identifiers were checked against the published baseline rather than
recalled. Where a control could not be adjudicated from a static scan of one
commit, it is marked Not Reviewed. Nothing was guessed into Not a Finding.

## Summary

| Status | Count |
|---|---|
| Open | 0 |
| Not a Finding | 16 |
| Not Applicable | 113 |
| Not Reviewed | 157 |
| **Total** | **286** |

| CAT | Open | Not a Finding | Not Applicable | Not Reviewed | Total |
|---|---|---|---|---|---|
| I | 3 | 4 | 15 | 12 | 34 |
| II | 4 | 5 | 89 | 132 | 230 |
| III | 0 | 0 | 11 | 11 | 22 |

## The machine-readable checklist

[`elasticsearch-oci-s3-workaround.cklb`](elasticsearch-oci-s3-workaround.cklb)
opens in STIG Viewer. It is built from DISA's own Application Security and
Development STIG, V6R4, benchmark dated 01 Oct 2025, so every rule carries
DISA's title, discussion, check content and fix text rather than anything
restated here.

**It records 16 determinations and leaves 270 not reviewed.** That does not
match the summary table above, and the difference is deliberate.

The 16 are the controls this assessment examined against the source and wrote
down a basis for. Each one carries that basis in its finding details, naming
the file and the mechanism it rests on, so a reviewer can check the claim
rather than take it.

The 113 counted as Not Applicable above were judged during the assessment but
never individually recorded. A count is not a determination. Asserting 113 Not
Applicable in a file an assessor imports, without a per-rule justification
behind any of them, would put a number in front of someone that nothing
supports. They stay not reviewed until each one has a reason written next to
it.

So the checklist understates what was looked at and overstates what remains.
That is the safe direction for an artifact somebody else signs.

## On the size of the Not Applicable bucket

This is a headless, single-operator command line tool. It has no interactive
login, no web front end, no cookies or session identifiers, no SOAP or SAML
interface, and no database. A large part of this STIG presumes a multi-user
interactive web application: session management, password complexity and
rotation, PIV and FICAM authentication, SAML assertions, XSS and CSRF, SQL
injection, account lifecycle audit logging. Each Not Applicable row was checked
against the control's actual subject matter and this codebase's architecture.
It is not a blanket exemption by category.

## On the size of the Not Reviewed bucket

It is the largest, deliberately. Audit log retention and centralisation,
configuration management board process, FIPS mode attestation for the deployed
OpenSSL build, DoD PKI trust anchor configuration, penetration testing cadence
and programme documentation cannot be honestly adjudicated from one static
scan. Marking them Not a Finding without that evidence would be the failure
this assessment exists to avoid.

## Closed since the assessment

All seven Open findings were fixed and pinned. Each was checked against this
runtime first, and one was found to be half wrong: the XML rule is usually
paired with an external-entity file read, and ElementTree resolves no external
entities here, so only the denial of service was real. Details and the
verbatim measurements are in
[`evaluation-report.md`](evaluation-report.md); what only you can answer is in
[`what-we-need-from-you.md`](what-we-need-from-you.md).

| Rule | CAT | Was | Now |
|---|---|---|---|
| APSC-DV-002440, 002450, 002460, 001750 | I / II | the transport took whatever scheme the endpoint carried and never refused http | plain http is refused off loopback, with `--insecure-http` as a deliberate, documented deviation for a lab store |
| APSC-DV-002550, 002390 | I / II | listing and delete responses parsed with no entity limits; a billion-laughs body expanded to 30,000 characters | a DOCTYPE is refused before parsing, in both readers. No dependency added |
| APSC-DV-002030 | II | `hashlib.md5(body)` unmarked, raising on a FIPS host | `usedforsecurity=False` |

One control deserves its reasoning stated rather than a bare status.
APSC-DV-002440 is marked Not a Finding with a deviation, not silently passed:
transport is secure by default and the override is explicit, named and
documented. An assessor who wants no override at all should read it as Open,
and the flag is one line to remove.

## Formerly open, now closed

| Rule | CAT | Title | Finding |
|---|---|---|---|
| APSC-DV-001750 | I | Transmit only cryptographically protected passwords | Credentials produce a SigV4 signature rather than travelling as a secret, but the transport scheme is not enforced, so a signed request and any Elasticsearch API key could still cross an unencrypted channel. |
| APSC-DV-002440 | I | Protect confidentiality and integrity of transmitted information | `generation_chain/sources/s3.py:53-58` takes the scheme from the operator's `--endpoint` and never refuses `http://`. Nothing in the S3 or OCI paths requires TLS. |
| APSC-DV-002550 | I | Not vulnerable to XML-oriented attacks | `sources/s3.py:136` and `reclaim/batch.py:96` parse store responses with `xml.etree.ElementTree` and no entity limits. Tested on Python 3.12.3: a billion-laughs body expands. An external entity referencing a local file is refused, so the disclosure half of this control is not met by exposure but by the parser's own behaviour. |
| APSC-DV-002030 | II | FIPS validated modules for cryptographic hashes | `reclaim/checksum.py:89` calls `hashlib.md5(body)` with no `usedforsecurity=False`. Raises on a FIPS mode host. Not used for authentication or the approval digest, which is SHA-256. |
| APSC-DV-002390 | II | Mitigate XML denial of service | Same root cause as APSC-DV-002550, confirmed by expansion test. |
| APSC-DV-002450 | II | Cryptographic mechanisms preventing disclosure in transit | Same root cause as APSC-DV-002440. Confidentiality rests on operator configuration rather than on the code. |
| APSC-DV-002460 | II | Maintain confidentiality and integrity during preparation for transmission | Same root cause. No scheme check before a request is sent. |

## Not a Finding

| Rule | CAT | Title | Basis |
|---|---|---|---|
| APSC-DV-000460 | I | Enforce approved authorisations for logical access | `reclaim/cli.py` gates the destructive path. `--execute` requires `--approve-digest` and `--approve-rows`, verified independently in `approval.py` against the exact manifest bytes, before a credential is loaded or a request built. |
| APSC-DV-001740 | I | Store only cryptographic representations of passwords | No password store of its own. Existing credentials are consumed from a file, config location or environment and wrapped in `Secret` on load. |
| APSC-DV-001850 | I | Do not display passwords as clear text | `Secret` renders `<secret>` on every path except `reveal()`, which has two call sites, both signing. Every site read. |
| APSC-DV-002340 | I | Prevent unauthorised modification of information at rest | The approval gate is the control against unauthorised deletion. Digest and row count must both match; dry run is the default and has no bypass flag. |
| APSC-DV-002510 | I | Protect from command injection | Every subprocess call uses the list form with `shell=False`. No command string is built from untrusted input. |
| APSC-DV-000650 | II | Do not write sensitive data to logs | The `Secret` wrapper applies at every credential entry point, so logging, error formatting and the report file cannot emit a raw secret by accident. |
| APSC-DV-001460 | II | Conduct an application vulnerability assessment | This engagement, against this commit. Recurring cadence remains an organisational commitment. |
| APSC-DV-002570 | II | Error messages aid correction without revealing exploitable information | `ManifestError`, `ApprovalError` and `CredentialError` messages are precise about the failure without embedding secret material. |
| APSC-DV-003170 | II | Perform an application code review | This engagement, plus this project's own committed-credential scanner, a standing automated review running on every change. |

## Not Applicable

Justification for every row below, unless noted: this is a headless
single-operator command line tool with no interactive login, no web front end,
no cookies or session identifiers of its own, no SOAP or SAML interface and no
database. The control presumes an architecture this codebase does not have.

**CAT I.** APSC-DV-000190, 000200, 000230, 000240 (WS-Security and SAML
assertion handling), 000510, 000530 (logon attempt limits), 001540, 001680,
002230, 002240 (session identifiers), 002490 (XSS), 002540 (SQL injection: no
SQL, no DBMS, no ORM anywhere, confirmed by source review and by the absence of
any dependency manifest), 002890 (DMZ web server placement).

**CAT II.** APSC-DV-000010, 000060, 000070, 000080, 000090, 000110, 000120,
000130, 000180, 000210, 000220, 000250, 000260, 000280, 000290, 000300,
000340, 000350, 000360, 000370, 000420, 000450, 000470, 000480, 000490,
000500, 000540, 000620, 000630, 000640, 000660, 000680, 000690, 000810,
000830, 000880, 000910, 001030, 001040, 001390, 001440, 001490, 001520,
001530, 001550, 001560, 001570, 001580, 001590, 001600, 001610 is reviewed
below rather than here, 001660, 001670, 001690, 001700, 001710, 001720,
001760, 001770, 001780, 001790, 001795, 001800, 001830, 001880, 001890,
001900, 001910, 001930, 001940, 001950, 001960, 001970, 002000, 002050,
002150, 002210, 002220, 002250, 002260, 002270, 002280, 002290, 002500,
002580 (single audience: the operator running the command, so there is no
separate user population from whom error detail must be withheld), 002870,
002880, 002990, 003300, 003310.

**CAT III.** APSC-DV-000100, 000310, 000320, 000400, 000410, 000430, 000550,
000560, 000570, 000580, 003360.

## Not Reviewed

Needs deployment, host or organisational evidence beyond a static scan of one
commit.

**CAT I.** APSC-DV-001810, 001820 (PKI path validation and private key
access), 001860, 002310 (secure state on abort), 002350, 002485, 002560
(input handling beyond the specific gaps recorded as Open), 002590 (overflow),
003110, 003120, 003240, 003250, 003280.

**CAT II.** APSC-DV-000160, 000170, 000330, 000440, 000520, 000590, 000600,
000670, 000700, 000710 through 000800 and 000820 (audit records for privilege
and security object events), 000840, 000850, 000860, 000870, 000940, 000950,
000960, 000970, 000980 through 001020 (audit record content), 001050, 001070,
001080, 001090, 001100, 001110, 001120, 001130 through 001220 (audit reduction
and reporting), 001250 through 001270 (timestamp source and granularity),
001280 through 001330 (protecting audit information and tools), 001340, 001350,
001360, 001370, 001410, 001420, 001430, 001480, 001500, 001510, 001610,
001620, 001630, 001640, 001650, 001730, 001840, 001870, 001980, 001995 (race
conditions), 002010, 002020 and 002040 (signing and protection use SHA-256
through hashlib and hmac, so compliance depends on the deployed OpenSSL build's
FIPS status, which is a host property not visible in source), 002300, 002320,
002330, 002360, 002370, 002380, 002400, 002410, 002470 (depends on the scheme
actually configured, see the Open finding), 002480, 002520, 002530 (the
delete-critical path in `manifest.py` and `approval.py` validates rigorously;
every other path was not individually re-verified), 002610, 002630, 002760,
002770, 002900, 002910, 002920, 002930, 002950, 002960, 002970, 002980,
002995 through 003020 (configuration management process), 003030, 003040,
003050 through 003090 (contingency and backup), 003100, 003140, 003150,
003190, 003200, 003210, 003230, 003235, 003236, 003270, 003285, 003290,
003320, 003330, 003350, 003400.

**CAT III.** APSC-DV-000380, 000390, 002780, 003130, 003160, 003180, 003215,
003220, 003260, 003340, 003345.
