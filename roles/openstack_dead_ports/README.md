# openstack_dead_ports

Finds — and optionally removes — Neutron ports that never came up, on any OpenStack instance.

Nova sometimes attaches a port that stays `DOWN`, then attaches a second one that works. The
instance carries both, the dead address is listed **first**, and anything that takes "the first
IPv4" talks to nothing. The guest is perfectly healthy; it is just unreachable at the address
everyone is using.

## Using it from a playbook

```yaml
- name: Build some machines
  hosts: new_machines
  tasks:
    - name: ...whatever creates them...

    - name: Clear any dead ports before anyone resolves an address
      ansible.builtin.include_role:
        name: openstack_dead_ports
      vars:
        openstack_dead_ports_dry_run: false
        authorizing_objective: my-objective
        granted_actions: ['openstack.port.delete']
```

**Run it before anything resolves a hostname to an address.** That ordering is the whole point: if
an inventory syncs first it records the dead address, a reachability check then calls a healthy
machine dark, and the escalation ladder starts rebooting something with nothing wrong with it.

Every OpenStack call is delegated to `localhost`, so the role behaves identically whether the
calling play connects to the guests over SSH or runs local. It needs an OpenStack cloud credential
available to the controller, and it reads and writes Neutron rather than the guest — so it works on
an instance nobody can log into.

## Variables

| variable | default | |
| :-- | :-- | :-- |
| `openstack_dead_ports_dry_run` | `true` | report only; no authority needed |
| `openstack_dead_ports_reachability_timeout` | `240` | seconds to wait for SSH after a deletion |
| `openstack_dead_ports_wait_for_ssh` | `true` | set false if the caller does its own waiting |
| `authorizing_objective` | `""` | **interlock** — see below |
| `granted_actions` | `[]` | **interlock** — must contain `openstack.port.delete` |

The two interlock variables are deliberately **not** prefixed. They are a repo-wide convention
shared by every destructive verb, so a caller granting one action to several roles passes one set
of variables rather than one set per role.

## The interlock

With `openstack_dead_ports_dry_run: false` the role asserts that `authorizing_objective` is set and
that `granted_actions` contains `openstack.port.delete`, and refuses otherwise. This is a guard
against accident, not against intent — anyone who can run the playbook can also type the variables.
Its value is that an unthinking invocation fails loudly, and that the authorising objective is
recorded in the job history beside the deletion.

The required action is written **literally** in the assertion rather than read from a variable. It
used to live in `required_actions`, which `extra_vars` could override — so a caller could pass
`required_actions=[]`, satisfy the interlock, and delete ports having granted nothing. That is the
difference between a check and a formality.

## Safety

- it will not remove a host's **last** live port, which would strand the instance with no network
  at all — a worse failure than the one being fixed
- after deleting, it waits for the instance to answer SSH again, because the port disappears before
  Neutron has converged and returning early hands back a machine that looks fixed and is not

## Also available as a standalone verb

`playbooks/openstack/dead_ports.yml` wraps this role, and is registered in AWX as
`[65] General: OpenStack: Remove dead ports`. That template carries **no** default inventory and
**no** default credential — both must be supplied at launch, because it is a `General:` verb and a
bound tenant would make a cross-tenant run silently wrong: every host would resolve to "no such
instance" and the job would report success having looked at nothing.
