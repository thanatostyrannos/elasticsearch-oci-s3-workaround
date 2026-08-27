# Skills

Operator runbooks for this repository's tools, packaged as agent skills. The
tools are the scripts in the repository root and the `generation_chain` package;
each skill here is the procedure for driving one of them safely: when to reach for
it, the gates to clear first, how to read its output, and what to verify
afterwards.

| Skill | Use it when |
|---|---|
| [`es-snapshot-audit`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/skills/es-snapshot-audit/SKILL.md) | Sizing or inventorying a snapshot repository, separating backup growth from frozen-tier footprint, planning storage before a migration |
| [`es-hybrid-migration`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/skills/es-hybrid-migration/SKILL.md) | Moving backups to block/NFS storage while the frozen tier stays mounted on the S3-compatible repository |
| [`test-rig-tuning`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/skills/test-rig-tuning/SKILL.md) | Standing up or tuning a rig that continuously exercises a tool whose failure destroys data, and spotting when the rig is measuring nothing |

## Three runbooks were removed, and the delete step came back under a different tool

`es-orphan-sweep`, `es-s3-orphan-sweep` and `es-log-cleanup` are gone. Each was
a procedure for driving one of the three sweepers, and the sweepers are retired
because they decided what to delete by absence from a set they computed
themselves, so any read that failed or any document that would not parse became
a deletion. A reviewer drove one of them, along the path its runbook documents,
to a real delete of a live segment blob.

They were removed rather than marked retired. A retirement banner at the top of
a delete procedure is a banner, and the fifteen hundred lines under it are still
a procedure. An operator who lands mid-document, or who has run it before, gets
no banner at all. A runbook for deleting production data with a tool that is not
in the repository is worse than no runbook.

The replacement has landed, and it is two commands rather than one.

[`generation_chain`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/generation_chain/README.md) is the audit, run
as `python3 -m generation_chain`. It reproduces Elasticsearch's own shard-local
set difference and condemns a blob on its PRESENCE in a deleted snapshot's file
list, never on its absence from a live set the tool computed. It has no delete
path at all: its HTTP layer allows GET and HEAD behind an assert. A read that
fails there makes the manifest shorter rather than wrong, which is the whole
difference from the three tools above.

[`generation_chain.reclaim`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/generation_chain/README.md#layout) is
the delete, run as `python3 -m generation_chain.reclaim`, kept as a separate
command for a separate step. It removes exactly the keys an approved manifest
names and derives nothing of its own. The dry run is the default, and
`--execute` will not run without `--approve-digest` and `--approve-rows` that
match the exact bytes of the manifest the dry run read.

So `es-snapshot-audit` is still read-only, and `es-hybrid-migration` now carries
one step that deletes, Step 9, written against those two commands. Its Step 8,
the scheduled log-driven drain, stays withdrawn: nothing in this repository
reads Elasticsearch's failed-delete WARN lines any more, and no current tool
does what that step did.

## Install

These are plain Markdown, and every coding assistant that supports skills
discovers them from a directory of its own. Check yours for the path; it is
usually under your home directory, and some also read a project-local one.

Symlink them, so a `git pull` updates them in place:

```bash
SKILLS_DIR="$HOME/path/to/your/agent/skills"   # see your assistant's docs
for s in skills/*/; do
  ln -snf "$(pwd)/$s" "$SKILLS_DIR/$(basename "$s")"
done
```

Or copy them if you prefer pinned versions, so a change between sessions is
something a person saw rather than something that arrived:

```bash
cp -R skills/* "$SKILLS_DIR"/
```

Confirm they loaded by asking for one by name, or check that each directory
holds a `SKILL.md` whose frontmatter `name` matches its directory.

A project-local skills directory, if your assistant reads one, is the better
choice when you want them to travel with the repository rather than the
machine.

## How these fit together

A typical engagement runs left to right:

1. `es-snapshot-audit` establishes the facts: what is in the repository, what is
   backup versus frozen-tier storage, how fast it grows, how much capacity a new
   repository needs.
2. `es-hybrid-migration` (or a full migration) acts on those facts.
3. `python3 -m generation_chain` audits the S3-compatible repository and writes
   a manifest of what a delete should have removed and did not. A person reads
   that manifest, and `python3 -m generation_chain.reclaim` deletes exactly what
   it names. That is Step 9 of the migration and the standing loop after it.
4. The fourth step used to be a scheduled log-driven drain that deleted the keys
   named in Elasticsearch's failed-delete WARN lines. It ran a retired sweeper
   and has no replacement, so it is withdrawn rather than rewritten.

## Safety invariants these skills preserve

One skill here has a destructive path, and one only: `es-hybrid-migration`
Step 9, which ends at `generation_chain.reclaim --execute`. The gates the removed
runbooks described, dry-run by default, a typed `DELETE <count>` confirmation, an
approved manifest, fail-safe classification and a mounted-snapshot pre-flight,
were real and were not enough: they all sat downstream of a set difference that
turned a failed read into a deletion. What changed since is the direction of that
test, not the count of gates in front of it. Read the old gates as an account of
what a delete tool needs, not as an account of what those tools achieved.

Two invariants sit outside the tools and the operator still has to supply them.

**A wrong delete in this bucket has no undo, so treat every object in it as
unrecoverable.** Earlier versions of these runbooks answered that with bucket
versioning plus a dated manifest. Through Oracle's Amazon S3 Compatibility API
that pair is not a recovery path: `ListObjectVersions` is absent from Oracle's
supported-operations list, so the version id of a deleted object can never be
discovered, and `GetBucketVersioning` and `PutBucketVersioning` are absent too,
so an operator on that surface can neither enable versioning nor confirm it.
Versions do exist on an Object Storage bucket with versioning on and are
restorable through Oracle's own API and the Console, which needs a credential
for that API rather than a Customer Secret Key. The reasoning and the operation
lists are in
[blast radius](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/blast-radius.md#issue-32-there-is-no-recovery-path-through-the-amazon-s3-compatibility-api).
This is the invariant that outlived the tools. It is why the delete is a
separate command from the audit, why its dry run is the default, and why its
approval is bound to the bytes of one manifest rather than to an operator's
intention.

**`_verify_integrity` runs after every sweep, before the next snapshot, and it is
not the whole check.** A snapshot taken after a bad delete reports `SUCCESS` and
cannot be restored, because Elasticsearch deduplicates shard files on physical
name, length and checksum and never checks that the blob is still in the store.
Skip the check and a single bad delete can seed weeks of green, unrestorable
backups. But `_verify_integrity` only walks snapshots the repository still lists,
so it reports pass after a snapshot was deleted while an index was mounted on it,
which is the loss the mounted-snapshot check exists to prevent. Pair it with
`snapshot_sizes.py --emit-classified` and require `0 mounted snapshot(s)
MISSING-FROM-CATALOG`.

Neither of those is a restore. A repository can list clean, report `SUCCESS` on
every snapshot and pass `_verify_integrity` while being unrestorable, and this
project has measured that. After deletion traffic, run
[`verify_restorable.py`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/verify_restorable.py), which restores an
index under a fresh name and counts the documents that come back. It deletes
nothing from the repository.

Evidence that these procedures work, with real measured outcomes:
[`methodology.md`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/methodology.md) (the replayable playbook) and
[`campaign-data.md`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/campaign-data.md) (the raw data from both live-rig campaigns).
