# Running this tool from your own GitLab

Two self-contained pipelines, for an operator with a private GitLab instance,
their own Elasticsearch, and their own Oracle Object Storage bucket.

| | [`readonly-scan/`](readonly-scan/README.md) | [`kubernetes-test-rig/`](kubernetes-test-rig/README.md) |
|---|---|---|
| Runs | `python3 -m generation_chain` | the load generator, the audit-and-reclaim loop, a scheduled audit |
| Can it delete? | No. Not structurally capable of it. | Yes, once you explicitly turn deletes on. |
| Where | Any GitLab runner with Python 3.9+ | A Kubernetes cluster, via Helm |
| Schedule | Yes. This is the one built for a schedule. | No. Refuses to trigger from one. |
| Start here if | you have a repository to check and want a number back | you want to prove the delete path works before trusting it on data you cannot lose |

## Which one do you need

Start with `readonly-scan`. It answers "how much is leaking, and where" for
any repository you can reach, and there is no way to make it remove
anything: the audit's HTTP transport permits `GET` and `HEAD`, and the one
`POST` that lists a bucket, and refuses anything else at the transport
level. It does not import the package that deletes.

Move to `kubernetes-test-rig` once you have a manifest from the scan and
want to reclaim the space, or you want to qualify this tool against your own
Oracle Object Storage before pointing it at data you cannot afford to lose.
It deploys the load generator that manufactures a leaking repository on
purpose, the loop that audits and reclaims what it finds cycle after cycle,
and a scheduled read-only audit CronJob for whatever cluster it lands in.

Most operators need only `readonly-scan`, run on a schedule, for as long as
the leak this tool works around exists upstream. `kubernetes-test-rig` is
for the one-time (or occasional) job of proving the fix works and reclaiming
what has already leaked.

## Setting up either one

1. Copy the directory (`readonly-scan/` or `kubernetes-test-rig/`) into a
   GitLab project that also holds a copy of this tool's source: either this
   whole repository, or at least `generation_chain/` and, for the test rig,
   `snapshot_churn_rig.py` and `reclaim_test_protocol.py` too.
2. Follow that directory's own README for the CI/CD variables or Helm
   values it needs.
3. Read [docs/quickstart-read-only.md](../docs/quickstart-read-only.md) and,
   for the test rig, [docs/quickstart-test-rig.md](../docs/quickstart-test-rig.md)
   first. Both pipelines automate the steps those pages walk through by
   hand; they do not change what the underlying tools do.

## The rule both pipelines follow

The audit (`python3 -m generation_chain`) and the delete tool
(`python3 -m generation_chain.reclaim`) are two separate programs. A
previous draft of this CI integration called the delete tool's module path
for what was supposed to be the read-only scan, which would have made the
"safe" pipeline capable of deleting. Both pipelines here call the audit by
its correct module path, `generation_chain` with no `.reclaim` on the end,
and `readonly-scan/.gitlab-ci.yml` has no `--execute` flag anywhere in it
because the audit does not have one. If you are extending either pipeline,
keep that separation: the safety property this whole split depends on is
that the thing capable of deleting is never the thing on the easy path, or
the schedule.
