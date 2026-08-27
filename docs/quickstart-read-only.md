# Reading a leaking repository, without deleting anything

**Do not use this release to delete. The delete path is not signed off yet.**
It exists, it refuses far more than it used to, and it has not been through a
full run against a real bucket since the last round of fixes. Read the report,
keep the file, delete nothing.

What this gets you: a count of orphaned objects, how much space they occupy,
and a file listing every one of them.

## Before you start

- Python 3.9 or later. Nothing else. The audit imports only the standard
  library and there is no dependency file to install.
- Read access to the bucket. The audit cannot delete: its transport allows
  `GET` and `HEAD` and refuses anything else, and the package that deletes is
  not importable from the audit at all.
- Your repository's `base_path`, which is the prefix inside the bucket.

## The credentials file

A path, never a value on the command line, because a secret in `argv` is
visible in `ps` to every user on the host. The file must be `0600` or `0400`
or it is refused.

```json
{
  "s3": {
    "access_key_id": "<<<Redacted>>>",
    "secret_access_key": "<<<Redacted>>>"
  }
}
```

On Oracle these are a **Customer Secret Key**, not your console password and
not an API signing key. Create one under Identity, Users, your user, Customer
Secret Keys. Oracle shows the secret once, at creation.

```bash
chmod 600 creds.json
```

## Get the report on screen

```bash
python3 -m generation_chain \
  --transport s3 \
  --endpoint "https://<namespace>.compat.objectstorage.<region>.oraclecloud.com" \
  --region "<region>" \
  --bucket "<bucket>" \
  --prefix "<base_path>/" \
  --credentials creds.json \
  --manifest orphans.tsv
```

`<namespace>` is your Object Storage namespace, which Oracle shows on the
bucket's detail page. Both the report and the progress lines go to **stderr**,
so you see them as they happen and they cannot be confused with the file.

While it runs it tells you where it is:

```
[00:07:05] listed 12,742 objects
[00:07:19] read the generation chain: 195 generation(s) believed, current 194
[00:07:19] reading 200 shard directories in 1 group(s). This is the slow part
```

**Expect that last phase to be slow and quiet.** It reads one shard document
per directory per generation, and nothing ever removes a generation, so a
repository that has been leaking for a while costs more to read than a fresh
one. Ten minutes is normal. It has not hung. `--quiet` turns the progress off.

The numbers you asked for are at the end:

```
Dispositions
  orphaned: 2311, 105.42 MB
  live: 288, 101.47 MB
  unexplained: 3601, 872.93 MB

Reclaimable
  105.42 MB across 2311 orphaned objects (105,422,499 bytes)
```

Read those three lines carefully, because only one of them is a list of things
to delete.

- **orphaned** is what a delete already stranded, and it is what the file
  contains.
- **live** is still referenced. Never touch it.
- **unexplained** is not known garbage. It is what this run could not decide
  either way, and some of it is live. A run that could not read some shard
  directories reports more here and fewer orphans, which is the safe
  direction: an unreadable directory produces a shorter list, never a longer
  one.

## Keep the file

`--manifest orphans.tsv` is the file. It is written whether or not you ever
delete, and it is tab separated with a header:

```
key  reason  category  snapshot_uuid  snapshot_name  from_generation  to_generation
```

Just the object names:

```bash
tail -n +2 orphans.tsv | grep -v '^#' | cut -f1 > orphan-keys.txt
wc -l orphan-keys.txt
```

Only segment blobs, which are the data:

```bash
awk -F'\t' '$3 == "segment blob" {print $1}' orphans.tsv | wc -l
```

Total bytes, checked against the report:

```bash
awk -F'\t' 'NR > 1 && $1 !~ /^#/ {n++} END {print n, "keys"}' orphans.tsv
```

To keep the report as well as the file:

```bash
python3 -m generation_chain ... --manifest orphans.tsv 2> report.txt
```

The last line of the manifest is `# derivation complete`. If it is missing,
the run refused partway and the file is not a manifest. Nothing will act on
one without it.

## Ask the cluster too, if you have one

```bash
  --elasticsearch "http://your-cluster:9200" \
  --es-repository "<repository name as Elasticsearch knows it>"
```

This adds a section to your credentials file:

```json
  "elasticsearch": { "username": "elastic", "password": "<<<Redacted>>>" }
```

It only ever removes keys from the list. It protects blobs behind mounted
searchable snapshots, which the bucket alone cannot show you, because
Elasticsearch will happily let a snapshot backing a mounted index be deleted
and its retention policy will reap one on schedule.

Use it if you can. Without it the report is still honest, it just cannot see
mounts.

## What cannot happen

Worth knowing before you point this at production.

The audit reads. Its HTTP transport permits `GET` and `HEAD` and raises on
anything else, and that refusal is a raise rather than an assertion, so
`python3 -O` cannot strip it. The audit path does not import the package that
deletes, and a test fails if a future change makes it.

What it names is a subset of what Elasticsearch itself would collect. Its rule
is Elasticsearch's rule, the set difference inside one shard directory between
the segment blobs present and the ones the current file list names, and then
it keeps only those it can attribute to a delete it actually observed. An
intersection cannot add a member.

A read that fails shrinks the list. Every gate that notices a problem reacts by
dropping a shard directory, so a repository being written while you read it
yields fewer names, never different ones.

## When it refuses

It refuses rather than guessing, and the message says what to do. The three you
are most likely to meet:

- **The credentials file is group or world readable.** `chmod 600` it.
- **`--region` is wrong.** A wrong region and a wrong endpoint both answer a
  bare 403, so it is never defaulted.
- **No `elasticsearch` section, but you passed `--elasticsearch`.** The audit
  reads its cluster credential from the credentials file. There is no flag
  that takes a password.
