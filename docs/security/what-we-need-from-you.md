# What only you can answer

The ASD STIG assessment leaves 157 controls Not Reviewed. That is not a gap in
the scan, it is the shape of the standard: most of what it asks about is not
visible in source code. Whether security audit records reach a central store, whether
the host runs FIPS-validated crypto, whether a change board approved a release,
whether anyone ran a penetration test this year.

Marking those Not a Finding without evidence would have made the report look
better and mean less. So here is the list, grouped, each answerable in a
sentence or two, each naming the controls it closes.

Answer what you can. Anything you leave blank stays Not Reviewed, honestly
labelled, which is a better outcome than a guess.

## 1. Audit records

Covers roughly forty controls, the largest single block.

- Where do this tool's logs go? It writes to stderr today. Is anything
  collecting that, and if so what?
- How long are records kept? The standard asks for thirty months of retention
  under ISSO control. *(APSC-DV-002900)*
- Who reviews them, and how often? *(APSC-DV-002910)*
- Is there a central log store to off-load to? *(APSC-DV-001070, 001080)*
- Should the tool alert on audit failure, and to whom? *(APSC-DV-001100, 001110)*

A useful shortcut: if you name the log destination and the retention period, a
large fraction of that block resolves together.

## 2. The host

- Does the deployment host run OpenSSL in FIPS mode? This decides
  APSC-DV-002020 and 002040 outright. It also matters practically: an unmarked
  MD5 call raises on such a host, which is why that was fixed.
- What Python build and version will run this? The read-only guarantee was
  tested on 3.12.
- Is the host itself STIG'd, and against which baseline?

## 3. Certificates and identity

- Which CA signs the Elasticsearch endpoint's certificate? Is it a DoD CA?
  *(APSC-DV-002300)*
- Is there a trust anchor the tool should validate against? It accepts
  `--es-ca-cert` today and otherwise uses the system store. *(APSC-DV-001810)*
- Who holds the Oracle Customer Secret Key, and what is the rotation
  interval? *(APSC-DV-001760, 001770)*
- Is access to it restricted to named individuals rather than a shared
  account? *(APSC-DV-000290)*

## 4. Process

- Is there a configuration control board, and does this repository fall under
  it? *(APSC-DV-003000 to 003020)*
- Is there a software configuration management plan naming this repository?
- Who is the ISSO of record?
- Is there an incident response plan this tool falls under?
  *(APSC-DV-003236)*
- Has a penetration test been run against the environment this operates in,
  and when? *(APSC-DV-002930)*
- Is there a dedicated security tester distinct from whoever writes the code?
  *(APSC-DV-003150)*

## 5. Data and classification

- Does the data in these snapshots carry a classification? If so, at what
  level, and is there a Security Classification Guide?
  *(APSC-DV-003120, 003290)*
- Does anything in the repository need marking on output? The reports name
  object keys and byte counts, never document content.
- Is the Oracle tenancy in a commercial realm or a government one? It changes
  which endpoint domain applies and may change the answer on several
  transmission controls.

## 6. Network

- Has the Elasticsearch port been registered through the DoD Ports, Protocols
  and Services Management process? *(APSC-DV-002980, 002990)*
- Does traffic to Oracle Object Storage leave an enclave, and if so through
  what? *(APSC-DV-003350)*

## 7. Contingency

- Where do backups of the snapshot repositories live, and how often are they
  taken? *(APSC-DV-003050 to 003090)*
- Is there a documented recovery procedure?

This one matters more than the STIG framing suggests. Oracle's S3
compatibility API exposes no object versioning, so a wrong delete has no
recovery path through that API. A backup held elsewhere is the only recovery
there is, and this repository already records moving backups off the broken
delete path onto a filesystem repository as the fix rather than the mitigation.

## What is already answered, and needs nothing from you

For contrast, so the list above does not read longer than it is.

The tool has no dependencies, so there is no supply chain to assess. It takes
no credential on a command line. It has no database, no web interface, no
session management and no user accounts, which is why 113 controls are Not
Applicable rather than unaddressed. Its destructive path requires a SHA-256
digest and a row count naming an exact file before it will act. The read path
cannot delete, and that is enforced structurally rather than by convention.
