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

## Logs, as well as metrics

Metrics say a host is unwell; logs say why. Every diagnosis on 2026-08-22 came from a log line and
not from a threshold — soft lockups in `dmesg`, Alloy's own `permission denied`, a Neutron port
stuck `DOWN`. In a workflow where a human periodically asks "how are things", the retrospective
matters more than the notification, because nothing is standing by to act on a notification anyway.

It also closes a blind spot this role's own verb has. `[67] Check kernel health` reads `dmesg`
live; the ring buffer wraps and a reboot erases it. Shipped to Loki, a soft lockup stays queryable
for 14 days and survives the reboot that hid it.

Two details that would otherwise fail silently:

- **The `alloy` user must be in the `systemd-journal` group.** Without it Alloy reads nothing and
  reports nothing — service active, metrics flowing, no logs. The role adds the membership and
  restarts, because group changes only apply to a new process.
- **`max_age` is set to 12h.** The PostgreSQL VM has 20 weeks of uptime; unbounded, the first run
  would ship its entire historical journal and spend a large part of the 50GB allowance on history
  nobody asked for.

Two rules exist purely to keep the log stream sane, and both were added after seeing real data:

- **Alloy's own info/debug lines are dropped.** It was the noisiest unit on every host by a wide
  margin — ~870 lines per 15 minutes — nearly all of it narrating its own scrape loop. Its
  warn/error/crit lines are kept, because a collector that stops reporting its own failures is a
  smoke alarm with the battery out.
- **Transient unit names are collapsed.** systemd names each SSH login `session-<N>.scope` with an
  ever-increasing N, so `unit` is an unbounded label and every login makes a new Loki stream. That
  is a slow-motion outage, not untidiness, and it had already started: `session-5246`,
  `session-5248`, climbing.

Measured after deploying both rules, over a window strictly after the Alloy restart:

| | before | after |
| :-- | :-- | :-- |
| `alloy.service` | 865 lines / 30m, the loudest unit on every host | absent — info dropped, warn/error still pass |
| `session-N.scope` | one new stream per SSH login, unbounded | none; all collapsed to `session.scope` |
| entries per host per run | 86–100 | 10–25 |

**How to verify this, and how not to.** Loki's `/loki/api/v1/label/{name}/values` **ignores the
start/end parameters** and returns every value in the 14-day index, so after a fix it still lists
the old stream names and looks like nothing changed. Ask `count_over_time` instead, which is time
accurate:

    sum by (unit) (count_over_time({job="journal"}[3m]))

The first measurement of these rules said they had failed. They had not; the query was answering a
different question from the one asked.

Label cardinality is kept deliberately small — `job`, `instance`, `unit`, `level`. Loki charges on
stream cardinality rather than volume, and the free tier rejects writes rather than degrading, so
anything derived from message text or a PID would be a slow-motion outage.

## Variables

| variable | default | |
| :-- | :-- | :-- |
| `alloy_remote_write_url` | from `GRAFANA_REMOTE_WRITE_URL` | endpoint, not secret |
| `alloy_remote_write_user` | from `GRAFANA_REMOTE_WRITE_USER` | numeric instance id, not secret |
| `alloy_remote_write_token` | from `GRAFANA_REMOTE_WRITE_TOKEN` | **secret** |
| `alloy_scrape_interval` | `60s` | |
| `alloy_ship_logs` | `true` | journald → Loki |
| `alloy_loki_url` | from `GRAFANA_LOKI_URL` | push endpoint, not secret |
| `alloy_loki_user` | from `GRAFANA_LOKI_USER` | Loki instance id — **not** the Prometheus one |
| `alloy_loki_token` | `GRAFANA_LOKI_TOKEN`, else the metrics token | works only if that access policy carries `logs:write` |
| `alloy_journal_max_age` | `12h` | how far back to read on first start |

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
