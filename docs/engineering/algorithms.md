# Algorithms

This document is for an engineer who is about to change code in
`generation_chain/`. It covers what happens inside the tool: the data
structures, the control flow, the decision points, and the refusals. It does
not cover deployment, network edges, or trust boundaries; see
[architecture.md](architecture.md) for the system view and
[../security/threat-model.md](../security/threat-model.md) for the security
posture. It is not a tutorial; for that, start with
[the project README](../../README.md) and
[repository-layout-and-reachability.md](../repository-layout-and-reachability.md).

Two terms recur everywhere below. A **generation** is one version of the
repository's catalog: Elasticsearch writes a new `index-N` at the repository
root every time a snapshot finishes or a delete completes, and never edits an
old one. A **root generation** is that `index-N` document, a complete
`RepositoryData` object naming every live snapshot and every index those
snapshots reference. Each index also has its own **shard generation**
documents, `indices/<index-uuid>/<shard>/index-<gen>`, which name the segment
blobs one shard's snapshots reference. Reading a chain of root generations and
comparing them against the shard generation documents they name is the whole
algorithm.

The safety condition this tool is built around, stated once here because
every diagram below is a picture of some piece of it: **the believed set of
references must be a superset of the true set.** An extra reference costs
storage. A missing reference costs data. Every uncertainty resolves toward
more references and a shorter manifest.

## Contents

1. [Data structures](#data-structures)
2. [Shard document identity](#shard-document-identity-earning-trust-for-one-read)
3. [The derivation, end to end](#the-derivation-end-to-end)
4. [The condemnation decision](#the-condemnation-decision)
5. [The shape gate cascade](#the-shape-gate-cascade)
6. [One loop cycle](#one-loop-cycle-audit-dry-run-approve-execute)
7. [Manifest state machine](#manifest-state-machine)
8. [Exit code map](#exit-code-map)
9. [Read-ahead and concurrency](#read-ahead-and-concurrency)
10. [Memory model](#memory-model)
11. [Existence is three-valued](#existence-is-three-valued)

## Data structures

Three focused diagrams rather than one crowded one, because the parsed
on-disk shapes, the working state one run builds, and the credential and
delete-time shapes belong to different readers.

### The parsed catalog

These are the shapes `formats/repository_data.py` and
`formats/shard_snapshots.py` build directly from bytes, defined in
`generation_chain/model.py`. Nothing downstream reaches back into the bytes;
everything downstream reads these records.

```mermaid
classDiagram
    class RootGeneration {
      +int generation
      +Optional~str~ repository_uuid
      +Mapping~str,SnapshotRef~ snapshots
      +Mapping~str,IndexEntry~ indices
      +Mapping~str,str~ index_metadata_identifiers
      +index_by_uuid(index_uuid) IndexEntry
    }
    class SnapshotRef {
      +str uuid
      +str name
      +Mapping~str,str~ metadata_lookup
      +index_uuids() Tuple~str~
    }
    class IndexEntry {
      +str name
      +str uuid
      +Tuple~str~ snapshot_uuids
      +Tuple~Optional~str~~ shard_generations
      +shard_generation(shard) Optional~str~
    }
    class ShardDocument {
      +FrozenSet~str~ blob_names
      +Mapping~str,FrozenSet~str~~ by_snapshot_name
      +FrozenSet~object~ writer_uuids
      +Mapping~str,int~ length_by_snapshot_name
      +int commit_oracle_checked
      +int commit_oracle_skipped
    }
    class ShardLocation {
      +str index_uuid
      +int shard
      +directory() str
    }
    RootGeneration "1" o-- "many" SnapshotRef
    RootGeneration "1" o-- "many" IndexEntry
    IndexEntry "many" --> "many" SnapshotRef : snapshot_uuids
    ShardLocation "1" ..> "1" ShardDocument : names one, per generation
```

`RootGeneration` is one `index-N`, decoded and cross-checked so that its two
halves, the `snapshots` array and the `indices` map, agree with each other
(`formats/repository_data.py::_cross_check`); a generation whose halves
disagree never becomes one of these. `ShardDocument` is one
`indices/<uuid>/<shard>/index-<gen>`, and it can never be built with an empty
`blob_names`: that is the shape gate, covered on its own below. `IndexEntry`
carries `shard_generations`, one id per shard, where a missing id and a
missing shard both read as `None`, "no opinion", never as "no live files."

### What one run builds

`Chain` (in `derivation/chain.py`), `ShardHistory` and `ShardSurvey` (in
`derivation/shards.py`), and the verdict shapes in `derivation/classification.py`
and `model.py` are working state, held only for the run that built them.

```mermaid
classDiagram
    class Chain {
      +int current_generation
      +str repository_uuid
      +Dict~int,RootGeneration~ generations
      +Tuple~int~ present
      +Optional~int~ latest_generation
      +str anchored_by
      +Dict~int,str~ rejected
      +usable() Tuple~int~
      +missing() Tuple~int~
      +adjacent_pairs() List
      +mixed_transitions() List
      +final() RootGeneration
    }
    class ShardHistory {
      +ShardLocation location
      +FrozenSet~str~ live_blobs
      +FrozenSet~str~ present_blobs
      +ShardDocument current
      +Dict~int,ShardDocument~ documents
      +Dict~int,Doubt~ unreadable
      +FrozenSet~object~ writer_uuids
      +collectable() FrozenSet~str~
    }
    class ShardSurvey {
      +Dict~ShardLocation,ShardHistory~ histories
      +Dict~str,Doubt~ dropped
      +Dict~str,Doubt~ retired
      +int considered
    }
    class DeleteOperation {
      +str snapshot_uuid
      +str snapshot_name
      +int from_generation
      +int to_generation
    }
    class Condemnation {
      +str key
      +str category
      +str reason
      +str snapshot_uuid
      +str snapshot_name
      +int from_generation
      +int to_generation
    }
    class Placement {
      +str key
      +str disposition
      +str detail
    }
    class Verdict {
      +List~Placement~ placements
      +List~Condemnation~ manifest
    }
    class Coverage {
      +Optional~str~ repository_uuid
      +Optional~int~ current_generation
      +Tuple~int~ generations_usable
      +Dict~int,str~ generations_rejected
      +int transitions_total
      +int operations_found
      +int operations_attributed
      +int shards_considered
      +Dict~str,str~ shards_dropped
      +Dict~str,str~ shards_retired
      +Tuple~str~ existence_unanswered
      +Optional~str~ refused
      +bool refusal_is_transient
      +bool refusal_needs_a_bigger_host
      +Optional~str~ corroborated_by
    }
    class AuditResult {
      +List~Condemnation~ condemned
      +Coverage coverage
      +List~Placement~ classification
    }
    ShardSurvey "1" o-- "many" ShardHistory
    AuditResult "1" *-- "1" Coverage
    AuditResult "1" o-- "many" Condemnation
    AuditResult "1" o-- "many" Placement
    Verdict "1" o-- "many" Placement
    Verdict "1" o-- "many" Condemnation
```

`Condemnation` is a candidate: one key, the delete operation that should have
removed it, and why. `Placement` is broader: every key the listing named gets
one, whatever it turned out to be. `Verdict.manifest` is the subset of
`Condemnation` values whose matching `Placement` came out `orphaned`, which is
the only disposition that reaches the file an operator acts on. `Coverage` is
the run's honesty check: it is what lets an operator tell "little to clean
up" apart from "could not see most of the repository," which read identically
without it.

### Credentials and delete-time shapes

```mermaid
classDiagram
    class Secret {
      -str value
      +reveal() str
    }
    class CredentialFile {
      +str path
      +Mapping sections
      +section(name) Mapping
      +required(section, field) str
    }
    class Credentials {
      +Optional~str~ username
      +Optional~Secret~ password
      +Optional~Secret~ api_key
      +header() Dict
    }
    class S3Credentials {
      +str access_key
      +Secret secret_key
    }
    class OciCredentials {
      +str key_id
      +RsaPrivateKey private_key
    }
    class Veto {
      +str endpoint
      +FrozenSet~str~ snapshot_uuids
      +FrozenSet~str~ index_uuids
      +Tuple~str~ mounted_indices
      +Tuple~str~ in_flight
      +int snapshots_reported
      +covers(condemnation) bool
      +apply(condemned) List
    }
    class ManifestData {
      +str path
      +Tuple~str~ keys
      +str digest
      +int byte_length
    }
    class BatchOutcome {
      +Tuple~str~ deleted
      +Tuple already_absent
      +Tuple failed
      +Tuple~str~ unconfirmed
    }
    CredentialFile ..> Secret : yields
    Credentials ..> Secret : holds
    S3Credentials ..> Secret : holds
```

`Secret` is not a security boundary against a determined reader of the
process's own memory; it is a guard against the accident, an f-string, a
repr, a JSON dump, that would otherwise print a credential. `reveal()` has
exactly two call sites in the whole codebase, both a signing step.
`ManifestData` is read once, so its `digest` and `keys` can never drift from
a second read seeing a file that changed underneath. `BatchOutcome` is the
per-key answer for one `DeleteObjects` batch: `deleted`, `already_absent`,
`failed`, and `unconfirmed`, the last being a key that appeared in neither
the store's `<Deleted>` nor `<Error>` list, which is treated as failure, not
success, because a 200 that omits a key told this tool nothing about it.

## Shard document identity: earning trust for one read

A `BlobStoreIndexShardSnapshots` document names neither its own shard, its
own index, nor its own generation; that was confirmed against real captured
documents, not assumed. So nothing inside the bytes ties a document to the
key it was fetched under, and every document has to earn that tie from
outside itself before its file list is trusted. This runs once per shard
directory (`derivation/identity.py`, called from `derivation/shards.py`).

```mermaid
flowchart TD
    A["read index-gen for shard directory D"] --> B{"blob_names non-empty"}
    B -->|"no"| R1["ShapeGateError: require_blob_names refuses. A shard snapshot always carries at least its Lucene commit"]
    B -->|"yes"| C{"document names a blob the listing does not show in D"}
    C -->|"yes"| R2["Doubt NAMES_BLOBS_NOT_HERE, drop D"]
    C -->|"no"| E{"at least one blob stem the listing puts in D and no other directory"}
    E -->|"no"| R3["Doubt NO_UNIQUE_WITNESS, drop D. Containment alone cannot separate two directories"]
    E -->|"yes"| F{"the store confirms at least one such witness on a second ask"}
    F -->|"no"| R4["Doubt WITNESS_UNCONFIRMED, drop D"]
    F -->|"yes"| G{"the document's snapshot names equal the catalog's expected set for D at this generation"}
    G -->|"no"| R5["Doubt SNAPSHOT_NAMES_DISAGREE, drop D. This is what catches a swap for another generation of the SAME shard"]
    G -->|"yes"| H["document trusted for D at this generation, added to ShardHistory.documents"]
    H --> I{"writer_uuids overlap a DIFFERENT directory's writer_uuids, checked once across the whole run"}
    I -->|"yes, for both directories"| R6["Doubt WRITER_UUID_COLLISION: BOTH directories dropped, one of the two reads returned the other's document"]
    I -->|"no"| J["document stays trusted"]
```

The rule this enforces is attributability, not containment. A document
naming nothing is a subset of every directory, which is why the shape gate
(`require_blob_names`) runs first and unconditionally. A document naming
only blobs another directory also holds is still not proof, because blob
names are globally unique, so a genuine document always has at least one
witness unique to its own directory; a document with none does not
distinguish this shard from any candidate it would equally fit. The writer
uuid check is the one signal that can catch a real document from a
neighbouring shard whose blobs happen to be a subset of the victim
directory's own blobs, a case attributability alone cannot see; it is
checked once, against the whole run's accumulated writer uuids per
directory, because two directories claiming the same Lucene writer identity
is a contradiction wherever in the run either was read.

## The derivation, end to end

Two flowcharts. The first is the command line's control flow, exit codes
included. The second is what happens inside `run_audit`, the single function
`derivation/audit.py` calls "the whole entry point."

```mermaid
flowchart TD
    S["cli.main"] --> T{"transport named, or --local-repo given"}
    T -->|"no, and stdin is a terminal"| ASK["prompt: choose_transport, then choose_s3_endpoint if needed"]
    T -->|"no, and no terminal"| U1["exit 3: no transport named, nothing read"]
    ASK --> BLD
    T -->|"yes"| BLD["build_source, then prepared: GuardedSource, CriticalReads, ReadAhead"]
    BLD -->|"Misconfigured or a credential error"| U2["exit 3"]
    BLD --> COR{"--elasticsearch passed"}
    COR -->|"no"| RUN["run_audit(source, veto=None, budget_bytes)"]
    COR -->|"yes"| VET["ElasticsearchVeto.fetch"]
    VET -->|"CorroborationUnavailable, transient"| E4["exit 4"]
    VET -->|"CorroborationUnavailable, not transient"| E2["exit 2"]
    VET -->|"ok"| RUN
    RUN --> RES{"result.coverage.refused"}
    RES -->|"no"| OK["write manifest, classification, coverage-json; exit 0"]
    RES -->|"yes, needs_a_bigger_host"| E5["exit 5"]
    RES -->|"yes, transient"| E4b["exit 4"]
    RES -->|"yes, settled"| E2b["exit 2"]
```

```mermaid
flowchart TD
    L["list_keys: every key under the repository root"] -->|"SourceReadError"| R1["refused: cannot list, transient, exit 4"]
    L --> ANC["load_chain: fetch index.latest, then the highest listed generation carrying THIS run's repository uuid, walking down from the top"]
    ANC -->|"the identity generation carries no repository uuid"| R2["refused, not transient, exit 2"]
    ANC -->|"a generation above index.latest cannot be read, so its ownership is unknown"| R3["refused, transient, exit 4"]
    ANC -->|"the anchor names no live snapshots"| R4["refused, not transient, exit 2"]
    ANC --> LOW["every OTHER present generation is read: unsupported or foreign ones are dropped and recorded, never abort the run"]
    LOW --> IDX["KeyIndex built over the listing, for later existence confirmation"]
    IDX --> PLAN["plan_shard_batches: size groups of shard directories to fit budget_bytes"]
    PLAN -->|"one directory alone exceeds budget_bytes"| R5["refused: ShardDirectoryTooLarge, needs_a_bigger_host, exit 5"]
    PLAN --> CUR["survey_shards pass 1: read only each shard's CURRENT document"]
    CUR --> EXT["check_declared_extent: every live snapshot's declared shard count, index list and size must be fully accounted for by what pass 1 read, or the shards it touches are dropped. See the shape gate cascade diagram"]
    EXT --> ERA["survey_shards pass 2: one group of surviving directories at a time, read era documents, condemn that group's segments, then discard the parsed documents"]
    ERA --> COLL["writer-uuid collision check, once, across every surviving directory's whole-run summary"]
    COLL --> REP["condemn_repository_wide: root snapshot docs, global metadata, shard-snapshot docs, index metadata. None of these needs a shard's file list"]
    REP --> DEC["decide: place every key the listing named, subtract the veto, keep only ORPHANED placements as the manifest"]
    DEC -->|"a key is both condemned and independently placed LIVE"| R6["refused: the derivation contradicted itself, exit 2"]
    DEC --> COV["Coverage assembled from every stage above"]
    COV --> OUT["AuditResult: manifest, coverage, per-key classification"]
```

The order in the second diagram is short enough to say in one sentence, and
`derivation/audit.py`'s own module docstring says it: anchor the chain on the
highest generation that is ours, survey the shards and drop every one whose
evidence is incomplete, take Elasticsearch's own shard-local set difference,
keep the part of it some observed delete accounts for, and record everything
none of that could see. Every stage that fails records itself in coverage and
contributes nothing to the manifest.

## The condemnation decision

One key's placement, from `derivation/classification.py::_place` and
`_place_in_shard`, followed by the veto and the self-contradiction refusal in
`decide`. Only `ORPHANED` reaches the manifest.

```mermaid
flowchart TD
    K["one key from the store's listing"] --> Q1{"is it index.latest"}
    Q1 -->|"yes"| L1["LIVE: pointer to the current root generation"]
    Q1 -->|"no"| Q2{"matches index-N at the repository root"}
    Q2 -->|"N equals current_generation"| L2["LIVE: the current root generation"]
    Q2 -->|"N less than current_generation"| EV1["EVIDENCE: superseded root generation. Kept because the derivation reads it to learn what a delete removed"]
    Q2 -->|"N greater than current_generation"| UX1["UNEXPLAINED: names a generation above the one this run anchored on"]
    Q2 -->|"no match"| Q3{"matches snap-uuid.dat or meta-uuid.dat at the root"}
    Q3 -->|"uuid still live"| L3["LIVE: document of a live snapshot"]
    Q3 -->|"uuid not live"| UX2["UNEXPLAINED, unless some delete operation names this uuid: then CONDEMNED"]
    Q3 -->|"no match"| Q4{"matches indices/idx/meta-id.dat"}
    Q4 -->|"the live metadata set could not be established completely"| UX3["UNEXPLAINED: no live set for index metadata at all"]
    Q4 -->|"id is in the live set"| L4["LIVE: metadata a live snapshot references"]
    Q4 -->|"id is not in the live set"| UX4["UNEXPLAINED, unless some delete operation names it: then CONDEMNED"]
    Q4 -->|"no match"| Q5{"matches indices/idx/shard/..."}
    Q5 -->|"no"| OUT["OUTSIDE_MODEL: not an object this tool models"]
    Q5 -->|"shard generation document, id equals the current shard generation"| L5["LIVE: current shard generation document"]
    Q5 -->|"shard generation document, id superseded"| EV2["EVIDENCE: superseded shard generation document, kept for the file lists of earlier eras"]
    Q5 -->|"snap-uuid.dat, uuid still live"| L6["LIVE: shard document of a live snapshot"]
    Q5 -->|"snap-uuid.dat, uuid not live"| UX5["UNEXPLAINED, unless some delete operation names it: then CONDEMNED"]
    Q5 -->|"segment blob"| Q6{"this shard directory was dropped or retired"}
    Q6 -->|"dropped: evidence in doubt"| UX6["UNEXPLAINED: in a shard this run dropped"]
    Q6 -->|"retired: index has no live snapshot"| UX7["UNEXPLAINED: no live set established here, ordinary and not a fault"]
    Q6 -->|"no, this directory was surveyed"| Q7{"blob is in the CURRENT shard document's live set"}
    Q7 -->|"yes"| L7["LIVE: a segment the current shard document names"]
    Q7 -->|"no, and this directory was never named by any generation this chain reads"| UX8["UNEXPLAINED"]
    Q7 -->|"no, but collectable: present and not in the live set"| Q8{"some observed delete's era document names this exact blob for this shard"}
    Q8 -->|"no"| UX9["UNEXPLAINED: Elasticsearch's own set difference would collect it, no readable file list attributes it to an observed delete"]
    Q8 -->|"yes"| COND["CONDEMNED candidate"]
    UX2 -.->|"delete found"| COND
    UX4 -.->|"delete found"| COND
    UX5 -.->|"delete found"| COND

    COND --> V1{"a veto is present and covers this key: snapshot_uuid matches, or the key starts with indices/index-uuid/ for an index_uuid the veto carries"}
    V1 -->|"yes"| PROT["PROTECTED: left out of the manifest. The veto only ever subtracts"]
    V1 -->|"no"| Q9{"the SAME key was also independently placed LIVE above"}
    Q9 -->|"yes"| REF["the whole run refuses: the derivation contradicted itself. No manifest is produced"]
    Q9 -->|"no"| ORPH["ORPHANED: written to the manifest"]
```

Two boundaries worth naming because an operator will overestimate them. The
veto's `index_uuids` are populated only from indices that carry an
`index.store.snapshot.*` setting, which is Elasticsearch's mark of a mounted
searchable snapshot. An ordinary live index nothing has mounted is outside
the veto entirely; only its snapshot's uuid can protect it, the same as any
other snapshot. And the contradiction check is not a normal disposition: it
is the one place the whole run refuses rather than shipping a shorter
manifest, because two readings of one live set disagreeing means a defect in
this code, not a fact about the repository.

## The shape gate cascade

Elasticsearch writes a shard document naming zero files for a snapshot entry
when it snapshots an empty shard, the window right after an index rollover
creates a fresh, still-empty backing index. The parser refuses that document.
The refusal is load bearing, and this is the cascade it sets off, measured on
the churn rig and described in `reclaim_test_protocol.py`'s own module
docstring.

```mermaid
flowchart TD
    ES1["Elasticsearch snapshots an empty shard: it writes a shard document whose snapshot entry names zero files"] --> P1["parse_shard_snapshots reads that entry"]
    GATE["shape gate: an empty files list raises ShapeGateError. A shard snapshot always carries at least its Lucene commit, so an empty list is a list that was not read, not a shard that references nothing"]
    P1 --> GATE
    GATE --> READ["_read in derivation/shards.py catches the error and returns no document for this key"]
    READ --> CUR{"was this the CURRENT document, the one the anchor generation names for this shard"}
    CUR -->|"yes"| DROPSHARD["Doubt CURRENT_DOCUMENT_UNREADABLE: this shard directory is dropped during pass 1. It never reaches histories, and no era document of it is ever read"]
    CUR -->|"no, an earlier era"| LOCAL["only that one generation's attributions are lost for this shard. The directory can still survive"]
    DROPSHARD --> EXTENT["check_declared_extent runs next, over the directories that DID survive pass 1"]
    EXTENT --> Q1{"for the live snapshot referencing the dropped shard's index: do the surviving shards of that index add up to its declared shard_count"}
    Q1 -->|"no"| DROPIDX["Doubt EXTENT_SHARD_COUNT: every OTHER shard directory of that SAME index is dropped too"]
    Q1 -->|"yes, but the snapshot's grand total across every index it touches is short"| DROPSNAP["Doubt EXTENT_TOTAL_SHARDS: every index that snapshot touches is dropped, including indices the missing shard never belonged to"]
    DROPIDX --> RESULT["none of the newly dropped directories reach pass 2. No segment blob in any of them is ever condemned, for this operation or any other"]
    DROPSNAP --> RESULT
    RESULT --> MEASURED["measured on the rig: one file-less shard document dropped 2 of 8 shard directories directly. The extent cascade then dropped all 8 shard directories the run had planned to read"]
```

The consequence stated in `reclaim_test_protocol.py` is worth repeating
exactly: the segment path, the one with real blast radius, needs a complete
view of a shard directory, and across every campaign run before that harness
existed it condemned exactly zero objects, not because it is broken but
because a single badly timed snapshot poisons a shard directory until a
later snapshot supersedes it, and the extent check spreads that poisoning
across the snapshot's other directories. Fixing this means changing when the
rig snapshots, never relaxing the refusal: an empty shard directory might
belong to an index Elasticsearch is about to use, and the bucket alone
cannot tell the difference. The Elasticsearch veto does not cover this case
either, because it protects by snapshot uuid and by mounted-index prefix,
neither of which an ordinary empty shard has yet.

## One loop cycle: audit, dry run, approve, execute

`reclaim_test_protocol.py` drives exactly this cycle, repeatedly, against a
live repository. The important thing this diagram makes visible: the dry run
is what produces the two numbers an approval has to match.

```mermaid
sequenceDiagram
    actor Operator
    participant Harness as reclaim_test_protocol.py
    participant Audit as generation_chain (audit)
    participant Store as object store
    participant ES as Elasticsearch, optional
    participant Reclaim as generation_chain.reclaim

    Harness->>Audit: run audit, --manifest orphans.tsv
    Audit->>Store: list_keys, fetch index.latest, root generations, shard documents
    Store-->>Audit: bytes, or SourceReadError
    opt corroboration requested
        Audit->>ES: GET snapshot list, mounted-index settings, in-flight status
        ES-->>Audit: three answers
    end
    Audit-->>Harness: manifest with COMPLETION_MARKER, coverage report, exit code

    Harness->>Reclaim: reclaim, --manifest orphans.tsv, no --execute
    Reclaim->>Reclaim: load_manifest, checksum the first batch
    Reclaim-->>Harness: DRY RUN report naming the sha256 digest and row count an approval must match

    Operator->>Operator: reads the manifest and the coverage report, decides

    Harness->>Reclaim: reclaim, --execute, --approve-digest D, --approve-rows N, plus --elasticsearch or --without-elasticsearch
    Reclaim->>Reclaim: verify_approval: D and N against THIS file's exact bytes, right now
    Reclaim->>Reclaim: staleness_problem against --max-manifest-age
    opt --elasticsearch passed at execute time
        Reclaim->>ES: re-fetch the veto, now
        ES-->>Reclaim: three answers
        Reclaim->>Reclaim: newly_protected against the manifest's keys
    end
    Reclaim->>Store: DeleteObjects per batch, checksum header, Quiet false
    Store-->>Reclaim: DeleteResult, Deleted or Error per key
    Reclaim-->>Harness: tally: deleted, already_absent, failed, unconfirmed, and exit code

    Harness->>Harness: next cycle's audit sees only what this execute actually removed
```

Approval binds one manifest, not a policy: `--approve-digest` is the sha256
of the exact bytes about to be acted on, so a regenerated run over the same
repository, byte for byte different, cannot be approved by yesterday's
answer. `--approve-rows` is redundant with the digest in the adversarial
sense and asked for anyway, because a hash is not a number a human notices
is wrong and a row count is.

## Manifest state machine

```mermaid
stateDiagram-v2
    [*] --> Deriving : run_audit starts
    Deriving --> Refused : coverage.refused is set. No COMPLETION_MARKER is written. Audit exits 2, 4 or 5
    Deriving --> Derived : written through --manifest FILE, atomically, then COMPLETION_MARKER appended
    Deriving --> Unmarked : written to stdout with no --manifest FILE. No marker is appended even though the run was not refused
    Refused --> [*]
    Unmarked --> [*] : reclaim's load_manifest refuses a file with no marker. Reclaim exits 2
    Derived --> Loaded : load_manifest checks header, trailing newline, per-row column count, and COMPLETION_MARKER
    Derived --> LoadRefused : any structural check fails. Reclaim exits 2
    LoadRefused --> [*]
    Loaded --> DryRunReported : reclaim without --execute. Prints the digest and row count an approval must match
    DryRunReported --> Approved : --execute, --approve-digest and --approve-rows match THIS file's sha256 and row count exactly
    DryRunReported --> ApprovalRefused : digest or row count does not match. Reclaim exits 3
    ApprovalRefused --> [*]
    Approved --> StaleRefused : age since the file's mtime exceeds --max-manifest-age, and the limit is not 0. Reclaim exits 3
    StaleRefused --> [*]
    Approved --> ReVetoed : --elasticsearch passed again at execute time, and the cluster now protects a key this manifest names. Reclaim exits 3
    ReVetoed --> [*]
    Approved --> Executing : neither stale nor newly protected. Either --elasticsearch or --without-elasticsearch was required to reach here
    Executing --> Executed : every batch sent. deleted, already_absent, failed, unconfirmed tallied
    Executed --> [*] : exit 0 if nothing failed or went unconfirmed, otherwise exit 4
```

The completion marker and the approval answer two different questions, and
both have to be answered before a delete happens. The marker proves the
derivation finished; it says nothing about what happened to the file
afterward, so a manifest hand-edited after the marker was written still
reads as marked. The approval is what closes that gap: it ties a digest to
the exact bytes an operator is about to act on, so an edited or superseded
manifest fails there even when it passes the marker check.

## Exit code map

Grouped by whether a retry could ever help, taken from each tool's `--help`
and the `EXIT_*` constants in `generation_chain/cli.py` and
`generation_chain/reclaim/cli.py`.

```mermaid
flowchart LR
    subgraph AuditExit["python3 -m generation_chain"]
        A0["0: completed, manifest written"]
        A2["2: refused for a settled reason, such as an unsupported repository format, an unanchorable catalog, or a self-contradiction. Retrying changes nothing"]
        A3["3: the invocation or a credential is wrong. Fix it, then rerun"]
        A4["4: the store or the cluster did not answer. A retry is reasonable"]
        A5["5: one shard directory alone needs more memory than this host offers. Retrying here never helps. A bigger host, a narrower --prefix, or --max-ram does"]
    end
    subgraph ReclaimExit["python3 -m generation_chain.reclaim"]
        B0["0: dry run reported, or every key executed against was deleted or already absent"]
        B2["2: the invocation, the manifest, or the checksum algorithm is wrong. Fix it, then rerun"]
        B3["3: --execute was passed without an approval matching this exact manifest, or the manifest is too stale, or the cluster now protects a key it names. Re-derive and re-approve, never just retry"]
        B4["4: the run executed and at least one key failed or went unconfirmed. Read --report first; retrying the same batch may or may not help depending what failed"]
    end
```

`RunRefused.transient` is what separates exit 2 from exit 4 on the audit
side, and it is a judgement this codebase states explicitly at each raise
site rather than infers from the exception type: a 5xx or 429 from a store or
a cluster is transient, an unsupported format or a self-contradiction is not.
`needs_a_bigger_host` is a second, independent flag for the same reason: a
scheduled caller reading only `transient` cannot tell "try again here" from
"try this somewhere else," and those are different instructions.

## Read-ahead and concurrency

Two wrappers sit between the derivation and a transport
(`sources/readahead.py`), on top of a shared bounded pool
(`sources/overlap.py`). Neither changes what a key answers; deleting every
`prefetch` call in the package would leave the manifest unchanged and only
make the run wait for one round trip at a time again.

```mermaid
sequenceDiagram
    participant D as derivation
    participant RA as ReadAhead
    participant Bud as Budget, width equals --concurrency, clamped 1 to 32
    participant Pool as shared ThreadPoolExecutor, 32 workers, one per process
    participant CR as CriticalReads
    participant G as GuardedSource
    participant S as transport: s3, oci, or local

    D->>RA: hint, meaning prefetch, with keys in the order they will be read
    RA->>RA: discard any previous plan, queue the new keys
    loop fill the window, up to concurrency outstanding
        RA->>Bud: submit(fetch, key)
        Bud->>Bud: acquire one of width permits
        Bud->>Pool: submit(_run, fetch, key)
    end
    Pool->>CR: fetch(key), on a worker thread
    CR->>G: fetch(key), or fetch_critical(key) for the listing, index.latest, and the anchor generation
    G->>S: the actual read
    S-->>G: bytes, or an exception
    G-->>CR: bytes, or SourceReadError raised
    CR-->>Pool: bytes, or the exception
    Pool-->>Bud: value or error captured, never raised on the worker thread, permit released
    D->>RA: fetch(key1), in the same order the plan was given
    RA->>RA: _fill tops up the window from the remaining plan
    RA->>RA: pop key1's future, block on result
    RA-->>D: bytes, or the ORIGINAL exception, re-raised unchanged, in the same place a serial read would have raised it
```

Two things are true at once and both matter to a reviewer. First, overlap
must not move the answer: work is submitted and settled by key, one thread
decides only when bytes arrive, never which bytes or which error belongs to
which key, and that is what this project's determinism tests hold. Second, exactly three reads
are escalated to a longer retry policy by `CriticalReads`: the listing,
`index.latest`, and the root generation `index.latest` names. Those three end
the whole run if they fail; everything else, a shard document, an existence
check, degrades locally and only shortens the manifest. The listing itself
has no partial form and cannot be overlapped with anything else: it is one
synchronous call that must finish before `load_chain` can even ask for
`index.latest`, so it is the one genuinely serial step at the front of every
run.

## Memory model

`--memory-mb` and `--max-ram` size `budget_bytes`, which flows into
`run_audit` and from there into `plan_shard_batches`
(`derivation/shards.py`). Without either flag, `available_bytes()`
(`sources/budget.py`) reads `/proc/meminfo` and the container's cgroup limit,
takes the smaller of the two, and keeps 80 percent of it.

```mermaid
flowchart TD
    START["run starts: --memory-mb, --max-ram, or the host's own available_bytes reading"] --> LIST["list_keys: every key held in memory as one Python list for the rest of the run"]
    LIST --> HOLD["held for the WHOLE run, never freed early: the key list, KeyIndex's per-key existence cache, the Chain's parsed root generations, ShardHistory.writer_uuids for every surviving directory, the condemned dict, and era_names' snapshot-name summaries"]
    HOLD --> PLAN["plan_shard_batches groups shard directories so that objects_in_directory times generations_for_that_directory times 1900 bytes stays under budget_bytes PER GROUP"]
    PLAN -->|"one directory alone exceeds budget_bytes"| REF5["ShardDirectoryTooLarge: refused before that directory is read, exit 5"]
    PLAN --> GROUP["one group read at a time: ShardHistory.documents holds one parsed ShardDocument per shard directory per generation, but only for THIS group"]
    GROUP --> COND["condemn_segments runs against this group's documents while they are still resident"]
    COND --> CLEAR["on_group clears history.documents for every location in this group before the next group is read"]
    CLEAR --> NEXT{"more groups"}
    NEXT -->|"yes"| GROUP
    NEXT -->|"no"| DONE["survey complete"]
    HOLD -.-> GAP["the listing itself is never batched or bounded by budget_bytes. Only the parsed per-group shard documents are. A repository with enough OBJECTS, spread across many small shard directories that each individually fit, can still exceed what this host holds, with nothing in the current wiring refusing early"]
```

The number this is measured against, 1.9 KB resident per object, is stated
in `sources/budget.py` as linear to 585,194 objects at 1.55 GB peak, and a 2
GB host was measured to die near 750,000 objects, at the end of a thirty
minute run, with an OOM kill and no manifest. `sources/budget.py` also
defines `MemoryBudget` and `with_budget`, a wrapper meant to refuse a listing
too large for the host before a single object is read. As of this reading,
that wrapper is exercised only by this project's own tests;
`generation_chain/cli.py` never calls `with_budget` or constructs a
`MemoryBudget`, so the door check the module's own docstring describes is
not reachable from the command line today. The only active memory
protection is `plan_shard_batches`'s per-group bounding and
`ShardDirectoryTooLarge`'s refusal of one oversized directory; the whole-run
overhead the docstring warns about (the listing, the key index, the chain)
is not bounded by `--memory-mb` or `--max-ram` at all. This gap is the one
[testing-in-your-oci-environment.md](../testing-in-your-oci-environment.md)
tracks as a real, open limitation, upstream issue 7: memory use scales with
object count, and `--memory-mb` today only makes the shard-batching refuse
before it reads rather than fail partway through, not the run as a whole.

## Existence is three-valued

A smaller diagram, added because it is the one measured place this tool's
own report was found to be wrong rather than merely conservative, and it
feeds both the identity witness check above and every root-level
condemnation that tests `key in keys`.

```mermaid
stateDiagram-v2
    [*] --> Listed : the store's listing named this key
    Listed --> Confirmed : source.exists(key) returns True
    Listed --> Denied : source.exists(key) returns False
    Listed --> Unanswered : source.exists(key) raised anything at all
    Confirmed --> InManifest : this is the only state that can reach the manifest or satisfy "in keys"
    Denied --> [*] : treated as absent, never named
    Unanswered --> [*] : reported separately in Coverage.existence_unanswered, never treated as a denial
```

`KeyIndex` (`derivation/keys.py`) caches this per key for the run. The
retired predecessor caught every exception from the existence check and
recorded it as a denial, which folded "the store said no" and "the store
could not answer" into one value; measured against a store failing 1 in
1000 checks, about 31 of 30,938 keys silently left a 100-percent-coverage
report. Keeping `Unanswered` separate does not change which keys reach the
manifest, since only `Confirmed` does either way; it changes whether an
operator reading the coverage report can tell a clean repository from one
this run could not finish asking about.
