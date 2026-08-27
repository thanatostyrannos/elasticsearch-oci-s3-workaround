data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

# Two buckets, both tiny.
#
# `probe` answers the questions that need a real Oracle endpoint and cannot be
# answered on MinIO: which checksum algorithms DeleteObjects accepts, and
# whether ListObjectsV2 pages. A handful of objects is enough for both.
#
# `repo` is where an Elasticsearch snapshot repository points if you want to
# reproduce the fault end to end. Kept separate so the probe experiments cannot
# disturb a repository mid-test.
#
# Versioning is left Disabled deliberately. A versioned bucket turns a delete
# into a delete marker, which changes the behaviour under test into something
# that is not what a real snapshot repository sees.
resource "oci_objectstorage_bucket" "probe" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.this.namespace
  name           = "${var.prefix}-probe"
  storage_tier   = "Standard"
  versioning     = "Disabled"
  access_type    = "NoPublicAccess"

  freeform_tags = {
    purpose = "elasticsearch-oci-deleteobjects-probe"
  }
}

resource "oci_objectstorage_bucket" "repo" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.this.namespace
  name           = "${var.prefix}-repo"
  storage_tier   = "Standard"
  versioning     = "Disabled"
  access_type    = "NoPublicAccess"

  freeform_tags = {
    purpose = "elasticsearch-snapshot-repository-under-test"
  }
}

# A dedicated user, so the keys below can be revoked without touching the
# console login you are reading this with.
# Empty each bucket before Terraform destroys it.
#
# Oracle refuses to delete a bucket that still holds objects, and unlike the
# AWS provider, `oci_objectstorage_bucket` has no `force_destroy`. So a plain
# `terraform destroy` fails with BucketNotEmpty on any bucket a test actually
# used, and whoever hits that reaches for a per-object delete loop. Against a
# repository holding tens of thousands of leaked blobs that runs for hours,
# which is how a teardown ends up being something you wait for at the START of
# the next test rather than the END of the last one.
#
# `terraform_data` is built in from Terraform 1.4, so this costs no extra
# provider. Each one depends on its bucket, and destroy runs in reverse
# dependency order, so the emptying happens first and the bucket delete then
# succeeds.
#
# Requires the OCI CLI on PATH, configured for the same tenancy.

# A destroy provisioner may only read `self`, so the finished command goes into
# `input` at plan time rather than being assembled at destroy time.
locals {
  _ns          = data.oci_objectstorage_namespace.this.namespace
  _s3_endpoint = "https://${local._ns}.compat.objectstorage.${var.region}.oraclecloud.com"

  # Loops because DeleteObjects accepts at most a thousand keys per call while
  # the CLI auto-paginates the listing. Without the loop this succeeds on an
  # empty bucket and fails on a full one.
  _empty_with_aws = <<-EOT
    set -eu
    while :; do
      objects=$(aws s3api list-objects-v2 --bucket "$BUCKET" --max-items 1000 \
        --query 'Contents[].{Key: Key}' --output json \
        --endpoint-url "$ENDPOINT")
      case "$objects" in ""|null|"[]") break ;; esac
      aws s3api delete-objects --bucket "$BUCKET" \
        --sdk-checksum-algorithm CRC32C \
        --delete "{\"Objects\": $objects}" \
        --endpoint-url "$ENDPOINT" >/dev/null
    done
  EOT

  _empty_with_oci = "oci os object bulk-delete -ns \"$NAMESPACE\" -bn \"$BUCKET\" --force"

  _empty_command = var.empty_buckets_with == "aws" ? local._empty_with_aws : local._empty_with_oci
}

resource "terraform_data" "empty_probe_bucket" {
  input = {
    namespace = local._ns
    bucket    = oci_objectstorage_bucket.probe.name
    endpoint  = local._s3_endpoint
    command   = local._empty_command
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/sh", "-c"]
    command     = self.input.command
    environment = {
      NAMESPACE = self.input.namespace
      BUCKET    = self.input.bucket
      ENDPOINT  = self.input.endpoint
    }
  }
}

resource "terraform_data" "empty_repo_bucket" {
  input = {
    namespace = local._ns
    bucket    = oci_objectstorage_bucket.repo.name
    endpoint  = local._s3_endpoint
    command   = local._empty_command
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/sh", "-c"]
    command     = self.input.command
    environment = {
      NAMESPACE = self.input.namespace
      BUCKET    = self.input.bucket
      ENDPOINT  = self.input.endpoint
    }
  }
}

resource "oci_identity_user" "probe" {
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-user"
  description    = "Object Storage access for the Elasticsearch DeleteObjects probe"

  # Required on an Identity Domains tenancy, which refuses CreateUser without a
  # primary email and returns error.identity.user.primaryEmailNotSpecified. The
  # classic IAM API did not ask for one, so a configuration written against the
  # older behaviour fails here after the buckets have already been created.
  email = var.user_email
}

resource "oci_identity_group" "probe" {
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-group"
  description    = "Object Storage access for the Elasticsearch DeleteObjects probe"
}

resource "oci_identity_user_group_membership" "probe" {
  user_id  = oci_identity_user.probe.id
  group_id = oci_identity_group.probe.id
}

# Scoped to the two buckets by name. `manage` is needed rather than `read`
# because the whole point is exercising deletes.
resource "oci_identity_policy" "probe" {
  compartment_id = var.compartment_ocid
  name           = "${var.prefix}-policy"
  description    = "Object Storage access for the Elasticsearch DeleteObjects probe"

  statements = [
    "Allow group ${oci_identity_group.probe.name} to manage objects in compartment id ${var.compartment_ocid} where any {target.bucket.name='${oci_objectstorage_bucket.probe.name}', target.bucket.name='${oci_objectstorage_bucket.repo.name}'}",
    "Allow group ${oci_identity_group.probe.name} to read buckets in compartment id ${var.compartment_ocid}",
  ]
}

# The S3 compatibility credential. This is what Oracle calls a Customer Secret
# Key, and it is what both this toolkit's `s3` transport and Elasticsearch's own
# s3 repository plugin need. It is NOT an API signing key and NOT a console
# password, which is the confusion this project's README warns about.
resource "oci_identity_customer_secret_key" "probe" {
  display_name = "${var.prefix}-s3-compat"
  user_id      = oci_identity_user.probe.id
}

# An API signing key for the native `oci` transport, generated here so there is
# nothing to paste. Only needed if you exercise `--transport oci`.
resource "tls_private_key" "probe" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "oci_identity_api_key" "probe" {
  user_id   = oci_identity_user.probe.id
  key_value = tls_private_key.probe.public_key_pem
}

resource "local_sensitive_file" "api_key" {
  content         = tls_private_key.probe.private_key_pem
  filename        = "${path.module}/oci_api_key.pem"
  file_permission = "0600"
}

# The credentials file the audit tool reads. It refuses a file other users can
# read, so the mode matters.
resource "local_sensitive_file" "creds" {
  count = var.write_credentials_file ? 1 : 0

  filename        = "${path.module}/creds.json"
  file_permission = "0600"
  # The elasticsearch section is included only when a password is supplied. The
  # audit REFUSES to run with --elasticsearch against a file that has no such
  # section, treating a missing section as a refusal rather than falling back to
  # an unauthenticated request. A file written without it cannot drive the
  # invocation this repository documents, and the failure arrives a hundred
  # cycles into a run rather than at the first one.
  content = jsonencode(merge({
    s3 = {
      access_key_id     = oci_identity_customer_secret_key.probe.id
      secret_access_key = oci_identity_customer_secret_key.probe.key
    }
    oci = {
      tenancy     = var.tenancy_ocid
      user        = oci_identity_user.probe.id
      fingerprint = oci_identity_api_key.probe.fingerprint
      key_file    = abspath(local_sensitive_file.api_key.filename)
      pass_phrase = null
    }
    }, var.elasticsearch_password == "" ? {} : {
    elasticsearch = {
      username = var.elasticsearch_username
      password = var.elasticsearch_password
    }
  }))
}
