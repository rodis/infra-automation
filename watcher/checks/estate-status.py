#!/usr/bin/env python3
"""Read-only status sweep of the estate. One invocation, no mutations.

  ./agent/run.sh python3 agent/estate-status.py

Written because "give the estate a look" was costing a dozen improvised queries and the answer
depended on which ones I happened to think of. This asks the same questions in the same order
every time, so a quiet day and a day I forgot to check something look different.

Three rules it tries hard to honour, all of them learned the hard way in this repo:

  * COLLECTOR LIVENESS COMES FIRST. If the agents are not reporting, every number below is stale
    and reassuring. That section is printed before anything else for that reason.
  * "no data" IS NOT "zero". A query returning nothing means the question could not be answered,
    which is a different fact from the answer being zero, and conflating them is how a healthy
    machine gets rebuilt (rule 8).
  * IT SAYS WHAT IT CANNOT SEE. A status report that only lists what it checked invites the reader
    to assume it checked everything.
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request

GRAFANA = (os.environ.get("GRAFANA_URL") or "").rstrip("/")
TOKEN   = os.environ.get("GRAFANA_TOKEN") or ""
AWX     = (os.environ.get("CONTROLLER_HOST") or "").rstrip("/")
AWXTOK  = os.environ.get("CONTROLLER_OAUTH_TOKEN") or ""

PROM = "/api/datasources/proxy/uid/grafanacloud-prom/api/v1"
LOKI = "/api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1"

# Hosts that must be reporting. Hardcoded on purpose: the whole point is to notice ABSENCE, and a
# list derived from what is currently present can never do that. Update it when the estate changes.
EXPECTED = ["k8s-south-master-1", "k8s-south-node-1", "k8s-south-node-2", "k8s-south-postgresql"]

# AWX task pods, one awx_instance_capacity series each. Mirrors `task_replicas` in
# helm-override-files/awx/awx.yml -- 6 until 2cb6911 (2026-09-02) bounded it to 4. Hardcoded for
# the same reason as EXPECTED; change both together or the sweep flags a healthy AWX.
AWX_TASK_REPLICAS = 4

findings = []          # (severity, text) -- severity in {"RED", "AMBER"}
NODATA = object()


def _get(path, params):
    url = f"{GRAFANA}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def wake_grafana(wait=180, every=10):
    """A free Grafana Cloud stack spins down when idle and answers {"code":"Loading"} while it wakes.

    Without this the first sweep after a quiet spell reports CANNOT REACH GRAFANA -- a broken
    monitoring path -- when the stack was merely asleep and our own request is what woke it.
    Returns True if the stack had to be woken. Anything other than "Loading" is left for the
    collectors check to report, so a genuinely broken path still fails loudly.
    """
    woke, deadline = False, time.monotonic() + wait
    while True:
        try:
            with urllib.request.urlopen(f"{GRAFANA}/api/health", timeout=15):
                return woke
        except urllib.error.HTTPError as e:
            try:
                loading = json.load(e).get("code") == "Loading"
            except Exception:
                loading = False
        except Exception:
            loading = False
        if not loading or time.monotonic() > deadline:
            return woke
        if not woke:
            print(f"   grafana stack is asleep; waiting up to {wait}s for it to wake ...")
        woke = True
        time.sleep(every)


def promq(expr):
    """Instant query. Returns list of (labels, float) or NODATA. Never returns 0 for 'unanswerable'."""
    try:
        d = _get(PROM + "/query", {"query": expr})
    except Exception as e:
        return ("ERROR", str(e)[:80])
    res = (d.get("data") or {}).get("result") or []
    if not res:
        return NODATA
    out = []
    for s in res:
        try:
            out.append((s.get("metric", {}), float(s["value"][1])))
        except (KeyError, ValueError):
            pass
    return out or NODATA


def scalar(expr):
    r = promq(expr)
    if r is NODATA or (isinstance(r, tuple) and r[0] == "ERROR"):
        return r
    return r[0][1]


def logq(expr):
    r = promq.__wrapped__(expr) if hasattr(promq, "__wrapped__") else None
    try:
        d = _get(LOKI + "/query", {"query": expr})
    except Exception as e:
        return ("ERROR", str(e)[:80])
    res = (d.get("data") or {}).get("result") or []
    if not res:
        return NODATA
    return sum(float(s["value"][1]) for s in res)


def fmt(v, unit="", nd=2):
    if v is NODATA:
        return "no data"
    if isinstance(v, tuple) and v[0] == "ERROR":
        return f"QUERY FAILED: {v[1]}"
    return f"{v:.{nd}f}{unit}".rstrip("0").rstrip(".") + ("" if unit else "") if nd else f"{v}{unit}"


def line(label, value, note=""):
    print(f"   {label:<34} {value:>14}   {note}")


def check(label, value, red=None, amber=None, unit="", nd=2, higher_is_worse=True, note=""):
    """Print a value and record a finding. 'no data' is itself a finding, never a pass."""
    if value is NODATA:
        line(label, "no data", "<- unanswerable, not zero")
        findings.append(("AMBER", f"{label}: no data"))
        return
    if isinstance(value, tuple) and value[0] == "ERROR":
        line(label, "QUERY FAILED", value[1])
        findings.append(("RED", f"{label}: query failed"))
        return
    v = f"{value:,.{nd}f}".rstrip("0").rstrip(".") if nd else f"{value:,.0f}"
    mark = ""
    # STRICT on the higher-is-better side. With <= a threshold of 1 fires on the value 1, so
    # `has leader = 1`, `pg_up = 1` and `up = 1` were all reported RED on a healthy estate the
    # first time this ran. A status tool that flags healthy readings is worse than no tool: it
    # trains you to skim the verdict, and the one time it is right you will skim that too.
    if red is not None and ((value >= red) if higher_is_worse else (value < red)):
        mark = "RED"; findings.append(("RED", f"{label} = {v}{unit}"))
    elif amber is not None and ((value >= amber) if higher_is_worse else (value < amber)):
        mark = "amber"; findings.append(("AMBER", f"{label} = {v}{unit}"))
    line(label, v + unit, (mark + "  " if mark else "") + note)


def check_counter(label, metric, red=None, amber=None, recent="15m", long="24h", note=""):
    """A COUNTER graded on the recent window, with the long window as context.

    Same reasoning as the log checks, and added for the same reason: `proposals failed = 55` and
    `container restarts = 10` were both reported RED when every one of those events happened
    around a restart we performed hours earlier. A counter summed over 24h cannot distinguish
    "happening" from "happened", so it makes every deliberate change look like an incident until
    the window rolls past it -- which is precisely how a status tool teaches you to ignore it.
    """
    now = scalar(f"increase({metric}[{recent}])")
    day = scalar(f"increase({metric}[{long}])")
    now = 0.0 if now is NODATA or isinstance(now, tuple) else now
    day = 0.0 if day is NODATA or isinstance(day, tuple) else day
    if now > 0:
        state, graded = f"{day:,.0f} in {long} - HAPPENING NOW", now
    elif day > 0:
        state, graded = f"{day:,.0f} in {long}, none in last {recent} - historical", 0.0
    else:
        state, graded = "quiet", 0.0
    check(label, graded, red=red, amber=amber, nd=0, note=(note + "  " if note else "") + state)


def section(title):
    print(f"\n\033[1m{title}\033[0m" if sys.stdout.isatty() else f"\n{title}")
    print("   " + "-" * 62)


# ---------------------------------------------------------------- preflight --
if not GRAFANA or not TOKEN:
    sys.exit("GRAFANA_URL / GRAFANA_TOKEN not resolved - run me through agent/run.sh")

print("=" * 70)
print("  ESTATE STATUS  (read-only; mutates nothing)")
print("=" * 70)

# ------------------------------------------------- 1. can we see at all? -----
section("1. COLLECTORS  -- read this first; everything below depends on it")
if wake_grafana():
    print("   grafana stack was asleep and has woken (free tier idles; not a fault)")
targets = promq("up")
if targets is NODATA or (isinstance(targets, tuple) and targets[0] == "ERROR"):
    print("   CANNOT REACH GRAFANA. Nothing below can be trusted.")
    print("   " + ("`up` returned no series" if targets is NODATA else f"query failed: {targets[1]}"))
    print("   That is itself the finding: the monitoring path is broken, or the")
    print("   credential is wrong. Check by hand before concluding the estate is fine.")
    sys.exit(2)

seen, down = {}, []
for m, v in targets:
    inst, job = m.get("instance", "?"), m.get("job", "?")
    seen.setdefault(inst, []).append((job, v))
    if v != 1:
        down.append(f"{job}/{inst}")
line("targets reporting", f"{sum(1 for _, v in targets if v == 1)}/{len(targets)}")
for host in EXPECTED:
    if host not in seen:
        line(f"  {host}", "MISSING", "<- expected host is not reporting at all")
        findings.append(("RED", f"{host} is not reporting"))
for d in down:
    line(f"  {d}", "DOWN")
    findings.append(("RED", f"target down: {d}"))
if not down and all(h in seen for h in EXPECTED):
    line("all expected hosts present", "ok")

# ------------------------------------------------------------ 2. alerts -----
section("2. ALERTS  -- evaluating, but NOT deliverable: no contact point exists")
try:
    d = _get("/api/prometheus/grafana/api/v1/rules", {})
    rules = [r for g in (d.get("data") or {}).get("groups", []) for r in g.get("rules", [])]
    firing = [r for r in rules if r.get("state") == "firing"]
    unhealthy = [r for r in rules if r.get("health") != "ok"]
    line("rules evaluating", f"{len(rules)}")
    if unhealthy:
        for r in unhealthy:
            line(f"  {r.get('name','?')[:30]}", f"health={r.get('health')}", "<- fires nothing")
            findings.append(("RED", f"alert rule unhealthy: {r.get('name')}"))
    if firing:
        for r in firing:
            line(f"  {r.get('name','?')[:30]}", "FIRING")
            findings.append(("RED", f"FIRING: {r.get('name')}"))
    else:
        line("firing", "none")
except Exception as e:
    line("alert state", "QUERY FAILED", str(e)[:40])
    findings.append(("AMBER", "could not read alert state"))

# ------------------------------------------------------------- 3. hosts -----
section("3. HOSTS")
for h in EXPECTED:
    mem  = scalar(f'100 * node_memory_MemAvailable_bytes{{instance="{h}"}} / node_memory_MemTotal_bytes{{instance="{h}"}}')
    disk = scalar(f'100 * node_filesystem_avail_bytes{{instance="{h}",mountpoint="/"}} / node_filesystem_size_bytes{{instance="{h}",mountpoint="/"}}')
    steal= scalar(f'100 * sum(rate(node_cpu_seconds_total{{instance="{h}",mode="steal"}}[15m]))')
    load = scalar(f'node_load1{{instance="{h}"}}')
    print(f"   {h:<24}", end="")
    for nm, v, red, hib in (("mem free%", mem, 10, False), ("disk free%", disk, 10, False),
                            ("steal%", steal, 5, True), ("load1", load, None, True)):
        if v is NODATA or isinstance(v, tuple):
            print(f" {nm}=?", end=""); findings.append(("AMBER", f"{h} {nm}: no data")); continue
        bad = red is not None and ((v >= red) if hib else (v <= red))
        if bad: findings.append(("RED", f"{h} {nm} = {v:.1f}"))
        print(f" {nm}={v:.1f}{'!' if bad else ''}", end="")
    print()

# -------------------------------------------------------- 4. kubernetes -----
section("4. KUBERNETES (south)")
check("nodes Ready", scalar('count(kube_node_status_condition{condition="Ready",status="true"} == 1)'),
      red=3, higher_is_worse=False, nd=0, note="expected 3; any node down is worth knowing")
check("pods not Running/Succeeded",
      scalar('count(kube_pod_status_phase{phase!="Running",phase!="Succeeded"} == 1) or vector(0)'),
      red=3, amber=1, nd=0)
check_counter("container restarts", "sum(kube_pod_container_status_restarts_total)",
              red=5, amber=1)
check("deployments below desired",
      scalar('count(kube_deployment_status_replicas_available < kube_deployment_spec_replicas) or vector(0)'),
      red=1, nd=0)

# -------------------------------------------------------------- 5. etcd -----
section("5. ETCD  -- slow fsync here is CHRONIC (network storage), not news")
check("has leader", scalar('etcd_server_has_leader'), red=1, higher_is_worse=False, nd=0)
check_counter("leader changes", "etcd_server_leader_changes_seen_total", red=2, amber=1)
check_counter("proposals failed", "etcd_server_proposals_failed_total", red=20, amber=1)
# Thresholds raised deliberately after the 2026-08-24 batching change. --backend-batch-interval
# went 100ms -> 500ms, so each commit now carries roughly 8x the work: the DURATION of a commit is
# expected to be high, and p99 of a per-operation metric is the wrong lens on a change that makes
# operations bigger and rarer on purpose. Judging it at the old threshold would flag RED forever.
# What actually improved is below it -- commits/sec collapsed, and apiserver timeouts went to zero.
check("backend commit p99", scalar('histogram_quantile(0.99, sum by (le) (rate(etcd_disk_backend_commit_duration_seconds_bucket[30m])))'),
      red=2.0, amber=1.0, unit="s", nd=3, note="high BY DESIGN since batching; watch the rate below")
check("backend commits/sec", scalar('rate(etcd_disk_backend_commit_duration_seconds_count[15m])'),
      red=8, amber=4, nd=2, note="was ~10 before batching, ~1.2 after")
check("commit load (sec/sec)", scalar('rate(etcd_disk_backend_commit_duration_seconds_sum[15m])'),
      red=0.5, amber=0.25, nd=3, note="fraction of wall time spent committing")
check("WAL fsync p99", scalar('histogram_quantile(0.99, sum by (le) (rate(etcd_disk_wal_fsync_duration_seconds_bucket[30m])))'),
      unit="s", nd=3, note="chronic ~0.4-0.6s; alert on apiserver timeouts instead")

# -------------------------------------------------------- 6. postgresql -----
section("6. POSTGRESQL")
check("pg_up", scalar('pg_up'), red=1, higher_is_worse=False, nd=0)
check("connection headroom",
      scalar('sum(pg_settings_max_connections) - sum(pg_settings_superuser_reserved_connections) - sum(pg_stat_database_numbackends)'),
      red=10, amber=30, higher_is_worse=False, nd=0)
check("utilisation",
      scalar('100 * sum(pg_stat_database_numbackends) / (sum(pg_settings_max_connections) - sum(pg_settings_superuser_reserved_connections))'),
      red=90, amber=80, unit="%", nd=1)
check("cache hit ratio",
      scalar('100 * sum(pg_stat_database_blks_hit) / clamp_min(sum(pg_stat_database_blks_hit) + sum(pg_stat_database_blks_read), 1)'),
      red=90, amber=98, higher_is_worse=False, unit="%", nd=2)

# --------------------------------------------------------------- 7. awx -----
section("7. AWX  -- the hands: if this is down, nothing can be repaired")
check("up", scalar('up{job="awx"}'), red=1, higher_is_worse=False, nd=0)
check("instances reporting", scalar('count(awx_instance_capacity)'), red=1, amber=AWX_TASK_REPLICAS,
      higher_is_worse=False, nd=0, note=f"expected {AWX_TASK_REPLICAS}")
check("capacity remaining", scalar('sum(awx_instance_remaining_capacity)'), red=1, amber=30, higher_is_worse=False, nd=0)
if AWX and AWXTOK:
    try:
        req = urllib.request.Request(f"{AWX}/api/v2/ping/", headers={"Authorization": f"Bearer {AWXTOK}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            line("API answering", f"HTTP {r.status}")
    except Exception as e:
        line("API answering", "FAILED", str(e)[:40]); findings.append(("RED", "AWX API not answering"))

# -------------------------------------------------------------- 8. logs -----
section("8. LOGS  -- graded on the last 5m; longer windows are context only")
# Each of these is asked over 6h AND over 30m. The long window says whether it happened; the
# short one says whether it is STILL happening, and only the second is actionable. Without the
# pair, every deliberate change -- a restart, a fix that stopped an error -- reads as a fresh
# problem for hours afterwards, which is exactly how a status report trains you to ignore it.
for label, sel, red, amber in [
    ("kernel lockups / OOM", '{job="journal"} |~ "(?i)watchdog|soft lockup|hard lockup|oom-kill|blocked for more than"', 1, None),
    ("postgres FATAL/PANIC", '{job="postgresql", level=~"FATAL|PANIC"}', 100, 1),
    ("postgres rejected logins", '{job="postgresql"} |~ "no pg_hba.conf entry"', None, 1),
    ("apiserver etcd timeouts", '{job="pods", container="kube-apiserver"} |~ "etcdserver: request timed out"', 50, 5),
    ("AWX db errors", '{job="pods", namespace="awx"} |~ "OperationalError|remaining connection"', 20, 1),
]:
    six  = logq(f"sum(count_over_time({sel} [6h]))")
    half = logq(f"sum(count_over_time({sel} [30m]))")
    now  = logq(f"sum(count_over_time({sel} [5m]))")
    six  = 0.0 if six  is NODATA or isinstance(six,  tuple) else six
    half = 0.0 if half is NODATA or isinstance(half, tuple) else half
    now  = 0.0 if now  is NODATA or isinstance(now,  tuple) else now

    # Three windows, because two were not enough. Grading on 30m called a 19-SECOND burst
    # "ONGOING" half an hour after it ended -- that was the PostgreSQL restart on 2026-08-24
    # dropping 62 connections, which is a thing that happened, not a thing that is happening.
    # A status report that cannot tell those apart makes every deliberate change look like an
    # incident for the rest of the day.
    if now > 0:
        state, graded = "HAPPENING NOW", now
    elif half > 0:
        state, graded = f"burst in last 30m, stopped ({half:,.0f})", 0.0
    elif six > 0:
        state, graded = f"{six:,.0f} in 6h, none since - historical", 0.0
    else:
        state, graded = "quiet", 0.0
    check(label, graded, red=red, amber=amber, nd=0,
          note=(f"{six:,.0f} in 6h - {state}" if now > 0 else state))

# ------------------------------------------------------------- verdict ------
section("VERDICT")
reds   = [f for s, f in findings if s == "RED"]
ambers = [f for s, f in findings if s == "AMBER"]
if not reds and not ambers:
    print("   Nothing needs attention.")
elif not reds:
    print(f"   Nothing urgent. {len(ambers)} thing(s) worth a look:")
    for f in ambers: print(f"     - {f}")
else:
    print(f"   {len(reds)} thing(s) need attention:")
    for f in reds: print(f"     ! {f}")
    for f in ambers: print(f"     - {f}")

print("""
   WHAT THIS CANNOT TELL YOU
     - Whether alerts would reach anyone. They would not: rules evaluate, but no
       contact point exists, so a firing rule notifies nobody.
     - Whether the monitoring itself died. Every number here comes from Grafana;
       if ingestion stopped, this reports the last known values as if current.
       That gap needs a dead-man's switch outside the estate.
     - Anything about west, east or north. This is south only.
     - Pod CPU/memory USED. kube-state-metrics reports what the API says is
       desired, not what is consumed; a pod at its limit looks healthy until it
       is OOMKilled.""")
sys.exit(1 if reds else 0)
