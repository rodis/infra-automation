# playbooks/openstack

Verbs that act on the **cloud's model of a machine**, not on the machine.

Different from `playbooks/machines/` in three ways that matter: they use an OpenStack credential
rather than SSH, they work on instances nobody can log into, and their failure modes are Neutron's
and Nova's rather than the guest's.

| playbook | what it does | mutates |
| :-- | :-- | :-- |
| `dead_ports.yml` | finds Neutron ports stuck DOWN, and removes them when authorised | only with `dry_run: false` |

## Declared actions and authorisation

A playbook here that mutates declares what it needs and refuses to run without a caller that
grants it:

```yaml
required_actions: [openstack.port.delete]
```

The caller supplies `authorizing_objective` and `granted_actions`, taken from that objective's
`may_destroy` list. If the grant does not cover the declared actions, the playbook stops before
doing anything.

**Read modes need no grant.** `dry_run: true` is the default, is a pure read, and runs standalone.
Only the mutation demands an authorising objective — which is the same line rule 1 draws between
reads and writes.

**This is an interlock against accident, not against intent.** Anyone who can launch the template
can also type the variables. What it buys is that a hand-launch without thinking fails loudly, and
that the authorising objective is recorded in AWX's job history next to the deletion.

## Why `dead_ports` exists

Nova sometimes attaches a port that never comes up, then attaches a second that works. The
instance keeps both, the dead address sorts first, and every consumer of "the first IPv4" — the
AWX OpenStack inventory included — talks to nothing. The guest is healthy the whole time: one
address, one interface, correctly configured.

Rebuilding the instance fixes it and destroys a working machine to do so. Removing one port fixes
it in seconds. On 2026-08-22 this happened to 2 of 4 instances in a single build.
