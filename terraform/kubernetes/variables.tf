variable "INTERNAL_AZ" {
  type = string
}

variable "master_flavor_name" {
  default = "gp1.warpspeed"
}

variable "node_flavor_name" {
  default = "gp1.warpspeed"
}

variable "key_pair" {
  default = "dh_machines"
}

variable "IMAGE_UUID" {
  type = string
}

variable "network" {
  default = "public"
}

variable "MASTERS" {
  description = "Number of kubernetes masters"
  type        = number
}
variable "NODES" {
  description = "Number of kubernetes nodes"
  type = number
}
variable "ETCD" {
  description = "Number of etcd nodes"
  type = number
}
# Opt-in etcd settings (2026-10-02). Unset, they leave a stack exactly as it was — east, north and
# west share this project root. See etcd.tf.
variable "ETCD_FLAVOR" {
  description = "Flavor for etcd hosts; empty means master_flavor_name"
  type        = string
  default     = ""
}
variable "ETCD_VOLUME_SIZE" {
  description = "GB of dedicated data volume per etcd host, mounted at /var/lib/etcd; 0 means none"
  type        = number
  default     = 0
}
variable "ETCD_ANTI_AFFINITY" {
  description = "Spread etcd hosts across hypervisors with a soft-anti-affinity server group"
  type        = bool
  default     = false
}
# Opt-in stacked etcd on the masters (2026-10-06). Unset, they leave a stack exactly as it was. See
# master.tf.
variable "MASTER_ETCD_VOLUME_SIZE" {
  description = "GB of etcd data volume per master; > 0 also puts every master in the etcd group (stacked etcd); 0 means neither"
  type        = number
  default     = 0
}
variable "MASTER_ANTI_AFFINITY" {
  description = "Spread masters across hypervisors with a soft-anti-affinity server group"
  type        = bool
  default     = false
}
