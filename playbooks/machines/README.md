# playbooks/machines

Machine-tier playbooks: everything that acts on an instance as a *machine*, before anything
cares that it is going to be a Kubernetes node.

| playbook | what it does | mutates |
| :-- | :-- | :-- |
| `disable_unattended_upgrades.yml` | masks the apt timers and unattended-upgrades, after waiting for any in-flight apt work | yes — masks units, writes apt config |
| `recover_unreachable.yml` | an instance that does not answer SSH: start it if stopped, then soft reboot, then hard reboot | yes — reboots, and starts stopped instances |
| `reboot_if_required.yml` | reboots machines that a package upgrade left needing one, one at a time unless told otherwise | yes — reboots |
| `prepare_for_kubespray.yml` | base packages, hostname consistency, swap off | yes — installs packages, edits fstab |

`recover_unreachable.yml` needs an **OpenStack cloud credential** on the job template in addition
to the machine credential, because it calls the Nova API. The cloud credential binds one tenant,
so one run covers one tenant — a cluster whose nodes span two tenants needs one run per tenant.

## `reboot_if_required` and `reboot_serial`

Defaults to `serial: 1`. Pass `reboot_serial: 100%` as an extra var to reboot everything at once —
appropriate while building, when nothing is running on the machines yet. The default protects the
caller who reaches for the verb without thinking about what is on the hosts.

## Order

Intended sequence for a freshly built cluster:

```
recover_unreachable  ->  disable_unattended_upgrades  ->  update_machines  ->  reboot_if_required
                     ->  prepare_for_kubespray  ->  set_hosts_file
```

Each step is a separate verb so it can be run on its own; the sequence belongs in an AWX workflow
rather than in a playbook that calls the others.

`disable_unattended_upgrades` comes second, and specifically **before** `update_machines`: quiesce
the machine's own package automation before doing your own package work. Debian fires
`apt-daily.timer` at `OnBootSec=15min`, which on a freshly built cluster lands on top of the first
`dist-upgrade` and makes it crawl while both contend for the dpkg lock. That is not hypothetical —
it is what happened on west on 2026-08-21.

## Still in playbooks/miscellaneous

`update_machines.yml`, `set_hosts_file_for_kubespray.yml` and `check_machines_are_reachable.yaml`
are machine-tier too and belong here. They have not been moved because AWX job templates 54, 45
and 44 reference their current paths, so moving them means editing those templates in the same
change. Worth doing, deliberately, as its own commit.
