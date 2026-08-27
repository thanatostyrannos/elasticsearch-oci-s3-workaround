# The test rig, on your own Kubernetes cluster

Deploys the load generator, the audit-and-reclaim loop, and a scheduled
read-only audit, as a Helm chart. Use this once you have proved the audit
finds what you expect on [the read-only scan](../readonly-scan/README.md)
and want to exercise the delete path against a repository you can afford to
lose.

**Read [the test-rig quickstart](../../docs/quickstart-test-rig.md) first.**
This chart automates exactly the steps that page walks through by hand: it
does not change what the tools do, only how they are deployed.

## What gets deployed

| Resource | Tool | Values group | Safe to schedule |
|---|---|---|---|
| Job `<release>-churn-rig` | `snapshot_churn_rig.py run` | `churnRig` | n/a (runs once, for `churnRig.duration`) |
| Job `<release>-qualify` | `reclaim_test_protocol.py` | `qualify` | **No.** Manual only. |
| CronJob `<release>-audit` | `python3 -m generation_chain` | `auditCronJob` | **Yes.** Read-only. |
| Job `<release>-teardown-hook` | `snapshot_churn_rig.py teardown` | `teardown` | runs on `helm uninstall` |
| Job `<release>-teardown-manual` (suspended template) | same | `teardown.standalone` | triggered by hand |

`values.yaml` exposes every flag every one of these commands accepts,
grouped by tool, with a comment on each taken from that tool's own `--help`
text. Nothing is hidden behind a chart-level abstraction; if you have read
`docs/quickstart-test-rig.md`, every value maps to a flag you already know.

## Why the audit is a CronJob and the loop is not

The root `.gitlab-ci.yml` and this chart share the same rule: the read path
cannot delete. The loop's own `generation_chain.reclaim` calls (made
internally by `reclaim_test_protocol.py`) go through a dry-run and
approval-digest handshake before anything is removed, but that handshake is
a check inside a program, not a structural guarantee the way the audit's
transport-level GET/HEAD-only restriction is. Keeping the loop off any
schedule is the belt to that handshake's suspenders: nothing here can ever
put a delete-capable run on a timer by accident.

## Set this up

1. `cp gitlab/kubernetes-test-rig/chart/values.yaml my-values.yaml` and edit
   the non-secret fields: endpoints, bucket, prefix, repository name,
   `elasticsearch.url` or `elasticsearch.eck.enabled`.
2. Create a second file for secrets, never committed:
   ```yaml
   credentials:
     s3:
       accessKeyId: "..."
       secretAccessKey: "..."
     elasticsearch:
       authMethod: apiKey
       apiKey: "..."
     harnessEsUser: "elastic"
     harnessEsPassword: "..."
   ```
3. Install:
   ```bash
   helm install rig gitlab/kubernetes-test-rig/chart \
     --namespace es-rig --create-namespace \
     -f my-values.yaml -f my-secrets.yaml
   ```
4. Watch the loop: `kubectl -n es-rig logs -f job/rig-es-rig-qualify` (the
   exact name is printed in `helm install`'s NOTES; it also carries the
   label `rig-component=qualify` if you would rather look it up:
   `kubectl -n es-rig get job -l rig-component=qualify`).
5. Tear down: `helm uninstall rig -n es-rig`. Read "Teardown" below before
   you trust that this alone proves the rig is gone.

`gitlab/kubernetes-test-rig/.gitlab-ci.yml` wraps steps 3 through 5 as a
GitLab pipeline: a manual `deploy:rig` job, a `qualify:wait` job that blocks
on the loop and publishes its log as an artifact, and a `teardown:rig` job
that always runs. It takes a values override file and a File-type secrets
override file rather than re-declaring every flag as a CI/CD variable
itself; see the comment at the top of that file for why duplicating
`values.yaml` into CI/CD variables would be the wrong design, not a
shortcut.

## Deletes stay off until you turn them on

`qualify.dryRunOnly` defaults to `true`. Every cycle still audits and dry
runs; nothing executes until you set it to `false`, and even then each
cycle's execute step is gated by the fresh approval digest that cycle's own
dry run printed, not by anything this chart adds. Read
[the read-only quickstart's exit-code section](../../docs/quickstart-read-only.md#when-it-refuses)
and the test-rig quickstart's "Reading the output" section before your
first live run.

## Elasticsearch: external by default, no client certificates anywhere

`elasticsearch.external` defaults to `true`, because not every cluster this
chart is installed into also runs Elasticsearch, and a cluster you already
have is usually the one you want to test against. Set `elasticsearch.url`,
and `elasticsearch.caCert` if it serves a certificate the `python:3.12-slim`
image does not already trust.

Set `elasticsearch.external: false` to deploy the in-cluster, ECK-managed
Elasticsearch this chart templates from this project's own lab manifest
instead (the ECK operator itself is not installed by this chart).

This tool has **no client-certificate (mTLS) support** anywhere in its
Elasticsearch path. Every credential is a password, an API key, or basic
auth. There is no `elasticsearch.clientCert` value and there should never be
one; if a future change adds mTLS to the underlying tools, add the value
then, backed by a real flag, not before.

One real gap worth knowing about: `reclaim_test_protocol.py`'s own calls to
the cluster (`--es-user` / `--es-password-file`, used to create ILM/SLM
policies and to wait for shards to settle in segment mode) take **no**
`--ca-cert` or `--insecure` flag in this tool's current CLI, unlike
`snapshot_churn_rig.py run` and the audit, which do. If your external
cluster's certificate is not already trusted by `python:3.12-slim`, the
`qualify` Job's own cluster calls will fail TLS verification even though
`elasticsearch.caCert` is set (the load generator and the audit are
unaffected; they read `--ca-cert` / `--es-ca-cert` correctly). This is a
limitation in the tool, not something this chart works around.

## Teardown

The rig's state lives inside Elasticsearch (the ILM policy, the SLM policy,
the data stream, the repository registration) and inside the bucket (the
leaked objects, by design, are still there for measurement), nowhere a
Kubernetes controller tracks on its own. `helm uninstall` alone does not
prove any of that is gone.

- `teardown.hook.enabled` (default `true`) runs `snapshot_churn_rig.py
  teardown` automatically as a **pre-delete** Helm hook, before this
  release's own resources (including the state PVC teardown reads
  `--state-file` from) are removed. A post-delete hook would run after that
  PVC was already gone; that ordering choice is why this is pre-delete.
- `teardown.standalone.enabled` (default `false`) renders a second copy of
  the same Job as an always-present, `suspend: true` template you can
  trigger by hand if the hook's pod never started:
  ```bash
  kubectl -n es-rig create job --from=job/rig-es-rig-teardown-manual retry-1
  ```
- `teardown.purgeBucket` (default `true`) also removes the leaked blobs with
  single-object deletes, which succeed even where the batch delete that
  causes the leak fails. Set it to `false` if you want the leaked corpus to
  survive teardown for further measurement; the qualification run itself
  already exercised the delete path, this flag only controls final cleanup.

The teardown Job's own output names the `oci os object bulk-delete` command
to run by hand if the bucket is not empty afterward. Trust that line over
`helm uninstall` reporting success.

## State storage note

The load generator's `--state-file` and `--report-file`, and the loop's
`--out` directory, all live on one `ReadWriteOnce` PersistentVolumeClaim
(`<release>-state`) so the churn-rig Job, the qualify Job, and both teardown
Jobs can all read it. `ReadWriteOnce` is enough on a single-node cluster
(this project's own lab manifests target Rancher Desktop, a single node). On
a multi-node cluster, either use a StorageClass that supports
`ReadWriteMany`, or keep these pods on the same node with a `nodeSelector`.

## Every CLI option in this chart, and the few left out

Every flag in `python3 -m generation_chain --help`, `python3
snapshot_churn_rig.py run --help`, `python3 snapshot_churn_rig.py teardown
--help`, and `python3 reclaim_test_protocol.py --help` is a value in
`chart/values.yaml`, except:

- `generation_chain --self-test` and `--local-repo` are in `values.yaml`
  (`auditCronJob.localRepo`) for completeness, but a `--local-repo`
  scan makes little sense from inside a CronJob pod with no persistent
  mirror mounted; it is there because the flag exists, not because this
  chart makes it practical to use.
- `generation_chain.reclaim`'s own flags (`--checksum-algorithm`,
  `--approve-digest`, `--approve-rows`, `--max-manifest-age`, and so on) are
  **not** chart values, because this chart never calls that tool directly.
  `reclaim_test_protocol.py` calls it internally, one cycle at a time, and
  does not expose those flags itself for the harness to forward. If you need
  to run `generation_chain.reclaim` with specific values for those flags
  outside the loop, do it by hand against a manifest from
  [the readonly-scan pipeline](../readonly-scan/README.md).
