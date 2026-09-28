"""Wake a sleeping free-tier Grafana Cloud stack before a check reads from it.

A free stack idles its query path when nobody has asked for a while. Grafana's own front end wakes
first and answers /api/health in a fraction of a second, while the Prometheus datasource behind it
is still cold: the first queries 502 after ~10s or take 20s+ (measured 2026-09-28, when that failed
the hourly cluster-status and platform-status runs with timeouts). Polling /api/health, as
estate-status did, therefore says "awake" too early. The only honest probe is a real query through
the same datasource proxy the checks use.

Only a stack that is WAKING is waited for — 502/503/504, {"code":"Loading"}, or a timeout. Any other
answer (a 401 from a revoked token, a 404 from a renamed datasource) returns at once, so the check's
own first query still fails loudly and the path is reported broken rather than asleep.
"""
import json, time, urllib.error, urllib.parse, urllib.request

PROM_PROBE = "/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query"
WAKING = {502, 503, 504}


def wake(grafana, token, wait=180, every=10, timeout=30):
    """Returns True if the stack had to be woken, False if it answered at once or is not merely asleep."""
    url = f"{grafana.rstrip('/')}{PROM_PROBE}?{urllib.parse.urlencode({'query': 'vector(1)'})}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    woke, deadline = False, time.monotonic() + wait
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                json.load(r)
            return woke
        except urllib.error.HTTPError as e:
            try:
                loading = e.code in WAKING or json.load(e).get("code") == "Loading"
            except Exception:
                loading = e.code in WAKING
        except (TimeoutError, urllib.error.URLError):
            loading = True
        except Exception:
            loading = False
        if not loading or time.monotonic() > deadline:
            return woke
        if not woke:
            print(f"   grafana's query path is asleep; waiting up to {wait}s for it to wake ...")
        woke = True
        time.sleep(every)
