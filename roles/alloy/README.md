# alloy

Installs Grafana Alloy on a VM and ships host metrics to Grafana Cloud.

One definition for every host in the estate. The differences between hosts — OS, login user,
inventory — are **not** handled here; they live in the Spacelift space that runs the playbook, which
is where they are already modelled.

```
PostgreSQL space          Debian    ANSIBLE_REMOTE_USER=debian, --limit postgresql
Kubernetes: Infra space   Ubuntu    ANSIBLE_REMOTE_USER=ubuntu,  mounted kubernetes.yaml inventory
```

Two stacks, one in each space, both pointing at `playbooks/observability`. Neither knows the other
exists. An OS conditional inside the role would drag that distinction out of the place that models
it properly and into a `when:` clause.

## Variables

| variable | default | |
| :-- | :-- | :-- |
| `alloy_remote_write_url` | from `GRAFANA_REMOTE_WRITE_URL` | endpoint, not secret |
| `alloy_remote_write_user` | from `GRAFANA_REMOTE_WRITE_USER` | numeric instance id, not secret |
| `alloy_remote_write_token` | from `GRAFANA_REMOTE_WRITE_TOKEN` | **secret** |
| `alloy_scrape_interval` | `60s` | |

All three come from the Spacelift `Observability` context. The role asserts they are present before
touching the host, because an Alloy that starts and writes to an empty URL looks healthy from every
angle except the one that matters.

## What it verifies

Not "did systemd start it" — Alloy starts happily against an endpoint refusing every sample. It
reads Alloy's own counters, `prometheus_remote_storage_samples_total` and `..._failed_total`, and
requires samples sent above zero with rejections at zero.

An earlier version grepped the journal for `/error/` and matched
`level=info msg="node exited without error"` — a success message containing the word — failing a
run in which everything worked. Counters cannot do that.

## Things learned the hard way

- **`root:alloy 0640`, not `root:root`.** The unit is `User=alloy` and reads its config as that user
  immediately; it does not start as root and drop privileges. Getting this wrong crash-loops the
  service on `permission denied` until systemd gives up.
- **Reset the systemd rate limiter.** A unit that has already crash-looped refuses further starts
  with *"start request repeated too quickly"*, so a fix appears not to work.
- **Mimir rejects out-of-order samples** rather than dropping them quietly, so the role ensures time
  synchronisation before installing anything, and keeps the send queue small so a backlog after an
  outage cannot wedge writes.

## Series budget

Grafana Cloud free allows 10,000 active series. Measured: **229 per host** with the trimmed
collector set in `templates/config.alloy.j2` — so the whole ~15-VM estate lands near 3,400. The
default collector set would be 500–1000 each and would put the estate at or over the cap. Add
collectors deliberately; the cost is invisible until it is not.

PostgreSQL metrics are deliberately absent. They need a dedicated read-only `pg_monitor` role in the
database rather than the application's own credentials, which is a change to the database and
belongs in its own reviewed step.
