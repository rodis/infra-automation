resource "openstack_compute_instance_v2" "node" {
  count = var.NODES
  name = "k8s-${var.INTERNAL_AZ}-node-${count.index+1}"
  image_id = var.IMAGE_UUID
  flavor_name = var.node_flavor_name
  key_pair = var.key_pair
  security_groups = concat([
    "default",
    openstack_networking_secgroup_v2.kubelet_sec_group.name,
  ], var.CALICO_SECURITY_GROUP ? [openstack_networking_secgroup_v2.calico_node_sec_group.name] : [])

  network {
    name = var.network
  }

  metadata = {
    groups = "kube_node"
  }
}