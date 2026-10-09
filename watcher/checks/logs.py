"""Where the checks read LOGS from: Grafana Cloud's Loki, or our own VictoriaLogs — the logs twin of
metrics.py (project self-hosted-metrics, task victorialogs, 2026-10-09).

Chosen by the environment, so a check needs no code change to move, and both backends can be graded
side by side during the dual-write:

  LOGS_BACKEND=loki          (default) GRAFANA_URL + GRAFANA_TOKEN, through the datasource proxy
  LOGS_BACKEND=victorialogs  VM_QUERY_URL (vmauth routes /select/logsql/* to VictoriaLogs),
                             VM_READ_TOKEN, VM_CA_FILE — the SAME settings metrics.py uses

A check asks one kind of question — how many lines matched, over a window — so a query is written
ONCE, as data, and rendered for each backend here rather than kept as two strings that can drift:

  count([("job", "=", "postgresql"), ("instance", "=", "k8s-south-postgresql")],
        line_re="too many clients", window="15m")

  LogQL   sum(count_over_time({job="postgresql",instance="k8s-south-postgresql"} |~ "too many clients" [15m]))
  LogsQL  _time:15m {job="postgresql",instance="k8s-south-postgresql"} ~"too many clients" | stats count() n

Both match the regex anywhere in the line, both are RE2, so (?i) means the same in each. That only
holds because VictoriaLogs keeps each pushed line whole (-loki.disableMessageParsing, c171b0c):
without it a JSON line has no _msg to match.

Returns a number of lines, 0 when nothing matched, and RAISES when the backend could not answer —
a failed query is never zero (infra-objectives rule 8).
"""
import json, os, ssl, time, urllib.parse, urllib.request

GRAFANA_LOKI = "/api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1"
OPS = ("=", "!=", "=~", "!~")

BACKEND = (os.environ.get("LOGS_BACKEND") or "loki").strip().lower()
if BACKEND not in ("loki", "victorialogs"):
    raise SystemExit(f"LOGS_BACKEND={BACKEND!r}: expected 'loki' or 'victorialogs'")


def _grafana():
    g = (os.environ.get("GRAFANA_URL") or "").strip().rstrip("/")
    g = g if g.startswith("http") or not g else "https://" + g
    return g, (os.environ.get("GRAFANA_TOKEN") or "").strip()


def configured():
    """(ok, what is missing) — so a check can exit 2 (facts not established) rather than crash."""
    if BACKEND == "loki":
        g, t = _grafana()
        return (bool(g and t), "GRAFANA_URL / GRAFANA_TOKEN")
    return (bool(os.environ.get("VM_QUERY_URL")), "VM_QUERY_URL")


def describe():
    if BACKEND == "loki":
        return f"loki ({_grafana()[0] or 'unset'})"
    return f"victorialogs ({os.environ.get('VM_QUERY_URL', 'unset')})"


def wake_if_needed():
    """A free Grafana Cloud stack idles; our own backend does not. True if a stack had to be woken."""
    if BACKEND != "loki":
        return False
    from grafana_wake import wake
    return wake(*_grafana())


def _selector(stream):
    parts = []
    for label, op, value in stream:
        if op not in OPS:
            raise ValueError(f"stream matcher op {op!r}: expected one of {OPS}")
        parts.append(f"{label}{op}{json.dumps(value)}")
    return "{" + ",".join(parts) + "}"


def render(stream, line_re=None, window="15m"):
    """The query each backend is sent, for display and for comparing the two."""
    sel = _selector(stream)
    if BACKEND == "loki":
        filt = f" |~ {json.dumps(line_re)}" if line_re else ""
        return f"sum(count_over_time({sel}{filt} [{window}]))"
    filt = f" ~{json.dumps(line_re)}" if line_re else ""
    return f"_time:{window} {sel}{filt} | stats count() n"


def _get(url, headers, ctx=None, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.load(r)


def count(stream, line_re=None, window="15m", timeout=30):
    """Lines matching `stream` (and `line_re`, if given) over the last `window`. 0 = none matched."""
    q = render(stream, line_re, window)
    if BACKEND == "loki":
        g, t = _grafana()
        params = {"query": q, "time": int(time.time()) * 10**9}
        d = _get(f"{g}{GRAFANA_LOKI}/query?{urllib.parse.urlencode(params)}",
                 {"Authorization": f"Bearer {t}"}, timeout=timeout)
    else:
        base = os.environ["VM_QUERY_URL"].strip().rstrip("/")
        tok = (os.environ.get("VM_READ_TOKEN") or "").strip()
        ca = (os.environ.get("VM_CA_FILE") or "").strip()
        ctx = ssl.create_default_context(cafile=ca) if (base.startswith("https") and ca) else None
        d = _get(f"{base}/select/logsql/stats_query?{urllib.parse.urlencode({'query': q})}",
                 {"Authorization": f"Bearer {tok}"} if tok else {}, ctx, timeout)
    if d.get("status") != "success":
        raise RuntimeError(f"{BACKEND} query failed: {str(d)[:120]}")
    # No series = no matching lines over the window (Loki); VictoriaLogs answers 0 or no series.
    return sum(float(r["value"][1]) for r in (d.get("data") or {}).get("result") or [])
