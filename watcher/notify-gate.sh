#!/usr/bin/env bash
# Decide whether a check's result should reach Telegram. Called by every check DAG between the check
# and its verdict steps (dagu/check-dag.yaml.j2):
#
#   notify-gate.sh <dag-name> <exit-code> <check-output-file>  ->  prints "alert" or "quiet"
#
# Why (2026-10-09): Dagu notifies on EVERY failed run, so a known finding — the cert-manager restarts,
# Grafana's no-data rules after the metrics cutover — sent the same message every hour, 24 a day, and
# an alert channel that repeats itself teaches you to stop reading it. Dagu's incident routing does
# deduplicate, but only for PagerDuty and SolarWinds, not Telegram.
#
# The rule: alert when the result is NEW or CHANGED, and once a day while it persists (a reminder that
# it is still there). A passing run clears the state and records when the problem ended. The signature
# is the exit code plus the failing lines with every number removed, so "2.19/day" becoming "2.18/day"
# is not a change, but a different predicate failing is.
#
# Fails open: if the state cannot be read or written, it says "alert". A gate that breaks must cost a
# duplicate message, never a silent finding.
set -u
dag="$1"; code="$2"; out="$3"
state_dir="$HOME/watcher/state"; state="$state_dir/$dag.state"
remind_after=86400   # seconds: one reminder a day while a finding persists
now=$(date -u +%s)

mkdir -p "$state_dir" 2>/dev/null || { echo alert; exit 0; }

if [ "$code" = 0 ]; then
  if [ -f "$state" ]; then
    first=$(sed -n 's/^first=//p' "$state")
    echo "$(date -u +%FT%TZ) $dag resolved (failing since $(date -u -d "@${first:-$now}" +%FT%TZ))" >> "$state_dir/history.log"
    rm -f "$state"
  fi
  echo quiet; exit 0
fi

# The failing lines, as the checks print them: PASS/FAIL/UNKNOWN predicate lines, the estate report's
# "! …" findings, and the overall verdict line.
lines=$(grep -E '^\s+(FAIL|UNKNOWN)\s|^\s+! |^(NOT SATISFIED|FACTS NOT ESTABLISHED|CHECK BROKEN)' "$out" 2>/dev/null | sed -E 's/^\s+//' | sort -u)
sig=$(printf '%s\n%s\n' "$code" "$lines" | sed -E 's/[0-9]+([.,:][0-9]+)*//g; s/ +/ /g' | sha256sum | cut -c1-16)

prev_sig=""; last=0; first=$now
if [ -f "$state" ]; then
  prev_sig=$(sed -n 's/^sig=//p' "$state"); last=$(sed -n 's/^last=//p' "$state"); first=$(sed -n 's/^first=//p' "$state")
fi

if [ "$sig" != "$prev_sig" ] || [ $(( now - ${last:-0} )) -ge $remind_after ]; then
  decision=alert; last=$now
  [ "$sig" != "$prev_sig" ] && first=$now
else
  decision=quiet
fi
printf 'sig=%s\nfirst=%s\nlast=%s\ncode=%s\n' "$sig" "$first" "$last" "$code" > "$state.tmp" && mv "$state.tmp" "$state" || decision=alert
echo "$decision"
