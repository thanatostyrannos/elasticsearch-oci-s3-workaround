# An enhanced OKE cluster with one virtual node pool.
#
# Enhanced rather than basic because virtual nodes require it. That choice
# carries the cluster fee, $0.10 an hour capped at $74.40 a month, which is
# the largest line in this rig's bill: more than the Elasticsearch pod itself.
# A rig that runs intermittently should be destroyed between campaigns rather
# than left idle, and `terraform destroy` here is the whole teardown.

resource "oci_containerengine_cluster" "rig" {
  compartment_id     = var.compartment_ocid
  kubernetes_version = var.kubernetes_version
  name               = "${var.prefix}-cluster"
  vcn_id             = oci_core_vcn.rig.id
  type               = "ENHANCED_CLUSTER"

  endpoint_config {
    subnet_id            = oci_core_subnet.api.id
    is_public_ip_enabled = true
  }

  # Virtual nodes require the VCN-native pod networking CNI. The flannel
  # overlay does not support them. This is a top-level block, not part of
  # `options`, which the provider schema is the authority on.
  cluster_pod_network_options {
    cni_type = "OCI_VCN_IP_NATIVE"
  }

  options {
    service_lb_subnet_ids = [oci_core_subnet.lb.id]

    kubernetes_network_config {
      pods_cidr     = "10.244.0.0/16"
      services_cidr = "10.96.0.0/16"
    }
  }
}

# Pods, not machines. Nothing to patch, nothing to size, and the bill follows
# the pod's own requests rather than a node that is mostly idle. A1 is Ampere:
# the cheapest shape OKE offers, and the rig is pure Python and Elasticsearch,
# neither of which needs x86.
resource "oci_containerengine_virtual_node_pool" "rig" {
  compartment_id = var.compartment_ocid
  cluster_id     = oci_containerengine_cluster.rig.id
  display_name   = "${var.prefix}-vnp"
  size           = var.virtual_node_pool_size

  pod_configuration {
    subnet_id = oci_core_subnet.nodes.id
    shape     = var.pod_shape
  }

  placement_configurations {
    availability_domain = var.availability_domain
    fault_domain        = [var.fault_domain]
    subnet_id           = oci_core_subnet.nodes.id
  }
}
