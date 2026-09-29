# A disposable OKE cluster for the test rig

This builds somewhere to run the churn rig and the audit that is not your own
Elasticsearch. The point is that nobody has to point this tooling at a cluster
they care about in order to find out whether it works.

`terraform apply` creates it. `terraform destroy` is the whole teardown.

## What it makes

A VCN with the three subnets OKE needs, an internet gateway for the public
control plane endpoint, a NAT gateway so private nodes can reach out, and a
service gateway so traffic to Object Storage never leaves Oracle's network.
Then an enhanced OKE cluster, one virtual node pool on Ampere pods, and a File
Storage file system for Elasticsearch's data path.

The security list rules were read back from a cluster the console's quick-create
wizard built, rather than guessed. One rule is deliberately not reproduced:
the wizard opens SSH from `0.0.0.0/0`, and a virtual node has no host to ssh
into, so the rule grants reachability to nothing and only widens what a
reviewer has to think about.

## Why virtual nodes, and what that costs you

Virtual nodes are pods without machines. Nothing to patch, nothing to size, and
the bill follows each pod's requests rather than a node that sits mostly idle.
Ampere `A1` is the cheapest shape OKE offers and the rig has no x86 dependency.

The catch is the cluster fee. Virtual nodes require an **enhanced** cluster,
which is $0.10 an hour capped at $74.40 a month. For this rig that is the
largest line in the bill, larger than the Elasticsearch pod:

| Item | Per month, running continuously |
|---|---|
| Enhanced cluster | $74.40 |
| Elasticsearch pod, 1 vCPU and 8 GiB | ~$19.71 |
| Rig jobs, about 1.1 vCPU and 1.2 GiB combined | ~$13.05 |

A rig that runs in campaigns should be destroyed between them. Two hours of
testing costs well under a dollar; a month of leaving it up costs a hundred.

## Storage, and the caveat that matters

Elasticsearch's data path is on **File Storage**, not Block Volume.

That is not a preference. OKE virtual nodes do not support Block Volume
persistent volumes. File Storage support for virtual nodes arrived on
28 August 2026, and it is the reason running Elasticsearch on virtual nodes is
possible at all.

**File Storage is NFS, and Elastic advise against NFS for a data path.** For a
rig whose question is "does the audit correctly identify leaked objects", that
is very likely fine: the rig measures correctness, not indexing throughput. For
anything where Elasticsearch performance is the subject, it is the wrong
storage and you want a managed node pool with Block Volume instead.

Treat the first run on File Storage as something to verify rather than assume.

## Getting to it

```
cp terraform.tfvars.example terraform.tfvars   # then fill it in
terraform init
terraform apply
eval "$(terraform output -raw kubeconfig_command)"
kubectl get nodes
```

`terraform output` also gives the File Storage mount target address and export
path, which the Elasticsearch persistent volume needs.

## Tearing it down

```
terraform destroy
```

Everything here is created by this configuration and named from `prefix`, so a
destroy leaves nothing behind. Object Storage buckets are not part of this
module; the bucket the rig writes into lives in `../oci-probe`.
