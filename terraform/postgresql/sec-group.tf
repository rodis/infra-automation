resource "openstack_networking_secgroup_v2" "postgres_sec_group" {
  name = "Postgres"
  description = "Security group to allow outside connection to the server"
  delete_default_rules = false
}

# Allow tcp connection on port 5432 for IPv4, FROM THE CLUSTER NODES ONLY.
#
# This rule previously had no remote_ip_prefix, which Neutron reads as 0.0.0.0/0 -- so PostgreSQL
# was reachable from the entire internet at the hypervisor, and pg_hba was the only thing turning
# attempts away. On 2026-08-23 that meant 138 rejected logins in a day from five addresses,
# guessing postgres, wog and pgg_superadmins.
#
# A security group is enforced as host-level packet filtering, so a blocked source never reaches
# the VM. pg_hba only refuses AFTER the TCP handshake and a parsed startup packet. Both are worth
# having and they fail differently: this one is the wall, pg_hba is the lock on the door behind it.
#
# THESE ADDRESSES ARE DUPLICATED in playbooks/postgresql/vars/main.yml (postgresql_hba_entries),
# deliberately and unavoidably -- Terraform cannot read that file and Ansible cannot read this one.
# Change one and you MUST change the other. Getting it wrong in this file is the more expensive
# mistake: pg_hba can be fixed over SSH, but a security group that excludes the nodes locks AWX out
# of its database at a layer no amount of database configuration can rescue.
#
# Why hardcoded addresses are acceptable here specifically: the cluster is three nodes with public
# IPs assigned directly rather than floated, and it is not autoscaled. `openstack server rebuild`
# keeps the port and therefore the address; only a destroy-and-recreate would change it, and that
# is precisely what rule 7 forbids absolutely in south. See the storage substrate and rule 7
# sections in infra-objectives/CLAUDE.md.
variable "DB_CLIENT_CIDRS" {
  type        = list(string)
  description = "Hosts permitted to reach PostgreSQL. The three south cluster nodes; AWX runs only there."
  default = [
    "208.113.131.96/32", # k8s-south-master-1 -- control-plane taint, no AWX pods today, included
    "208.113.135.15/32", # k8s-south-node-1   -- AWX connects from here
    "208.113.130.17/32", # k8s-south-node-2   -- AWX connects from here
  ]
}

resource "openstack_networking_secgroup_rule_v2" "rule_postgres_tcp_5432_ipv4" {
  for_each = toset(var.DB_CLIENT_CIDRS)

  direction        = "ingress"
  ethertype        = "IPv4"
  protocol         = "tcp"
  port_range_min   = 5432
  port_range_max   = 5432
  remote_ip_prefix = each.value

  security_group_id = openstack_networking_secgroup_v2.postgres_sec_group.id
}