# Control-plane hosts, and optionally stacked etcd on a dedicated data volume for each.
#
# STACKED ETCD (2026-10-06): with MASTER_ETCD_VOLUME_SIZE > 0 every master also joins the etcd
# group — kubespray then runs etcd on it as a host systemd unit — and gets its own volume, which
# the prep playbook mounts at /var/lib/etcd. Same reasoning as etcd.tf: the root disks freeze for
# minutes and a dedicated volume escapes the long freezes. On a master the root disk also carries
# the API server, kubelet, containerd and logs, so the isolation matters more, not less.
#
# No security group is added for etcd: every instance is in `default`, which admits all traffic
# between its members, so master-to-master 2379/2380 already flows.
#
# EVERYTHING HERE IS OPT-IN. This project root is shared by west's, east's and north's stacks; with
# the defaults in variables.tf, a stack that sets none of the MASTER_* variables plans no change.

resource "openstack_compute_servergroup_v2" "master" {
  count    = var.MASTER_ANTI_AFFINITY ? 1 : 0
  name     = "k8s-${var.INTERNAL_AZ}-master"
  policies = ["soft-anti-affinity"]
}

resource "openstack_blockstorage_volume_v3" "master_etcd" {
  count       = var.MASTER_ETCD_VOLUME_SIZE > 0 ? var.MASTERS : 0
  name        = "k8s-${var.INTERNAL_AZ}-master-${count.index+1}-etcd"
  size        = var.MASTER_ETCD_VOLUME_SIZE
  description = "etcd data for k8s-${var.INTERNAL_AZ}-master-${count.index+1}, mounted at /var/lib/etcd"
}

resource "openstack_compute_instance_v2" "master" {
  count = var.MASTERS
  name = "k8s-${var.INTERNAL_AZ}-master-${count.index+1}"
  image_id = var.IMAGE_UUID
  flavor_name = var.master_flavor_name
  key_pair = var.key_pair
  security_groups = [
    "default",
    openstack_networking_secgroup_v2.kube_api_server_sec_group.name,
    openstack_networking_secgroup_v2.kubelet_sec_group.name,
    openstack_networking_secgroup_v2.nginx_controller_sec_group.name,
    openstack_networking_secgroup_v2.calico_node_sec_group.name,
  ]

  network {
    name = var.network
  }

  dynamic "scheduler_hints" {
    for_each = openstack_compute_servergroup_v2.master
    content {
      group = scheduler_hints.value.id
    }
  }

  # `groups` is how the constructed inventory places a host; appending etcd is what makes kubespray
  # stack etcd here. Unstacked, the string must stay byte-identical or every stack plans a change.
  # etcd_volume: the prep playbook finds the disk by this id, as for etcd.tf's hosts.
  metadata = merge(
    { groups = var.MASTER_ETCD_VOLUME_SIZE > 0 ? "kube_control_plane,nginx_ingress_controller,etcd" : "kube_control_plane,nginx_ingress_controller" },
    var.MASTER_ETCD_VOLUME_SIZE > 0 ? { etcd_volume = openstack_blockstorage_volume_v3.master_etcd[count.index].id } : {}
  )
}

resource "openstack_compute_volume_attach_v2" "master_etcd" {
  count       = var.MASTER_ETCD_VOLUME_SIZE > 0 ? var.MASTERS : 0
  instance_id = openstack_compute_instance_v2.master[count.index].id
  volume_id   = openstack_blockstorage_volume_v3.master_etcd[count.index].id
}
