#!/usr/bin/env bash
# The watcher's only path to a credential — the VPS counterpart of infra-objectives/agent/run.sh.
#
#   ./run.sh python3 -u checks/check-awx-status.py
#
# Secrets are injected into the child's environment by `op run` from watcher.env, which holds only
# op:// references into the 1Password vault `watcher`. Nothing is printed, and op masks any value a
# child prints by accident.
#
# The bootstrap secret is the `watcher-vps` service-account token (read on vault `watcher` only),
# placed BY HAND by the owner — never by automation, so it is not in AWX's job history. The Mac keeps
# its copy in the Keychain; here it is a 0600 file.
#
# ONE INVOCATION PER CHECK, NEVER A LOOP. The 1Password budget (1,000 requests/day) is shared by every
# service account in the owner's account, and exhausting it locks out every credential for ~24h —
# which happened on 2026-08-21. Each check resolves 4 references; three checks a day is ~12 requests.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
token_file="${WATCHER_OP_TOKEN_FILE:-$HOME/.config/watcher/op-token}"
[ $# -gt 0 ] || { echo "usage: $(basename "$0") <command> [args...]" >&2; exit 64; }
[ -r "$token_file" ] || { echo "error: no 1Password token at $token_file (the owner places it by hand)" >&2; exit 78; }
[ "$(stat -c %a "$token_file")" = "600" ] || { echo "error: $token_file must be mode 600" >&2; exit 78; }
# The cache cuts 1Password requests about tenfold (measured on the Mac, 2026-08-22).
OP_SERVICE_ACCOUNT_TOKEN="$(cat "$token_file")" \
OP_CACHE="${WATCHER_OP_CACHE:-true}" \
  exec "$HOME/bin/op" run --env-file="$here/watcher.env" -- "$@"
