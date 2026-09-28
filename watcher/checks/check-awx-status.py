#!/usr/bin/env python3
"""Evaluate objectives/awx-healthy.yml. One invocation, and the only thing it changes is that it
launches the read-only canary job [68].

  ./agent/run.sh python3 agent/check-awx-status.py

Exit codes, which is what Dagu grades on:
  0  every success: predicate holds                       -> SATISFIED
  1  at least one predicate is false                      -> NOT SATISFIED (a finding)
  2  a predicate could not be evaluated at all            -> FACTS NOT ESTABLISHED (rule 8)

2 outranks 0: a run that could not check something never reports satisfied (rule 6 — no green on
a partial match). 1 outranks 2: a predicate that is plainly false is a finding even if another
could not be read.

The `report_without_gating` block is printed on every run and never changes the exit code. That is
the objective's design, not leniency: those are "AWX could not be recovered", and a missing backup
blocking every objective that requires AWX would be true and useless.

It never remediates. AWX lives in `south`, and rule 7 leaves one response to a failed predicate:
read further, stop, report.
"""
import datetime, json, os, re, socket, ssl, sys, time
import urllib.error, urllib.parse, urllib.request

GRAFANA = (os.environ.get("GRAFANA_URL") or "").rstrip("/")
GRAFANA = GRAFANA if GRAFANA.startswith("http") or not GRAFANA else "https://" + GRAFANA
GTOKEN  = os.environ.get("GRAFANA_TOKEN") or ""
AWX     = (os.environ.get("CONTROLLER_HOST") or "").rstrip("/")
AWXTOK  = os.environ.get("CONTROLLER_OAUTH_TOKEN") or ""

PROM = "/api/datasources/proxy/uid/grafanacloud-prom/api/v1"
LOKI = "/api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1"

PG_NODE  = "k8s-south-postgresql"
# Every PostgreSQL series and log stream is scoped to AWX's database VM, so another cluster's series in
# the same Grafana stack can never answer for it.
PGSEL    = f'instance="{PG_NODE}"'
TLS_HOST = "infra.rods.me"
CANARY_TEMPLATE = 68                  # General: AWX: Canary
CANARY_INVENTORY = 29                 # General: AWX: Canary — localhost, ansible_connection=local
CANARY_TASKS = 3                      # assert, ping, debug in playbooks/awx/canary.yml
PROJECT = 11                          # Infra Automation
EXPECTED_EE = 1                       # AWX EE (24.6.1)

# Read at check time, never typed here: it was 6 until 2cb6911 bounded it to 4, and a hardcoded
# copy is what made estate-status flag a healthy AWX.
HOF = os.environ.get("HELM_OVERRIDE_FILES", os.path.expanduser("~/Development/personal/helm-override-files"))
AWX_VALUES = os.path.join(HOF, "awx", "awx.yml")   # on the VPS, a read-only clone refreshed before each run

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
results = []      # (verdict, name, detail)
reported = []     # (flag, name, detail) — never gates


def verdict(name, ok, detail=""):
    """ok: True / False / None (None = could not evaluate)."""
    v = PASS if ok is True else FAIL if ok is False else UNKNOWN
    results.append((v, name, detail))
    print(f"  {v:8} {name:46} {detail}")


def report(name, flag, detail=""):
    reported.append((flag, name, detail))
    print(f"  {flag:8} {name:46} {detail}")


def http_json(url, headers, body=None, method=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers, "Content-Type": "application/json"},
                                 method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def awx(path, body=None):
    return http_json(AWX + path, {"Authorization": "Bearer " + AWXTOK}, body)


def prom(expr):
    """Scalar from an instant query, or None when the query returns no series. None is 'no data',
    which is never the same fact as zero."""
    d = http_json(f"{GRAFANA}{PROM}/query?" + urllib.parse.urlencode({"query": expr}),
                  {"Authorization": "Bearer " + GTOKEN})
    res = d["data"]["result"]
    return float(res[0]["value"][1]) if res else None


def loki_count(expr):
    now = int(time.time())
    d = http_json(f"{GRAFANA}{LOKI}/query?" + urllib.parse.urlencode({"query": expr, "time": now * 10**9}),
                  {"Authorization": "Bearer " + GTOKEN})
    res = d["data"]["result"]
    return sum(float(r["value"][1]) for r in res)   # no series over the window = no matching lines


def guarded(name, fn):
    """Run one predicate; any exception means it could not be evaluated, never that it passed."""
    try:
        fn()
    except Exception as e:
        verdict(name, None, f"could not evaluate: {type(e).__name__}: {str(e)[:120]}")


def section(title):
    print(f"\n== {title}")


# ------------------------------------------------------------------------------------------------
print(f"awx-healthy — {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M UTC}")
if not (GRAFANA and GTOKEN and AWX and AWXTOK):
    print("missing GRAFANA_* or CONTROLLER_* — run through ./agent/run.sh")
    sys.exit(2)

section("1. freshness — nothing below is trusted on stale data")
def _fresh():
    ages = {j: prom(f'time() - timestamp({q})') for j, q in (("awx", 'up{job="awx"}'), ("postgres", f"pg_up{{{PGSEL}}}"))}
    missing = [j for j, a in ages.items() if a is None]
    if missing:
        verdict("collectors scraped within 5m", None, f"no series for {missing}")
    else:
        verdict("collectors scraped within 5m", all(a < 300 for a in ages.values()),
                ", ".join(f"{j} {a:.0f}s ago" for j, a in ages.items()))
guarded("collectors scraped within 5m", _fresh)
if results[-1][0] != PASS:
    print("\nFACTS NOT ESTABLISHED — the collectors are not current, so every reading below would be stale.")
    sys.exit(2)

section("2. AWX control plane")
def _ping():
    p = awx("/api/v2/ping/")
    verdict("GET /api/v2/ping/ answers", True, f"version {p.get('version')}")
guarded("GET /api/v2/ping/ answers", _ping)

def _instances():
    want = int(re.search(r"^\s*task_replicas:\s*(\d+)", open(AWX_VALUES).read(), re.M).group(1))
    inst = awx("/api/v2/instances/?page_size=100")["results"]
    ready = [i for i in inst if i["node_type"] == "control" and i["enabled"]
             and i.get("node_state") == "ready" and (i.get("capacity") or 0) > 0]
    verdict("ready control instances == task_replicas", len(ready) == want,
            f"{len(ready)} ready, task_replicas {want} (from awx.yml)")
    now = datetime.datetime.now(datetime.timezone.utc)
    ages = [(now - datetime.datetime.fromisoformat(i["last_seen"].replace("Z", "+00:00"))).total_seconds()
            for i in inst if i["node_type"] == "control" and i.get("last_seen")]
    verdict("every instance heartbeat < 120s old", bool(ages) and max(ages) < 120,
            f"oldest {max(ages):.0f}s" if ages else "no heartbeats reported")
guarded("control instances / heartbeats", _instances)

def _ig():
    igs = {g["name"]: g for g in awx("/api/v2/instance_groups/")["results"]}
    d = igs.get("default")
    verdict("`default` instance group is a container group", bool(d) and d.get("is_container_group") is True,
            "capacity is always 0 for a container group — deliberately not graded")
guarded("`default` instance group is a container group", _ig)

def _ee():
    ee = awx("/api/v2/settings/system/").get("DEFAULT_EXECUTION_ENVIRONMENT")
    verdict("DEFAULT_EXECUTION_ENVIRONMENT == 1", ee == EXPECTED_EE, f"is {ee} (settings/system)")
guarded("DEFAULT_EXECUTION_ENVIRONMENT == 1", _ee)

def _tls():
    ctx = ssl.create_default_context()
    with socket.create_connection((TLS_HOST, 443), timeout=10) as s, ctx.wrap_socket(s, server_hostname=TLS_HOST) as t:
        exp = datetime.datetime.fromtimestamp(ssl.cert_time_to_seconds(t.getpeercert()["notAfter"]), datetime.timezone.utc)
    days = (exp - datetime.datetime.now(datetime.timezone.utc)).days
    verdict(f"TLS for {TLS_HOST} valid > 14 days", days > 14, f"{days} days, until {exp:%Y-%m-%d}")
guarded(f"TLS for {TLS_HOST} valid > 14 days", _tls)

section("3. AWX's database — measured from outside AWX")
def _pgup():
    v = prom(f"pg_up{{{PGSEL}}}")
    verdict("pg_up == 1", None if v is None else v == 1, "no series" if v is None else f"{v:.0f}")
guarded("pg_up == 1", _pgup)

def _conns():
    v = prom(f"100 * sum(pg_stat_database_numbackends{{{PGSEL}}}) / (sum(pg_settings_max_connections{{{PGSEL}}})"
             f" - sum(pg_settings_superuser_reserved_connections{{{PGSEL}}}))")
    verdict("connection use < 80%", None if v is None else v < 80, "no series" if v is None else f"{v:.0f}%")
guarded("connection use < 80%", _conns)

def _disk():
    v = prom(f'100 * node_filesystem_avail_bytes{{instance="{PG_NODE}",mountpoint="/"}}'
             f' / node_filesystem_size_bytes{{instance="{PG_NODE}",mountpoint="/"}}')
    verdict("root filesystem > 20% free", None if v is None else v > 20,
            "no series" if v is None else f"{v:.0f}% free (data is on the root disk)")
guarded("root filesystem > 20% free", _disk)

def _logs():
    slots = loki_count(f'sum(count_over_time({{job="postgresql",{PGSEL}}} |~ "remaining connection slots|too many clients" [15m]))')
    panic = loki_count(f'sum(count_over_time({{job="postgresql",{PGSEL},level="PANIC"}} [15m]))')
    lines = loki_count(f'sum(count_over_time({{job="postgresql",{PGSEL}}} [15m]))')
    if lines == 0:
        # Zero lines of any kind is a blind collector, not a quiet database (CLAUDE.md, PostgreSQL logs).
        verdict("no slot exhaustion / PANIC in 15m", None, "no postgresql log lines at all in 15m — collector blind?")
    else:
        verdict("no slot exhaustion / PANIC in 15m", slots == 0 and panic == 0,
                f"slot-exhaustion {slots:.0f}, PANIC {panic:.0f}, of {lines:.0f} lines")
guarded("no slot exhaustion / PANIC in 15m", _logs)

def _xid():
    # pg_database_wraparound_age_datfrozenxid_seconds is age(datfrozenxid), a transaction COUNT —
    # the `_seconds` suffix is an upstream misnomer.
    pct = prom(f"100 * max(pg_database_wraparound_age_datfrozenxid_seconds{{{PGSEL}}}) / 2147483647")
    verdict("transaction ID age < 50% of wraparound", None if pct is None else pct < 50,
            "no series" if pct is None else f"{pct:.2f}%")
guarded("transaction ID age < 50% of wraparound", _xid)

section("4. canary — the only proof AWX can execute a job")
def _canary():
    last_err = ""
    for attempt in range(1, 4):   # rule 8: a canary that could not run is not a verdict
        job_id = awx(f"/api/v2/job_templates/{CANARY_TEMPLATE}/launch/", {})["job"]
        t0 = time.time()
        while True:
            j = awx(f"/api/v2/jobs/{job_id}/")
            if j["status"] in ("successful", "failed", "error", "canceled") or time.time() - t0 > 480:
                break
            time.sleep(5)
        events, url = [], f"/api/v2/jobs/{job_id}/job_events/?page_size=200"
        while url:
            page = awx(url.replace(AWX, "")); events += page["results"]; url = page["next"]
        # Terminal events only, counted — never last-write-wins across all events (rule 8).
        term = [e for e in events if e["event"] in
                ("runner_on_ok", "runner_on_failed", "runner_on_unreachable", "runner_on_skipped")]
        if not term:
            last_err = (j.get("job_explanation") or j.get("result_traceback") or j["status"])[-160:]
            print(f"           canary job {job_id}: no host events ({j['status']}) — attempt {attempt}/3: {last_err}")
            time.sleep(20)
            continue
        ok = [e for e in term if e["event"] == "runner_on_ok" and e.get("host_name") == "localhost"]
        bad = [e for e in term if e not in ok]
        proj = awx(f"/api/v2/projects/{PROJECT}/").get("scm_revision") or ""
        rev = j.get("scm_revision") or ""
        verdict("canary: 3 x runner_on_ok on localhost", len(ok) == CANARY_TASKS and not bad,
                f"job {job_id}: {len(ok)} ok, {len(bad)} other, status {j['status']}, "
                f"EE {j.get('execution_environment')}, {j.get('elapsed')}s")
        verdict("canary: ran the project's current revision", bool(rev) and rev == proj,
                f"job {rev[:10]} vs project {proj[:10]}")
        return
    verdict("canary: 3 x runner_on_ok on localhost", None, f"did not run after 3 attempts: {last_err}")
guarded("canary", _canary)

section("reported — never gating")
def _backup():
    # Not observable without SSH until a backup system exists to report on; the facts below were
    # measured on 2026-09-26 (archive_mode off, no tool, no timer). This becomes a real check —
    # backup age from whatever the backup system publishes — with the awx-database-backup task.
    report("backup < 26h old", "FINDING", "none configured (measured 2026-09-26) — not re-checked; see task awx-database-backup")
_backup()

def _sessions():
    idle = prom(f'max(pg_stat_activity_max_tx_duration{{{PGSEL},state=~"idle in transaction.*"}})')
    oldest = prom(f"max(pg_stat_activity_max_tx_duration{{{PGSEL}}})")
    # max_tx_duration is measured from xact_start, so for idle-in-transaction it reads the whole
    # transaction's age, not just the idle part: slightly stricter than the SQL it replaced.
    report("no idle-in-transaction > 10 min", "UNKNOWN" if idle is None else ("ok" if idle <= 600 else "FINDING"),
           "no series" if idle is None else f"oldest {idle:.0f}s")
    report("no transaction open > 1 h", "UNKNOWN" if oldest is None else ("ok" if oldest <= 3600 else "FINDING"),
           "no series" if oldest is None else f"oldest {oldest:.0f}s")
try: _sessions()
except Exception as e: report("sessions", "UNKNOWN", str(e)[:120])

for name, expr, fmt in (
    ("deadlocks in the last hour", f"sum(increase(pg_stat_database_deadlocks{{{PGSEL}}}[1h]))", "{:.0f}"),
    ("disk write latency, 1h avg (never thresholded)",
     f'1000*rate(node_disk_write_time_seconds_total{{instance="{PG_NODE}",device="vda"}}[1h])'
     f'/rate(node_disk_writes_completed_total{{instance="{PG_NODE}",device="vda"}}[1h])', "{:.1f} ms"),
):
    try:
        v = prom(expr)
        flag = "info" if "latency" in name else ("ok" if v == 0 else "FINDING")
        report(name, "UNKNOWN" if v is None else flag, "no series" if v is None else fmt.format(v))
    except Exception as e:
        report(name, "UNKNOWN", str(e)[:120])

# ------------------------------------------------------------------------------------------------
fails = [r for r in results if r[0] == FAIL]
unknown = [r for r in results if r[0] == UNKNOWN]
findings = [r for r in reported if r[0] == "FINDING"]
print()
if fails:
    print(f"NOT SATISFIED — {len(fails)} predicate(s) false: " + "; ".join(r[1] for r in fails))
    code = 1
elif unknown:
    print(f"FACTS NOT ESTABLISHED — {len(unknown)} predicate(s) could not be evaluated: " + "; ".join(r[1] for r in unknown))
    code = 2
else:
    print(f"SATISFIED — all {len(results)} predicates hold.")
    code = 0
if findings:
    print(f"Reported findings (not gating): " + "; ".join(r[1] for r in findings))
print("Remediation: none. AWX is in south (rule 7) — read further, stop, report.")
sys.exit(code)
