# Nothing here carries a default that identifies a tenancy. Every identifier
# comes from terraform.tfvars, which is gitignored, so a clone of this
# repository holds the shape of the rig and none of the owner's account.

variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }
variable "private_key_path" { type = string }
variable "region" {
  type        = string
  description = "region the cluster runs in, e.g. us-ashburn-1"
}
variable "compartment_ocid" {
  type        = string
  description = "compartment the cluster and its network are created in"
}

variable "prefix" {
  type        = string
  default     = "esrig"
  description = "name prefix for everything created here, so a teardown can find exactly what it made"
}

variable "kubernetes_version" {
  type        = string
  default     = "v1.36.1"
  description = "must be a version OKE currently offers; `oci ce cluster-options get --cluster-option-id all` lists them"
}

variable "vcn_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

# The three subnets the OKE quick-create topology uses. Kept as variables
# rather than hardcoded so a second rig can sit beside the first.
variable "api_subnet_cidr" {
  type    = string
  default = "10.0.0.0/28"
}
variable "node_subnet_cidr" {
  type    = string
  default = "10.0.10.0/24"
}
variable "lb_subnet_cidr" {
  type    = string
  default = "10.0.20.0/24"
}

variable "virtual_node_pool_size" {
  type        = number
  default     = 1
  description = "how many virtual nodes. The rig is one Elasticsearch and a handful of short jobs, so one is usually enough."
}

variable "pod_shape" {
  type        = string
  default     = "Pod.Standard.A1.Flex"
  description = "A1 is Ampere, the cheapest pod shape OKE offers, and the rig has no x86 dependency"
}

variable "availability_domain" {
  type        = string
  description = "AD the virtual node pool places pods in; `oci iam availability-domain list` gives the names"
}
variable "fault_domain" {
  type    = string
  default = "FAULT-DOMAIN-3"
}

variable "fss_capacity_gb" {
  type        = number
  default     = 50
  description = "size hint for the Elasticsearch data volume. File Storage bills on what is written, not on this number."
}
