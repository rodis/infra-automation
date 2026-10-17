"""Where the checks read LOGS from: our own VictoriaLogs, behind the same vmauth as the metrics —
the logs twin of metrics.py (project self-hosted-metrics, task victorialogs, 2026-10-09).

  VM_QUERY_URL   vmauth routes /select/logsql/* to VictoriaLogs
  VM_READ_TOKEN  }  the SAME settings metrics.py uses
  VM_CA_FILE     }

A check asks one kind of question — how many lines matched, over a window — so a query is written
ONCE, as data, and rendered here rather than built by hand at each call site:

  count([("job", "=", "postgresql"), ("instance", "=", "k8s-south-postgresql")],
        line_re="too many clients", window="15m")

  LogsQL  _time:15m {job="postgresql",instance="k8s-south-postgresql"} ~"too many clients" | stats count() n

**There was a LOGS_BACKEND switch here until 2026-10-17, defaulting to `loki`**, and the same
query was rendered in LogQL too so the two could be compared line for line during the cutover.
That earned its keep — the cutover happened only after 24 runs in which every query matched on
both. It stopped earning it the moment logs left Grafana Cloud, and became the same trap
metrics.py describes: a caller that did not set the variable queried a backend holding nothing and
got a confident zero. Zero matching lines is exactly what "healthy" looks like in a log check, so
the wrong default here is quieter and worse than it was for metrics.

Matching is RE2 and applies anywhere in the line, so (?i) means what it does in Loki. That only
holds because VictoriaLogs keeps each pushed line whole (-loki.disableMessageParsing, c171b0c):
without it a JSON line has no _msg to match, and every text check silently misses.

Returns a number of lines, 0 when nothing matched, and RAISES when the backend could not answer —
a failed query is never zero (infra-objectives rule 8).
"""
import json, os, ssl, urllib.parse, urllib.request

OPS = ("=", "!=", "=~", "!~")


def configured():
    """(ok, what is missing) — so a check can exit 2 (facts not established) rather than crash."""
    return (bool(os.environ.get("VM_QUERY_URL")), "VM_QUERY_URL")


def describe():
    return f"victorialogs ({os.environ.get('VM_QUERY_URL', 'unset')})"


def wake_if_needed():
    """A no-op, kept so callers need not care — see metrics.wake_if_needed."""
    return False


def _selector(stream):
    parts = []
    for label, op, value in stream:
        if op not in OPS:
            raise ValueError(f"stream matcher op {op!r}: expected one of {OPS}")
        parts.append(f"{label}{op}{json.dumps(value)}")
    return "{" + ",".join(parts) + "}"


def render(stream, line_re=None, window="15m"):
    """The query the backend is sent, for display and for reading back what a check asked."""
    sel = _selector(stream)
    filt = f" ~{json.dumps(line_re)}" if line_re else ""
    return f"_time:{window} {sel}{filt} | stats count() n"


def _get(url, headers, ctx=None, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.load(r)


def count(stream, line_re=None, window="15m", timeout=30):
    """Lines matching `stream` (and `line_re`, if given) over the last `window`. 0 = none matched."""
    q = render(stream, line_re, window)
    base = os.environ["VM_QUERY_URL"].strip().rstrip("/")
    tok = (os.environ.get("VM_READ_TOKEN") or "").strip()
    ca = (os.environ.get("VM_CA_FILE") or "").strip()
    ctx = ssl.create_default_context(cafile=ca) if (base.startswith("https") and ca) else None
    d = _get(f"{base}/select/logsql/stats_query?{urllib.parse.urlencode({'query': q})}",
             {"Authorization": f"Bearer {tok}"} if tok else {}, ctx, timeout)
    if d.get("status") != "success":
        raise RuntimeError(f"victorialogs query failed: {str(d)[:120]}")
    # No series = no matching lines over the window.
    return sum(float(r["value"][1]) for r in (d.get("data") or {}).get("result") or [])
