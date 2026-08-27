# An OCI bucket to settle the questions MinIO cannot

Everything in this repository was reproduced against MinIO pinned to the last
release that rejects the batch delete. That proves the fault and it cannot
answer three questions, because they are about Oracle's endpoint specifically.

This configuration stands up the smallest thing that can answer them.

## What it creates

| Resource | Why |
|---|---|
| `<prefix>-probe` bucket | The checksum and pagination experiments. A handful of objects. |
| `<prefix>-repo` bucket | Where an Elasticsearch snapshot repository points, if you want the fault end to end. |
| A user, group and policy | So the keys below can be revoked without touching your console login. Scoped to those two buckets. |
| A Customer Secret Key | The S3 compatibility credential. This is what the `s3` transport and Elasticsearch's own s3 plugin need. |
| An API signing key | For `--transport oci`, generated locally so there is nothing to paste. |
| `creds.json`, mode 0600 | What the audit tool reads. The tool refuses a file other users can read. |

Versioning is left `Disabled` on purpose. A versioned bucket turns a delete into
a delete marker, which is not the behaviour a real snapshot repository sees, and
testing against it would answer a different question than the one asked.

## What it costs, measured

Always Free gives 20 GB of storage and 50,000 API requests a month. You will
exceed the requests, and it does not matter, because Oracle charges **$0.0034
per 10,000 requests**. That is 34 cents per million.

One full cycle against a repository of 191,773 objects, counted by instrumenting
the transport rather than estimated:

```
audit        31,142 requests    30,161 HEAD, 981 GET
delete           31 requests    31 batches of up to 1,000 keys
one cycle    31,173 requests
```

The audit is almost all HEAD, because every condemned key is confirmed present
before it is named. The delete is nearly free because the batch response carries
a per-key result, so nothing has to go back and check.

| | requests | cost |
|---|---|---|
| one cycle | 31,173 | under a cent |
| 100 cycles | 3,117,300 | **$1.04** |

Storage is the larger line and still small: 93 GB is $1.86 a month after the
20 GB that is free. Egress matters only when something restores; the audit reads
981 small documents and sends HEADs that carry no body.

So the free tier is a threshold you cross, not a budget you have to plan around.
Nothing stops when you cross it. You start paying cents.

## Running it

You need an OCI account and a working `~/.oci/config`. `oci setup config`
creates one.

```bash
cd terraform/oci-probe
cp terraform.tfvars.example terraform.tfvars   # then fill it in
terraform init
terraform plan
terraform apply
```

Then:

```bash
terraform output s3_endpoint
terraform output -raw credentials_file
```

## The three questions

**1. Does OCI accept CRC32C on `DeleteObjects`?** Oracle's documentation lists
`x-amz-checksum-sha256` and `x-amz-checksum-crc32c` as alternatives to
`Content-MD5`. Elasticsearch sends `x-amz-checksum-crc32`, which is a different
algorithm and not on that list. If CRC32C is accepted, then a one line change in
Elasticsearch, `.checksumAlgorithm(ChecksumAlgorithm.CRC32_C)` in
`S3BlobStore.bulkDelete`, is a real fix rather than a guess.

```bash
python3 -m generation_chain.reclaim --checksum-algorithm crc32c ...
```

**2. Does `ListObjectsV2` page?** Oracle's supported operations list names only
`ListObjects`. Their own tutorial calls V2 successfully but never crosses a page
boundary. Three objects and `--max-keys 2` settles it:

```bash
aws s3api list-objects-v2 --bucket "$(terraform output -raw probe_bucket)" \
  --max-keys 2 --endpoint-url "$(terraform output -raw s3_endpoint)" --output json
```

`KeyCount` present with a populated `NextContinuationToken` means V2 works.
Their absence, with `IsTruncated: true` still there, means the store answered V1.

**3. Does the reclaim path work against Oracle?** It is proven on MinIO: without
`Content-MD5` a batch delete returns 400, with it returns 200 and the keys are
gone. Oracle is the endpoint that matters and has never been tested.

## Pointing an existing cluster at it

```bash
terraform output -raw elasticsearch_repository_body
```

Register with `?verify=false`. Verification writes test objects and deletes them
with the same broken batch call, so on an affected cluster registration fails on
the delete rather than on anything being wrong with the bucket. Those
`tests-<uuid>/` leftovers are the fault's first fingerprint, and they stay.

Elasticsearch needs the same Customer Secret Key in its keystore:

```bash
bin/elasticsearch-keystore add s3.client.default.access_key
bin/elasticsearch-keystore add s3.client.default.secret_key
```

Then reload, cluster wide:

```bash
curl -XPOST "$ES/_nodes/reload_secure_settings"
```

### Keeping it away from real data

This matters if the cluster holds anything you care about. `snapshot_churn_rig.py`
namespaces everything it creates under `--prefix`, and writes only where you
point it:

```bash
python3 snapshot_churn_rig.py run \
  --es "$ES" --user elastic --password-file espw \
  --prefix octest \
  --repo-type s3 \
  --bucket "$(terraform output -raw repo_bucket)" \
  --base-path octest \
  --shards 4 --docs-per-second 200 \
  --snapshot-interval 5m --delete-min-age 15m \
  --duration 2h
```

That creates `octest-repo`, `octest-ilm`, `octest-template`, `octest-stream` and
`octest-slm`, and nothing else. The isolation that makes it safe is in the SLM
policy the harness writes:

```json
"config": {"indices": ["octest-stream"]}
```

Scoped to its own data stream, never `*`. It cannot snapshot an index it did not
create, so it will not pull production data into the test bucket even sharing a
cluster with it.

`--base-path` keeps several rigs apart inside one bucket, so the probe
experiments and a repository under test do not collide.

One trap worth knowing: `indices.lifecycle.poll_interval` defaults to ten
minutes, so a one minute `--delete-min-age` does nothing for ten. The harness
sets `--ilm-poll-interval` for you; if you build the policies by hand, set it
yourself or the fast lifecycle is fast only on paper.

## Secrets

**The Customer Secret Key lands in Terraform state in plaintext.** State is
gitignored here, and that is the only thing protecting it. Do not commit state,
do not put it in a shared backend without encryption, and revoke the key when
you are done rather than leaving it live.

`creds.json` and `oci_api_key.pem` are written into this directory at 0600 and
gitignored for the same reason.

## Tearing it down

```bash
terraform destroy
```

That is the whole procedure. Both buckets empty themselves first.

Oracle refuses to delete a bucket that still holds objects, and
`oci_objectstorage_bucket` has no `force_destroy` the way the AWS provider
does, so a plain destroy after a test run used to fail with `BucketNotEmpty`.
Each bucket now has a `terraform_data` resource carrying a destroy-time
provisioner that empties it first, using whichever CLI you selected:

```
oci os object bulk-delete -ns <namespace> -bn <bucket> --force
```

Destroy runs in reverse dependency order, so the emptying happens before the
bucket delete. **This needs the OCI CLI on your PATH, configured for the same
tenancy.** Without it the destroy fails at the provisioner rather than at the
bucket, which is at least a clearer error.

`aws s3 rm --recursive` will not do this, and the reason is worth knowing
because it is the fault itself. That command sends `DeleteObjects` with the
SDK's default CRC32 checksum, which Oracle rejects.

The algorithm is selectable, just not there. Measured on aws-cli 2.36.31:

| Command | `--checksum-algorithm` |
|---|---|
| `aws s3 rm --recursive` | absent |
| `aws s3api delete-objects` | present |

So the batch delete does work against Oracle once you name an algorithm it
accepts, which is CRC32C, SHA256 or `Content-MD5`. List the keys, then delete
them:

```bash
OBJECTS=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX/" \
  --query "Contents[].{Key: Key}" --output json)

aws s3api delete-objects --bucket "$BUCKET" \
  --sdk-checksum-algorithm "CRC32C" \
  --delete "{\"Objects\": $OBJECTS}"
```

Both `--sdk-checksum-algorithm` and `--checksum-algorithm` are accepted on
aws-cli 2.36.31; the second is the one in `help`.

**That works up to a thousand objects and then stops working.**
`delete-objects` takes at most 1000 keys per call, while the CLI auto-paginates
`list-objects-v2`, so on any bucket a test actually filled, `$OBJECTS` comes
back larger than the delete will accept. Chunk it, or the command fails on a
full bucket and succeeds on an empty one, which is the worst way round to find
out.

Which of the two runs on destroy is yours to choose:

```hcl
empty_buckets_with = "oci"   # default
empty_buckets_with = "aws"
```

`oci` is the default because anyone already operating in OCI has that CLI, and
because it needs no key list and no chunking. `aws` runs the loop above,
naming CRC32C explicitly, and reads `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` from your environment. The module does not inject
them, so they stay out of Terraform state and out of the destroy log.

Whichever you pick, the CLI has to be on PATH when you run `terraform
destroy`. If it is not, the destroy fails at the provisioner rather than at
the bucket, which is at least the clearer error.

### Teardown belongs at the end of a test, not the start of the next one

A cycle that destroys what it created leaves nothing to wait for. A cycle that
skips it leaves the next run to discover tens of thousands of leaked blobs and
clear them before it can even begin measuring, which is how a teardown turns
into a startup cost. The `known-state-test-cycle` skill has the full ordering.

## What has been checked, and what has not

`terraform fmt`, `init` and `validate` pass against the real Oracle provider.

**Applied against a real tenancy on 2026-08-26**, and it got most of the way:
both buckets, the group and the policy were created. The user creation failed,
and the failure is worth knowing about.

## The Identity Domains gotcha

On a tenancy using Identity Domains, which is what a new account gets,
`CreateUser` refuses without a primary email:

```
400-IdcsConversionError
"The primary email must be specified."
error.identity.user.primaryEmailNotSpecified
```

The classic IAM API never asked for one, so a configuration written against the
older behaviour fails here, and it fails **after** the buckets already exist.
Terraform is fine with that: re-running continues from where it stopped.

Fixed by setting `email` on the user, defaulting to an `example.com` address.
That domain is reserved by RFC 2606 and cannot receive mail, which is what you
want on a service account that never logs in. Override with `user_email` if your
tenancy requires something deliverable.

The same tenancy shape shows up elsewhere: `oci iam user get` returns 401 while
Object Storage calls succeed with the same credentials, because the user lives
in a domain and the legacy endpoint will not answer for it. Object Storage is
unaffected, which is all this configuration needs.
