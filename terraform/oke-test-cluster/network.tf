# The network OKE's quick-create wizard builds, written down.
#
# Three subnets, because the control plane endpoint, the nodes and the load
# balancers each need different reachability. Nodes are private and reach the
# internet through a NAT gateway; they reach Oracle services, including Object
# Storage, through a service gateway so that traffic never leaves Oracle's
# network. The rig's whole point is talking to Object Storage, so that path
# is the one that matters.

resource "oci_core_vcn" "rig" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = [var.vcn_cidr]
  display_name   = "${var.prefix}-vcn"
  dns_label      = replace(var.prefix, "-", "")
}

resource "oci_core_internet_gateway" "rig" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-igw"
  enabled        = true
}

resource "oci_core_nat_gateway" "rig" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-ngw"
}

data "oci_core_services" "all" {
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}

resource "oci_core_service_gateway" "rig" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-sgw"
  services {
    service_id = data.oci_core_services.all.services[0]["id"]
  }
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-public-rt"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.rig.id
  }
}

resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-private-rt"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.rig.id
  }
  route_rules {
    destination       = data.oci_core_services.all.services[0]["cidr_block"]
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.rig.id
  }
}

# Node security list. These rules are the quick-create set, read back from a
# console-built cluster rather than guessed:
#   in  : everything from the node subnet itself
#   in  : ICMP and TCP from the API endpoint subnet
#   in  : TCP 30000-32767 and 10256 from the load balancer subnet
#   out : everything to the node subnet, 6443 and 12250 to the API endpoint,
#         443 to the Oracle services network, and general egress
#
# SSH from 0.0.0.0/0 is in the wizard's output and is NOT reproduced here.
# Virtual nodes have no host to ssh into, so the rule grants reachability to
# nothing and would only widen the surface a reviewer has to think about.
resource "oci_core_security_list" "nodes" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-nodes-sl"

  ingress_security_rules {
    protocol = "all"
    source   = var.node_subnet_cidr
  }
  ingress_security_rules {
    protocol = "1"
    source   = var.api_subnet_cidr
  }
  ingress_security_rules {
    protocol = "6"
    source   = var.api_subnet_cidr
  }
  ingress_security_rules {
    protocol = "6"
    source   = var.lb_subnet_cidr
    tcp_options {
      min = 30000
      max = 32767
    }
  }
  ingress_security_rules {
    protocol = "6"
    source   = var.lb_subnet_cidr
    tcp_options {
      min = 10256
      max = 10256
    }
  }

  egress_security_rules {
    protocol    = "all"
    destination = var.node_subnet_cidr
  }
  egress_security_rules {
    protocol    = "6"
    destination = var.api_subnet_cidr
    tcp_options {
      min = 6443
      max = 6443
    }
  }
  egress_security_rules {
    protocol    = "6"
    destination = var.api_subnet_cidr
    tcp_options {
      min = 12250
      max = 12250
    }
  }
  egress_security_rules {
    protocol    = "1"
    destination = var.api_subnet_cidr
  }
  egress_security_rules {
    protocol         = "6"
    destination      = data.oci_core_services.all.services[0]["cidr_block"]
    destination_type = "SERVICE_CIDR_BLOCK"
    tcp_options {
      min = 443
      max = 443
    }
  }
  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }
}

resource "oci_core_security_list" "api" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-api-sl"

  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
    tcp_options {
      min = 6443
      max = 6443
    }
  }
  ingress_security_rules {
    protocol = "6"
    source   = var.node_subnet_cidr
    tcp_options {
      min = 6443
      max = 6443
    }
  }
  ingress_security_rules {
    protocol = "6"
    source   = var.node_subnet_cidr
    tcp_options {
      min = 12250
      max = 12250
    }
  }
  ingress_security_rules {
    protocol = "1"
    source   = var.node_subnet_cidr
  }

  egress_security_rules {
    protocol         = "6"
    destination      = data.oci_core_services.all.services[0]["cidr_block"]
    destination_type = "SERVICE_CIDR_BLOCK"
    tcp_options {
      min = 443
      max = 443
    }
  }
  egress_security_rules {
    protocol    = "all"
    destination = var.node_subnet_cidr
  }
  egress_security_rules {
    protocol    = "1"
    destination = var.node_subnet_cidr
  }
}

resource "oci_core_security_list" "lb" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.rig.id
  display_name   = "${var.prefix}-lb-sl"

  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
  }
  egress_security_rules {
    protocol    = "all"
    destination = var.node_subnet_cidr
  }
}

resource "oci_core_subnet" "api" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.rig.id
  cidr_block                 = var.api_subnet_cidr
  display_name               = "${var.prefix}-api-subnet"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.api.id]
  prohibit_public_ip_on_vnic = false
}

resource "oci_core_subnet" "nodes" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.rig.id
  cidr_block                 = var.node_subnet_cidr
  display_name               = "${var.prefix}-node-subnet"
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.nodes.id]
  prohibit_public_ip_on_vnic = true
}

resource "oci_core_subnet" "lb" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.rig.id
  cidr_block                 = var.lb_subnet_cidr
  display_name               = "${var.prefix}-lb-subnet"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.lb.id]
  prohibit_public_ip_on_vnic = false
}
