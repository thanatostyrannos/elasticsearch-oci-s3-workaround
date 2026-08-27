# Read-only scan, on your own GitLab

Runs the audit (`python3 -m generation_chain`) on a schedule and publishes an
orphan manifest and a summary as pipeline artifacts. This is the safe entry
point: the audit's HTTP transport permits `GET` and `HEAD`, and the one `POST`
that lists a bucket, and refuses anything else. It does not import the package
that deletes. There is no way to make this pipeline remove an object.

If you have not read it yet, [the read-only quickstart](../../docs/quickstart-read-only.md)
explains what the audit reports and how to read `orphans.tsv`.

## Set this up

1. Copy `.gitlab-ci.yml` from this directory into the root of a GitLab
   project that also holds a copy of this tool (either this whole repository,
   or at least the `generation_chain/` package).
2. Under Settings, CI/CD, Variables, add:
   - `CREDS_JSON`, **type File**, containing:
     ```json
     {"s3": {"access_key_id": "...", "secret_access_key": "..."},
      "elasticsearch": {"api_key": "..."}}
     ```
     Set the `s3` section from an Oracle **Customer Secret Key** (Identity,
     Users, your user, Customer Secret Keys), not your console password.
     Leave the `elasticsearch` section out if you are not setting
     `GENCHAIN_ELASTICSEARCH`. To authenticate with a username and password
     instead of an API key, replace `"api_key": "..."` with
     `"username": "...", "password": "..."`; nothing else changes.
3. Set the other variables listed in `.gitlab-ci.yml` under "what to scan":
   at minimum `GENCHAIN_ENDPOINT`, `GENCHAIN_REGION`, `GENCHAIN_BUCKET`, and
   `GENCHAIN_PREFIX` (your repository's `base_path` inside the bucket, with
   the trailing slash kept).
4. Add a schedule under CI/CD, Schedules, targeting the branch that holds
   this file. The audit job runs automatically on a schedule pipeline; on any
   other pipeline it is a manual job, so pushes to the branch do not spend
   money on a network read.

## Why API key by default

`CREDS_JSON`'s `elasticsearch` section defaults to `api_key` in the example
above. An API key can be scoped and revoked on its own; a leaked username and
password takes the whole account with it, and CI variables are exactly the
kind of place a credential leaks from (a misconfigured job, a forked pipeline,
a maintainer who can read protected variables they should not act on).
Username and password still work if you need them: see step 2.

## What you get back

Every run, whether it found nothing or found a large orphaned set, publishes
as pipeline artifacts (90 day expiry):

- `orphans.tsv` is the manifest, every key this run condemned, tab separated,
  with `# derivation complete` as its last line if the run was not refused.
- `audit-summary.txt` is the human report: repository coverage, then the
  orphaned, live and unexplained counts and their byte totals.
- `classification.tsv` lists every key the run saw and its disposition, if
  `GENCHAIN_WRITE_CLASSIFICATION` is `"yes"` (the default). Useful for
  confirming a specific key you expected to be live.
- `coverage.json` is the machine-readable version of the summary, if
  `GENCHAIN_WRITE_COVERAGE_JSON` is `"yes"` (the default). Diff this against a
  prior run to see whether coverage is improving or degrading over time.

Read `audit-summary.txt`'s **unexplained** count before trusting a short
`orphans.tsv`. Unexplained is not known garbage, it is what the run could not
decide either way, and a run that could not read some of the repository
reports more there and fewer orphans, never the reverse.

## When something needs deleting

This pipeline cannot do it, on purpose. Take `orphans.tsv` to
[the kubernetes-test-rig pipeline](../kubernetes-test-rig/README.md) or run
`python3 -m generation_chain.reclaim --manifest orphans.tsv ...` by hand,
starting with a dry run. Read
[the test-rig quickstart](../../docs/quickstart-test-rig.md) first if you have
not exercised the delete path against a repository you can afford to lose.

## CLI options this pipeline does not expose, and why

- `--self-test` proves the signing and framing offline, a smoke test for
  contributors to this tool, not something an operator runs per scan.
- `--memory-mb` and `--max-ram` are both variables here
  (`GENCHAIN_MEMORY_MB`, `GENCHAIN_MAX_RAM`) but the tool refuses both passed
  together. Set at most one.

Everything else `python3 -m generation_chain --help` lists is a variable in
`.gitlab-ci.yml`.
