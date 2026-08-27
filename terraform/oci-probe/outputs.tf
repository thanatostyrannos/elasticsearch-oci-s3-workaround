output "namespace" {
  description = "Object Storage namespace. Part of the S3 compatibility hostname."
  value       = data.oci_objectstorage_namespace.this.namespace
}

output "probe_bucket" {
  value = oci_objectstorage_bucket.probe.name
}

output "repo_bucket" {
  value = oci_objectstorage_bucket.repo.name
}

# Two hostnames exist and picking the wrong one fails in a way that reads like a
# network or credential problem rather than a wrong endpoint. Standard works in
# every realm. The dedicated one is commercial realm OC1 only, and Oracle
# recommends it there.
output "s3_endpoint" {
  description = "S3 compatibility endpoint, standard domain. Works in every realm."
  value       = "https://${data.oci_objectstorage_namespace.this.namespace}.compat.objectstorage.${var.region}.oraclecloud.com"
}

output "s3_endpoint_dedicated" {
  description = "S3 compatibility endpoint, dedicated domain. Commercial realm OC1 only."
  value       = "https://${data.oci_objectstorage_namespace.this.namespace}.compat.objectstorage.${var.region}.oci.customer-oci.com"
}

output "s3_region" {
  description = "What SigV4 signs with on the compatibility endpoint."
  value       = var.region
}

output "s3_access_key_id" {
  description = "The access key half of the Customer Secret Key."
  value       = oci_identity_customer_secret_key.probe.id
}

output "s3_secret_access_key" {
  description = "The secret half. Oracle shows this once; Terraform holds it in state."
  value       = oci_identity_customer_secret_key.probe.key
  sensitive   = true
}

output "credentials_file" {
  description = "Path to creds.json for the audit tool, or a note if it was not written."
  value       = var.write_credentials_file ? abspath(local_sensitive_file.creds[0].filename) : "not written; write_credentials_file = false"
}

output "elasticsearch_repository_body" {
  description = "The PUT _snapshot body for pointing a cluster at the repo bucket. Register with ?verify=false while deletes are broken."
  value = jsonencode({
    type = "s3"
    settings = {
      bucket    = oci_objectstorage_bucket.repo.name
      endpoint  = "${data.oci_objectstorage_namespace.this.namespace}.compat.objectstorage.${var.region}.oraclecloud.com"
      region    = var.region
      base_path = "snapshots"
    }
  })
}
