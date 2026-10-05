# monitoring/

The self-hosted monitoring stack — VictoriaMetrics single-node behind `vmauth` — on the watcher VPS
(`vps71884`), as its own Unix user `rods_brain`. Installed by `playbooks/monitoring/install.yml`
(AWX template `General: Monitoring: Install`, inventory `Monitoring: VPS`). Policy `monitoring-estate`
in `infra-objectives/CLAUDE.md`; plan in the task vault, project `self-hosted-metrics`.

| path | what |
| :-- | :-- |
| `watchdog.sh.j2` | cron's supervisor (every 5 min and `@reboot`), holding every process's flags |
| `vmauth.yml.j2` | one bearer-token user per sender, each routed to `/api/v1/write` only |

Rules that hold for everything here:

- **One public listener: vmauth**, on 8427, TLS. VictoriaMetrics (8428) and vmauth's own
  `/health` and `/metrics` (8426) are loopback only. The box has no firewall; the install fails if
  anything else of ours binds publicly.
- **Writer tokens live in the AWX credential "VictoriaMetrics writers" and on this box only** — never
  in a vault the agent reads, so the agent cannot forge what it is graded on.
- **Never as `rods_pinky`.** That user holds the watcher's 1Password token; the play refuses to run as it.
- **Memory ceilings on every process.** The 4 GiB cgroup cap is shared with the watcher and kills
  rather than swaps.
- **No Grafana.** Readers are programs; `vmui` on loopback for an ad-hoc look.
- **Trial settings** (2026-10-05): a self-signed certificate senders pin, 30-day retention.
