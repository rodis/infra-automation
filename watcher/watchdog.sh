#!/usr/bin/env bash
# Keep Dagu running. Called by cron every 5 minutes and @reboot: a Managed VPS gives no root and no
# system service, and user systemd units stop at logout (Linger=no), so cron is the supervisor.
# Starting only when not already running makes it safe to call as often as cron likes.
set -u
if ! pgrep -u "$(id -u)" -f "$HOME/bin/dagu start-all" >/dev/null; then
  mkdir -p "$HOME/watcher/logs"
  nohup "$HOME/bin/dagu" start-all >>"$HOME/watcher/logs/dagu.log" 2>&1 &
  echo "$(date -u +%FT%TZ) watchdog: started dagu (pid $!)" >>"$HOME/watcher/logs/watchdog.log"
fi
