# watcher/

The estate's scheduled checks, deployed to the DreamHost VPS by `playbooks/watcher/install.yml`
(AWX template `General: Watcher: Install`). Dagu runs them and alerts the Telegram group
*Pinky and the Brain*.

| path | what |
| :-- | :-- |
| `checks/` | the checkers — exit 0 satisfied, 1 a finding, 2 facts not established |
| `run.sh` | the only path to a credential: `op run` against `watcher.env`, cache on |
| `watcher.env` | `op://watcher/...` references only |
| `watchdog.sh` | cron's supervisor for Dagu (every 5 min and `@reboot`) |
| `dagu/` | templates: server config, workspace bases, one DAG template rendered per check |

**The checkers' source of truth moved here on 2026-09-27** from `infra-objectives/agent/`, so AWX can
deploy them. The objectives they evaluate, and the reasoning behind every rule they encode, stay in
`infra-objectives` (local-only, never an AWX project).

Rules that hold for everything in here:

- **Read-only.** Nothing here converges or remediates.
- **No SSH.** Checks read Grafana (south) and AWX; see `infra-objectives/CLAUDE.md`, "Unattended
  checks never use SSH".
- **Narrow credentials only.** Vault `watcher`: AWX user `watcher` (auditor + execute on the canary)
  and Grafana `sa-watcher` (Viewer). Never `agent-ops`: DreamHost administers this box as root.
- **The watcher must never need AWX to run.** AWX builds it; once built it runs on its own.
- **One `run.sh` per check, never a loop** — the 1Password budget is account-wide.
