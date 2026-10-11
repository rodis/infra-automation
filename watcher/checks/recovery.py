"""Recovery after a storage stall — the predicates shared by platform-healthy and awx-healthy
(owner, 2026-10-11; objectives/platform-healthy.yml and awx-healthy.yml, "recovery").

Why these exist: iad-2's storage backend stalls, across hosts, for up to an hour (2026-10-11
02:43–03:35 UTC: a 36.8 s etcd fsync, 60 s etcd requests). Every leader-elected controller loses its
lease and restarts. The owner has decided nothing on our side can prevent that; what is graded is
whether everything RECOVERED. So a predicate here never counts lifetime restarts — that only counts
stalls, and stays red for two weeks after each one — it asks: running, ready, not restarting any
more, holding its lease again, and the API answering.

Each function returns rows; the calling check prints and grades them with its own verdict()/report():
  predicates -> [(name, ok: True/False/None, detail)]
  reports    -> [(name, flag, detail)]
All reads are kube-state-metrics, node_exporter and etcd series in the metrics backend (metrics.py)
plus kube-apiserver's log in the logs backend (logs.py). A read that fails raises; the caller's
guarded() turns that into UNKNOWN, never into a pass.
"""
import datetime, time

import logs
import metrics

QUIET_FOR = "30m"         # no restart in this long = the burst is over
LEASE_FRESH_S = 120       # a lease renewed within this long has a live, working holder
APISERVER_QUIET = "15m"   # no "etcdserver: request timed out" for this long = etcd is serving again
STALL_PSI = 0.5           # io "full" pressure above this for a minute counts as a stalled minute
STALL_GAP_MIN = 15        # stalled minutes this close together are one episode (5 split 2026-10-11 02:43–03:20 into five)


def _q(expr):
    return metrics.query(expr)


def _range(expr, start, end, step):
    return metrics.get("/query_range", {"query": expr, "start": int(start), "end": int(end), "step": step}, timeout=60)["data"]["result"]


def containers(ksm, namespaces, container_re=".+", pod_re=".+"):
    """Every matching container: Ready, not in CrashLoopBackOff, no restart in the last QUIET_FOR."""
    sel = f'instance="{ksm}",namespace=~"{namespaces}",container=~"{container_re}",pod=~"{pod_re}"'
    ready = {(r["metric"]["pod"], r["metric"]["container"]): float(r["value"][1]) == 1
             for r in _q(f"kube_pod_container_status_ready{{{sel}}}")}
    if not ready:
        return [(f"containers in {namespaces} ({container_re})", None, "no kube_pod_container_status_ready series — nothing to grade")]
    crash = {(r["metric"]["pod"], r["metric"]["container"])
             for r in _q(f'kube_pod_container_status_waiting_reason{{{sel},reason="CrashLoopBackOff"}} == 1')}
    recent = {(r["metric"]["pod"], r["metric"]["container"]): round(float(r["value"][1]))
              for r in _q(f"increase(kube_pod_container_status_restarts_total{{{sel}}}[{QUIET_FOR}])")}
    rows = []
    for (pod, ctr), is_ready in sorted(ready.items()):
        n = recent.get((pod, ctr), 0)
        bad = [b for b, cond in (("not Ready", not is_ready), ("CrashLoopBackOff", (pod, ctr) in crash),
                                 (f"{n} restart(s) in {QUIET_FOR}", n > 0)) if cond]
        rows.append((f"{pod[:38]}/{ctr} recovered", not bad,
                     "ready, no restart in " + QUIET_FOR if not bad else ", ".join(bad)))
    return rows


def leases(ksm, wanted):
    """wanted: [(label, namespace, lease name or None = every lease in that namespace)].
    Each must have been renewed within LEASE_FRESH_S. A renewal means a live holder that is working:
    a controller that lost its lease and never re-acquired it is running and doing nothing."""
    renew = {(r["metric"]["namespace"], r["metric"]["lease"]): float(r["value"][1])
             for r in _q(f'kube_lease_renew_time{{instance="{ksm}"}}')}
    if not renew:
        return [("leader-election leases", None, "no kube_lease_renew_time series — kube-state-metrics is not exporting leases")]
    now, rows = time.time(), []
    for label, ns, name in wanted:
        found = {k: v for k, v in renew.items() if k[0] == ns and (name is None or k[1] == name)}
        if not found:
            rows.append((f"{label} lease renewed < {LEASE_FRESH_S}s", False, f"no lease {ns}/{name or '*'}"))
            continue
        ages = {f"{k[0]}/{k[1]}": now - v for k, v in found.items()}
        stale = {k: a for k, a in ages.items() if a > LEASE_FRESH_S}
        rows.append((f"{label} lease renewed < {LEASE_FRESH_S}s", not stale,
                     ", ".join(f"{k} {a:.0f}s ago" for k, a in ages.items())))
    return rows


def apiserver(ksm, hosts_re):
    """The API server is serving: /readyz probe succeeds, and kube-apiserver has logged no etcd
    timeout for APISERVER_QUIET. While a stall is still in progress this is the predicate that says so."""
    rows = []
    probe = _q('probe_success{job="integrations/blackbox/readyz"}')
    rows.append(("API server /readyz", bool(probe) and all(float(r["value"][1]) == 1 for r in probe),
                 "no probe series" if not probe else ", ".join(f'{r["metric"].get("instance")}={r["value"][1]}' for r in probe)))
    n = logs.count([("job", "=", "pods"), ("instance", "=~", hosts_re), ("container", "=", "kube-apiserver")],
                   "etcdserver: request timed out", APISERVER_QUIET)
    rows.append((f"no etcd timeouts in API server log, {APISERVER_QUIET}", n == 0, f"{n:.0f} in {APISERVER_QUIET}"))
    return rows


def restart_history(ksm, namespaces, days=14):
    """Restarts per container over `days`, grouped into bursts (report only — what the old restart-rate
    predicate measured, kept as information)."""
    end = time.time(); start = end - days * 86400
    sel = f'instance="{ksm}",namespace=~"{namespaces}"'
    series = _range(f"sum by (namespace, pod, container) (increase(kube_pod_container_status_restarts_total{{{sel}}}[10m]))",
                    start, end, "10m")
    rows = []
    for s in series:
        hits = [(float(t), float(v)) for t, v in s["values"] if float(v) >= 0.5]
        if not hits:
            continue
        bursts, cur = [], None
        for t, v in hits:
            if cur and t - cur[1] <= 1800:
                cur = (cur[0], t, cur[2] + v)
            else:
                if cur: bursts.append(cur)
                cur = (t, t, v)
        bursts.append(cur)
        m = s["metric"]
        rows.append((f'{m["pod"][:38]}/{m["container"]} restarts, {days}d', "info",
                     f"{sum(b[2] for b in bursts):.0f} in {len(bursts)} burst(s): " +
                     ", ".join(f"{_hm(b[0])}" for b in bursts[-6:])))
    return rows or [(f"restarts, {days}d", "info", "none")]


def storage_stalls(hosts_re, days=14):
    """Storage-stall episodes (report only, and kept on purpose: the numbers to look at should another
    idea come along — owner, 2026-10-11). Minutes where a host's io 'full' pressure exceeded STALL_PSI,
    merged into episodes, each with its length, the hosts it hit, and etcd's WAL fsyncs over 1 s and
    over 8 s. PSI is collected since 2026-10-10 ~16:24 UTC; nothing earlier can show here."""
    end = time.time(); start = end - days * 86400
    psi = _range(f'rate(node_pressure_io_stalled_seconds_total{{instance=~"{hosts_re}"}}[1m])', start, end, "60")
    minutes = {}
    for s in psi:
        for t, v in s["values"]:
            if float(v) > STALL_PSI:
                minutes.setdefault(int(float(t)), set()).add(s["metric"]["instance"])
    if not minutes:
        return [(f"storage stalls, {days}d", "info", f"none (io full pressure > {STALL_PSI:.0%} for a minute)")]
    episodes, cur = [], None
    for t in sorted(minutes):
        if cur and t - cur["end"] <= STALL_GAP_MIN * 60:
            cur["end"] = t; cur["hosts"] |= minutes[t]; cur["minutes"] += 1
        else:
            if cur: episodes.append(cur)
            cur = {"start": t, "end": t, "hosts": set(minutes[t]), "minutes": 1}
    episodes.append(cur)
    rows = []
    for e in episodes[-8:]:
        w = f'{max(int(e["end"] - e["start"]) + 60, 60)}s'
        over1 = _scalar(f'sum(increase(etcd_disk_wal_fsync_duration_seconds_count[{w}] @ {e["end"] + 60})) - '
                        f'sum(increase(etcd_disk_wal_fsync_duration_seconds_bucket{{le="1.024"}}[{w}] @ {e["end"] + 60}))')
        over8 = _scalar(f'sum(increase(etcd_disk_wal_fsync_duration_seconds_count[{w}] @ {e["end"] + 60})) - '
                        f'sum(increase(etcd_disk_wal_fsync_duration_seconds_bucket{{le="8.192"}}[{w}] @ {e["end"] + 60}))')
        hosts = ", ".join(sorted(h.replace("k8s-south-", "") for h in e["hosts"]))
        rows.append((f'storage stall {_hm(e["start"])}', "info",
                     f'{(e["end"] - e["start"]) / 60 + 1:.0f} min span, {e["minutes"]} stalled min; hosts: {hosts}; '
                     f'etcd WAL fsync >1s: {over1:.0f}, >8s: {over8:.0f}'))
    rows.insert(0, (f"storage stalls, {days}d", "info", f"{len(episodes)} episode(s)" + (", last 8 shown" if len(episodes) > 8 else "")))
    return rows


def _scalar(expr):
    r = _q(expr)
    return float(r[0]["value"][1]) if r else 0.0


def _hm(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%m-%d %H:%M")
