"""Where the checks read METRICS from: Grafana Cloud, or our own VictoriaMetrics. Logs are in logs.py,
chosen the same way by LOGS_BACKEND (project self-hosted-metrics).

Chosen by the environment, so a check needs no code change to move, and both backends can be graded
side by side during the dual-write:

  METRICS_BACKEND=grafana          (default) GRAFANA_URL + GRAFANA_TOKEN, through the datasource proxy
  METRICS_BACKEND=victoriametrics  VM_QUERY_URL (e.g. https://vps71884.dreamhostps.com:8427),
                                   VM_READ_TOKEN (a vmauth READ token; optional on a loopback URL),
                                   VM_CA_FILE (the pinned certificate; optional for plain http)

Two behaviours differ, and both are documented VictoriaMetrics behaviour rather than data loss
(measured 2026-10-06, project self-hosted-metrics): a query "at now" does not see the newest ~30 s
(-search.latencyOffset), so freshness reads ~30 s older; and MetricsQL's rate() does not extrapolate at
window edges, so a quantile over rate() can differ by about 1% on the same samples. No threshold in a
check should sit on a knife edge either way.
"""
import json, os, ssl, urllib.parse, urllib.request

GRAFANA_PROM = "/api/datasources/proxy/uid/grafanacloud-prom/api/v1"

BACKEND = (os.environ.get("METRICS_BACKEND") or "grafana").strip().lower()
if BACKEND not in ("grafana", "victoriametrics"):
    raise SystemExit(f"METRICS_BACKEND={BACKEND!r}: expected 'grafana' or 'victoriametrics'")


def _grafana():
    g = (os.environ.get("GRAFANA_URL") or "").strip().rstrip("/")
    g = g if g.startswith("http") or not g else "https://" + g
    return g, (os.environ.get("GRAFANA_TOKEN") or "").strip()


def configured():
    """(ok, what is missing) — so a check can exit 2 (facts not established) rather than crash."""
    if BACKEND == "grafana":
        g, t = _grafana()
        return (bool(g and t), "GRAFANA_URL / GRAFANA_TOKEN")
    return (bool(os.environ.get("VM_QUERY_URL")), "VM_QUERY_URL")


def describe():
    if BACKEND == "grafana":
        return f"grafana ({_grafana()[0] or 'unset'})"
    return f"victoriametrics ({os.environ.get('VM_QUERY_URL', 'unset')})"


def _base_headers_context():
    if BACKEND == "grafana":
        g, t = _grafana()
        return g + GRAFANA_PROM, {"Authorization": f"Bearer {t}"}, None
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
    """A free Grafana Cloud stack idles; our own backend does not. True if a stack had to be woken."""
    if BACKEND != "grafana":
        return False
    from grafana_wake import wake
    return wake(*_grafana())
