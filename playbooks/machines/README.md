# playbooks/machines

Machine-tier playbooks: everything that acts on an instance as a *machine*, before anything
cares that it is going to be a Kubernetes node.

| playbook | what it does | mutates |
| :-- | :-- | :-- |
| `recover_unreachable.yml` | an instance that does not answer SSH: start it if stopped, then soft reboot, then hard reboot | yes — reboots, and starts stopped instances |
| `reboot_if_required.yml` | reboots machines that a package upgrade left needing one | yes — reboots |
| `prepare_for_kubespray.yml` | base packages, hostname consistency, swap off | yes — installs packages, edits fstab |

`recover_unreachable.yml` needs an **OpenStack cloud credential** on the job template in addition
to the machine credential, because it calls the Nova API. The cloud credential binds one tenant,
so one run covers one tenant — a cluster whose nodes span two tenants needs one run per tenant.

## Order

Intended sequence for a freshly built cluster:

```
recover_unreachable  ->  update_machines  ->  reboot_if_required  ->  prepare_for_kubespray  ->  set_hosts_file
```

Each step is a separate verb so it can be run on its own; the sequence belongs in an AWX workflow
rather than in a playbook that calls the others.

## Still in playbooks/miscellaneous

`update_machines.yml`, `set_hosts_file_for_kubespray.yml` and `check_machines_are_reachable.yaml`
are machine-tier too and belong here. They have not been moved because AWX job templates 54, 45
and 44 reference their current paths, so moving them means editing those templates in the same
change. Worth doing, deliberately, as its own commit.
