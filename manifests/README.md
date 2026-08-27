# Test-rig manifests

Kubernetes manifests for the local E2E validation rig used in TEST-RESULTS.md:
ECK-managed Elasticsearch 9.5.2 plus the fault-reproducing MinIO release, run in
a Rancher Desktop (k3s) cluster under kubectl context `rancher-desktop`.

## Why this MinIO version

`minio/minio:RELEASE.2025-01-18T00-31-37Z` is pinned deliberately: it is the last
MinIO release that rejects S3 DeleteObjects requests carrying only
`x-amz-checksum-crc32` (no Content-MD5), the same behavior as OCI Object
Storage's S3-compat API. That makes it reproduce the ES 9.5.x snapshot-delete
fault exactly (the very next release, RELEASE.2025-01-20T14-49-07Z, accepts).
Do not "upgrade" it; losing the fault is losing the rig's purpose.

## Order of application

```bash
CTX="--context rancher-desktop"   # never your production context
# 1. ECK operator (downloaded, not vendored here, about 850KB of CRDs):
curl -fsSL https://download.elastic.co/downloads/eck/3.5.0/crds.yaml | kubectl $CTX create -f -
curl -fsSL https://download.elastic.co/downloads/eck/3.5.0/operator.yaml | kubectl $CTX apply -f -
# 2. Substitute every CHANGEME. See "What you must substitute" below; there
#    are more of them than there used to be, and a rebuild that misses one
#    fails in a way that does not name the setting.
# 3. The rig:
kubectl $CTX apply -f namespace.yaml
kubectl $CTX apply -f minio.yaml
kubectl $CTX apply -f minio-bucket-job.yaml
kubectl $CTX apply -f s3-credentials-secret.yaml
kubectl $CTX apply -f elasticsearch.yaml
kubectl $CTX apply -f ingress.yaml
# 4. Wait for green, then follow the E2E playbook in ../TEST-RESULTS.md:
kubectl $CTX get elasticsearch -n es-rig -w
```

## What you must substitute

Nothing here ships with working values, because the repository is public. A
rebuild that misses one of these fails in a way that does not name the missing
setting, so check all of them.

| Placeholder | Files | What it is |
|---|---|---|
| `CHANGEME-access`, `CHANGEME-secret` | `minio.yaml`, `minio-bucket-job.yaml`, `s3-credentials-secret.yaml` | MinIO root credentials. **The value must match across all three** or Elasticsearch authenticates against a MinIO that does not know it. |
| `CHANGEME-oci-access`, `CHANGEME-oci-secret` | `s3-credentials-secret.yaml` | An Oracle Customer Secret Key. Only needed to test against a real Oracle bucket. |
| `CHANGEME-namespace`, `CHANGEME-region` | `elasticsearch.yaml` | Your Oracle tenancy namespace and region, in `s3.client.oci.endpoint`. Placeholders because the namespace identifies your tenancy. |

Two settings in `elasticsearch.yaml` need no substitution but are easy to lose,
and both were configured out-of-band on an earlier rig and only added here
after a rebuild from this directory could not start:

- `xpack.searchable.snapshot.shared_cache.size`. Static, so
  `PUT _cluster/settings` will not set it. Without it a `data_frozen` node
  cannot mount a partial searchable snapshot and the churn rig refuses to
  start. It is off-heap and counts against the container memory limit, so
  budget for it alongside the heap.
- `s3.client.oci.disable_chunked_encoding`. Oracle answers 501 to
  chunked-encoding uploads.

If a repository registered with `--s3-client oci` reports *"The AWS Access Key
Id you provided does not exist in our records"*, the endpoint is missing and
the request went to real AWS S3. It is not a credential problem.

## Reaching it

Every service is ClusterIP, so `ingress.yaml` publishes Elasticsearch and MinIO
through Traefik, which is already the controller in this cluster. Use that
rather than `kubectl port-forward`: a forward is a process on your workstation
and it dies with the pod it points at. When Elasticsearch was OOMKilled during
a test the forward went with it, and the harness failed minutes later on a
connection refused, which reads like a cluster fault rather than a dead tunnel.

Map the hosts to wherever Traefik answers on your machine. On Rancher Desktop
that is localhost, NOT the address `kubectl get ingress` prints in its ADDRESS
column, which is the in-VM address an outside client cannot reach:

```bash
echo "127.0.0.1  es-rig.demo minio-rig.demo minio-console.demo" | sudo tee -a /etc/hosts
```

Then `http://es-rig.demo` replaces `http://localhost:9200`. To check the route
without editing `/etc/hosts`, send the header yourself. A 401 means
Elasticsearch was reached and wants credentials:

```bash
curl -H "Host: es-rig.demo" http://127.0.0.1/
```

Elastic password: `kubectl --context rancher-desktop get secret rig-es-elastic-user -n es-rig -o go-template='{{.data.elastic | base64decode}}'`

It is regenerated whenever the cluster is recreated, so re-read it after a
rebuild rather than reusing a cached copy. A stale one returns 401 through the
ingress, which looks exactly like a broken route.

Credentials note: the original rig used throwaway local-only values; they are
scrubbed to CHANGEME placeholders here because this repo is public. Nothing in
this rig is reachable from outside your machine, but don't publish real values.
