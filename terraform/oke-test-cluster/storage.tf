# Elasticsearch's data path, on File Storage.
#
# Block Volume is the obvious choice and it is not available here: OKE virtual
# nodes do not support Block Volume persistent volumes. File Storage on virtual
# nodes was added on 28 August 2026, which is what makes running Elasticsearch
# on virtual nodes possible at all.
#
# Read the caveat in README.md before using this for anything but a test rig.
# File Storage is NFS, and Elastic advise against NFS for a data path.

resource "oci_file_storage_file_system" "es_data" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.availability_domain
  display_name        = "${var.prefix}-es-data"
}

resource "oci_file_storage_mount_target" "es_data" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.availability_domain
  subnet_id           = oci_core_subnet.nodes.id
  display_name        = "${var.prefix}-es-mt"
}

resource "oci_file_storage_export_set" "es_data" {
  mount_target_id = oci_file_storage_mount_target.es_data.id
  display_name    = "${var.prefix}-es-exports"
}

resource "oci_file_storage_export" "es_data" {
  export_set_id  = oci_file_storage_export_set.es_data.id
  file_system_id = oci_file_storage_file_system.es_data.id
  path           = "/${var.prefix}-es-data"

  # Reachable from the node subnet and nowhere else. Elasticsearch runs as a
  # non-root uid, so squashing is off; an NFS export that maps every caller to
  # nobody gives the data directory to nobody.
  export_options {
    source                         = var.node_subnet_cidr
    access                         = "READ_WRITE"
    identity_squash                = "NONE"
    require_privileged_source_port = false
  }
}
