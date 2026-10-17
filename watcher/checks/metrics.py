"""Where the checks read METRICS from: our own VictoriaMetrics, behind vmauth on the watcher VPS.
Logs are in logs.py and work the same way (project self-hosted-metrics).

  VM_QUERY_URL   e.g. https://vps71884.dreamhostps.com:8427
  VM_READ_TOKEN  a vmauth READ token; optional on a loopback URL
  VM_CA_FILE     the pinned certificate; optional for plain http

**There was a METRICS_BACKEND switch here until 2026-10-17, and its DEFAULT was `grafana`.** That
was right while both backends were being graded side by side during the dual-write, and it became
a trap the moment metrics left Grafana Cloud on 2026-10-08: anything that did not set the variable
silently queried a stack holding none of the estate's data. The hourly checks set it in
watcher.env and were fine; `estate-status.py` run by hand through agent/run.sh did not, and so
reported **"NOTHING FROM SOUTH IS REPORTING"** about a perfectly healthy estate — the command
infra-objectives/CLAUDE.md names as "the answer to 'give the estate a look'", answering it wrongly
for nine days, in the shape of a catastrophic finding.

So the switch is gone rather than re-defaulted. A configuration option whose wrong value produces a
convincing false alarm is worth more as one less option: there is one backend, naming it cannot be
forgotten, and nothing can disagree about which one is live.

Two VictoriaMetrics behaviours differ from Prometheus/Mimir, and both are documented behaviour
rather than data loss (measured 2026-10-06): a query "at now" does not see the newest ~30 s
(-search.latencyOffset), so freshness reads ~30 s older; and MetricsQL's rate() does not extrapolate
at window edges, so a quantile over rate() can differ by about 1% on the same samples. No threshold
in a check should sit on a knife edge either way.
"""
import json, os, ssl, urllib.parse, urllib.request


def configured():
    """(ok, what is missing) — so a check can exit 2 (facts not established) rather than crash."""
    return (bool(os.environ.get("VM_QUERY_URL")), "VM_QUERY_URL")


def describe():
    return f"victoriametrics ({os.environ.get('VM_QUERY_URL', 'unset')})"


def _base_headers_context():
    url = os.environ["VM_QUERY_URL"].strip().rstrip("/") + "/api/v1"
    tok = (os.environ.get("VM_READ_TOKEN") or "").strip()
    ca = (os.environ.get("VM_CA_FILE") or "").strip()
    ctx = ssl.create_default_context(cafile=ca) if (url.startswith("https") and ca) else None
    return url, ({"Authorization": f"Bearer {tok}"} if tok else {}), ctx


def get(path, params, timeout=30):
    """GET <backend>/api/v1<path>?<params> and return the decoded JSON. Raises on any HTTP error."""
    base, headers, ctx = _base_headers_context()
    req = urllib.request.Request(f"{base}{path}?{urllib.parse.urlencode(params)}", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.load(r)


def query(expr, timeout=30):
    """Instant query -> the raw `result` list (possibly empty)."""
    return get("/query", {"query": expr}, timeout)["data"]["result"]


def wake_if_needed():
    """Kept as a no-op so callers need not care: a free Grafana Cloud stack idled its query path and
    had to be woken before a read could be trusted. Our own backend does not idle, so there is
    nothing to wake and nothing for a caller to report."""
    return False
