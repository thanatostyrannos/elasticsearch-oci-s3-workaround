# Credentials for Terraform itself. These are your own console user's API key,
# not the ones this configuration creates. Take them from ~/.oci/config after
# running `oci setup config`, or from the Console under your user's API keys.

variable "tenancy_ocid" {
  description = "OCID of the tenancy. Console: Profile, Tenancy."
  type        = string
}

variable "user_ocid" {
  description = "OCID of the user Terraform authenticates as."
  type        = string
}

variable "fingerprint" {
  description = "Fingerprint of that user's API signing key."
  type        = string
}

variable "private_key_path" {
  description = "Path to the PEM private key matching the fingerprint."
  type        = string
}

variable "region" {
  description = "Region identifier, for example uk-london-1. This is also the region SigV4 signs with on the S3 compatibility endpoint."
  type        = string
}

variable "compartment_ocid" {
  description = "Compartment to create the buckets in. The tenancy OCID works and puts them in the root compartment."
  type        = string
}

variable "prefix" {
  description = "Name prefix for everything this configuration creates, so it is obvious what to delete."
  type        = string
  default     = "esprobe"
}

variable "user_email" {
  description = "Email for the service user. Identity Domains tenancies refuse to create a user without one, where the classic IAM API did not. Nothing is sent to it; it is a required field on a service account that never logs in. Uses an example.com address by default, which is reserved by RFC 2606 and cannot receive mail."
  type        = string
  default     = "esprobe-service-user@example.com"
}

variable "elasticsearch_username" {
  description = "Elasticsearch user for the audit's veto. The audit refuses to run with --elasticsearch unless creds.json carries an elasticsearch section, so leaving this empty produces a file that cannot drive the documented invocation."
  type        = string
  default     = "elastic"
}

variable "elasticsearch_password" {
  description = "Password for that user. Empty by default, which omits the elasticsearch section entirely rather than writing a blank credential."
  type        = string
  default     = ""
  sensitive   = true
}

variable "write_credentials_file" {
  description = "Write creds.json for the audit tool. It contains a live secret, so it is written 0600 and gitignored."
  type        = bool
  default     = true
}

variable "empty_buckets_with" {
  description = <<-EOT
    Which CLI empties the buckets when Terraform destroys them: "oci" or "aws".

    Oracle refuses to delete a bucket that still holds objects and this
    provider has no force_destroy, so something has to empty them first.
    Both routes work.

    Defaults to "oci" because anyone already operating in OCI has that CLI,
    and because it needs no key list and no chunking. Choose "aws" when the
    AWS CLI is what you have.

    "oci" runs `oci os object bulk-delete --force`. It takes no key list and
    needs no chunking.

    "aws" lists and batch-deletes with `--sdk-checksum-algorithm CRC32C`,
    looping a thousand keys at a time because that is the DeleteObjects limit.
    CRC32C is named explicitly because the SDK default is CRC32, which Oracle
    rejects, which is the fault this whole repository exists for. It reads
    AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY from the environment: the
    module does not inject them, so they stay out of state and out of logs.
  EOT
  type        = string
  default     = "oci"

  validation {
    condition     = contains(["oci", "aws"], var.empty_buckets_with)
    error_message = "empty_buckets_with must be \"oci\" or \"aws\"."
  }
}
