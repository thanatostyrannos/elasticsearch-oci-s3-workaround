output "cluster_id" {
  value = oci_containerengine_cluster.rig.id
}

output "cluster_name" {
  value = oci_containerengine_cluster.rig.name
}

output "kubeconfig_command" {
  description = "run this to point kubectl at the cluster"
  value = join(" ", [
    "oci ce cluster create-kubeconfig",
    "--cluster-id", oci_containerengine_cluster.rig.id,
    "--file $HOME/.kube/config",
    "--region", var.region,
    "--token-version 2.0.0",
    "--kube-endpoint PUBLIC_ENDPOINT",
  ])
}

output "node_subnet_id" {
  value = oci_core_subnet.nodes.id
}

output "fss_file_system_id" {
  value = oci_file_storage_file_system.es_data.id
}

output "fss_mount_target_ip" {
  description = "the address the persistent volume mounts from"
  value       = oci_file_storage_mount_target.es_data.private_ip_ids
}

output "fss_export_path" {
  value = oci_file_storage_export.es_data.path
}
