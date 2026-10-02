# etcd hosts, and optionally a dedicated data volume for each.
#
# WHY A VOLUME: the root disks here freeze for minutes at a time. On 2026-10-01 a single fsync on
# k8s-west-etcd-1's root disk took 121s and etcd's own WAL fdatasync 2m2.8s, while an XFS volume on
# the same host never went over 6.9s in 47 hours (infra-objectives/state/evidence/
# etcd-volume-fsync-probe-2026-10-02). The prep playbook mounts the volume at /var/lib/etcd, so
# kubespray's etcd_data_dir does not change.
#
# EVERYTHING HERE IS OPT-IN. This project root is shared by west's, east's and north's stacks; with
# the defaults in variables.tf, a stack that sets none of the ETCD_* variables plans no change.

resource "openstack_compute_servergroup_v2" "etcd" {
  count    = var.ETCD_ANTI_AFFINITY ? 1 : 0
  name     = "k8s-${var.INTERNAL_AZ}-etcd"
  policies = ["soft-anti-affinity"]
}

resource "openstack_blockstorage_volume_v3" "etcd" {
  count       = var.ETCD_VOLUME_SIZE > 0 ? var.ETCD : 0
  name        = "k8s-${var.INTERNAL_AZ}-etcd-${count.index+1}-data"
  size        = var.ETCD_VOLUME_SIZE
  description = "etcd data for k8s-${var.INTERNAL_AZ}-etcd-${count.index+1}, mounted at /var/lib/etcd"
}

resource "openstack_compute_instance_v2" "etcd" {
  count = var.ETCD
  name = "k8s-${var.INTERNAL_AZ}-etcd-${count.index+1}"
  image_id = var.IMAGE_UUID
  flavor_name = coalesce(var.ETCD_FLAVOR, var.master_flavor_name)
  key_pair = var.key_pair
  security_groups = [
    "default",
    openstack_networking_secgroup_v2.etcd_sec_group.name
  ]

  network {
    name = var.network
  }

  dynamic "scheduler_hints" {
    for_each = openstack_compute_servergroup_v2.etcd
    content {
      group = scheduler_hints.value.id
    }
  }

  # etcd_volume: the prep playbook finds the disk by this id (/dev/disk/by-id/virtio-<id[:20]>)
  # rather than by device name, which attach order does not guarantee.
  metadata = merge(
    { groups = "etcd" },
    var.ETCD_VOLUME_SIZE > 0 ? { etcd_volume = openstack_blockstorage_volume_v3.etcd[count.index].id } : {}
  )
}

resource "openstack_compute_volume_attach_v2" "etcd" {
  count       = var.ETCD_VOLUME_SIZE > 0 ? var.ETCD : 0
  instance_id = openstack_compute_instance_v2.etcd[count.index].id
  volume_id   = openstack_blockstorage_volume_v3.etcd[count.index].id
}
