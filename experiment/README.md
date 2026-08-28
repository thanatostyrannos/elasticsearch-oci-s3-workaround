# experiment

> [!CAUTION]
> **This is a toy. Do not use it for anything.**
>
> Nothing in this folder ships in a release, nothing here is supported, and
> nothing here is on the path an operator should take. It exists because
> someone asked whether the whole tool could be folded into a single file, and
> the answer turned out to be yes.
>
> The supported way to install this project is to copy the `generation_chain`
> directory into an empty folder. That is already one step, it leaves you with
> readable files, and it is the thing that is tested.
>
> **If you are reclaiming objects from a repository you care about, close this
> page and read [the read-only quickstart](../docs/quickstart-read-only.md).**

## genchain-all-in-one.py

The whole tool as one file: the audit, the delete path, the loop harness, the
load generator, the sizing tool, the restore checker and the checksum probe.

    python3 build_single_file.py            # regenerate it
    python3 genchain-all-in-one.py audit --help

59 source files and 557,329 bytes fold into roughly 274 KB.

### The subcommands

| Command | What it runs |
|---|---|
| `audit` | The read-only audit. Reads a repository, writes an orphan manifest, cannot delete |
| `reclaim` | The delete path. Needs a manifest and an approval digest that matches its exact bytes |
| `harness` | The audit-and-reclaim loop, one cycle at a time |
| `rig` | The load generator, which manufactures a leaking repository to test against |
| `restorable` | Restores an index under a fresh name and counts documents |
| `sizes` | Repository sizing and mounted-snapshot classification |
| `probe` | Asks a store which delete checksum it accepts, deleting nothing |
| `cycle` | Runs the loop from a config file, with the preflight checks |

Each takes `--help` and behaves as it does when run the normal way.

### It is not one giant block of pasted code

That is the part worth explaining, because it is the only reason this is
defensible rather than a novelty.

The file is 87 lines. Eighty-six of them are ordinary readable Python: a
loader, a table of subcommands, and a dispatcher. The other line is a base64
string holding a zip of the 59 source files, which `zipimport` imports at run
time. Python has been able to import from a zip since 2.3.

Nothing is rewritten, so the code that runs from one file is byte for byte the
code that runs from fifty-nine. You can check that rather than take it:

```python
import base64, io, re, zipfile
src = open("genchain-all-in-one.py").read()
z = zipfile.ZipFile(io.BytesIO(base64.b64decode(
    re.search(r'_ARCHIVE = "([^"]+)"', src).group(1))))
same = z.read("generation_chain/sources/http_reads.py") == \
       open("../generation_chain/sources/http_reads.py", "rb").read()
print(same)   # True
```

The alternative, concatenating fifty-nine modules and fixing the imports by
hand, produces something that diverges from the real thing the moment either
changes. For a tool with a delete path, "the single file and the repository
disagree about what the guard says" is not a trade worth making for
convenience.

### It was tested, once, against a real store

Against MinIO pinned to `RELEASE.2025-01-18T00-31-37Z` and Elasticsearch
9.5.2, both in a disposable namespace:

- Every subcommand starts.
- The audit against a bucket with no repository in it refused, exit code 4:
  `REFUSED: cannot read index.latest ... This run explains nothing. An empty
  manifest here is not evidence that the repository is clean.` A clean refusal
  is the correct answer there.
- After registering a repository and taking a snapshot, the audit read 20
  objects and reported 18 live at 105 KB, 0 orphaned. Exit code 0.
- `probe` reported `accepted: md5`, and that MinIO release rejected crc32,
  crc32c and sha256. That is the fault this whole project exists for, measured
  by the tool itself.
- Nothing was deleted. The bucket, the snapshot and the shard count were
  unchanged afterwards.

### Why you still should not use it

**A reviewer cannot read it.** Eighty-six lines of loader plus 271 KB of
opaque base64 is worse than fifty-nine readable files for anybody assessing a
tool that can remove data. They can extract and diff it, but they cannot read
it, and "extract it and trust your diff" is a worse story than "here are the
files".

**It writes to disk at startup.** `zipimport` needs a real file, so the
archive is written to a temporary one and removed on exit. That needs
somewhere writable, which the normal layout does not.

**Nothing keeps it in step.** It is not regenerated automatically and no test
covers it. A stale copy is a copy that lies about what it runs, which is the
one property that made the approach defensible in the first place.

It is here because a single file occasionally is the only thing that gets
through a transfer process, and because it was a satisfying thing to prove.
Rebuild it after any change to the real modules, or delete it.
