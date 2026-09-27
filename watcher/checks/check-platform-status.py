#!/usr/bin/env python3
"""Evaluate objectives/platform-healthy.yml for one target. Read-only.

  ./agent/run.sh python3 agent/check-platform-status.py infra

Unattended checks never use SSH (CLAUDE.md), so the default source is Grafana: kube-state-metrics
with the objects and custom resources added on 2026-09-27 (infra-automation 9d4d473), plus public
HTTPS probes. Until 2026-09-27 it read the cluster over SSH; before that path was deleted the two
were run side by side and gave identical verdicts on all 17 predicates. The owner-approved
predicates (rule 6) did not change, only where the facts come from.

Exit codes, the contract every check-*.py shares (see CLAUDE.md, "Scheduled checks"):
  0  every success: predicate holds        1  a predicate is false        2  facts not established

It never reads a Secret: the DopplerSecret predicate was amended by the owner (2026-09-27) rather
than grant `list secrets`, which returns the values.
"""
import datetime, json, os, re, ssl, sys, urllib.error, urllib.parse, urllib.request

# infra reads south's kube-state-metrics in Grafana. production, when added, reads through AWX jobs
# (task production-cluster-check) — never SSH.
TARGETS = {"infra": {}}
# The pins live in helm-override-files. On the VPS they must be shipped with the checker.
HOF = os.environ.get("HELM_OVERRIDE_FILES", os.path.expanduser("~/Development/personal/helm-override-files"))
ISSUERS = ["letsencrypt-prod", "letsencrypt-staging"]
CM_DEPLOYS = ["cert-manager", "cert-manager-cainjector", "cert-manager-webhook"]
PLATFORM_NS = {"cert-manager", "doppler-operator-system", "traefik"}

results, reported = [], []
NOW = datetime.datetime.now(datetime.timezone.utc)


def verdict(name, ok, detail=""):
    v = "PASS" if ok is True else "FAIL" if ok is False else "UNKNOWN"
    results.append((v, name, detail)); print(f"  {v:8} {name:50} {detail}")


def report(name, flag, detail=""):
    reported.append((flag, name, detail)); print(f"  {flag:8} {name:50} {detail}")


def guarded(name, fn):
    try: fn()
    except Exception as e: verdict(name, None, f"could not evaluate: {type(e).__name__}: {str(e)[:110]}")


def ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def from_epoch(x):
    return datetime.datetime.fromtimestamp(x, datetime.timezone.utc) if x else None


def pin(path, pattern):
    m = re.search(pattern, open(os.path.join(HOF, path)).read(), re.M)
    if not m: raise ValueError(f"no pin matching {pattern!r} in {path}")
    return m.group(1)


# ------------------------------------------------------------------------------------------------
# FACTS — what the source produces, and all the predicates read:
#   cp_nodes            int                       control-plane node count
#   traefik             {ready, desired, chart}
#   ingressclass        controller string or None
#   hosts               [host, ...]
#   cm_deploys          {name: {ready, desired, version}}
#   issuers             {name: bool}
#   certs               [{name, ready: bool, not_after: dt, renewal: dt}]
#   doppler             {ready: bool, images: [image, ...]}  or None when missing
#   dsecrets            [{name, sync: bool}]
#   containers          [{pod, container, restarts, started: dt, crashloop: bool}]
# ------------------------------------------------------------------------------------------------

def facts_from_grafana():
    G = (os.environ.get("GRAFANA_URL") or "").rstrip("/")
    G = G if G.startswith("http") or not G else "https://" + G
    T = os.environ.get("GRAFANA_TOKEN") or ""
    if not (G and T):
        raise RuntimeError("GRAFANA_URL / GRAFANA_TOKEN not set — run through ./agent/run.sh")

    def q(expr):
        url = f"{G}/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer " + T}), timeout=30) as r:
            return json.load(r)["data"]["result"]

    def one(expr):
        r = q(expr); return float(r[0]["value"][1]) if r else None

    # Freshness first: every fact below is from kube-state-metrics, so if it is not being scraped they
    # are all stale — and stale looks exactly like healthy (rule 8).
    age = one('time() - max(timestamp(kube_daemonset_status_number_ready{daemonset="traefik"}))')
    if age is None or age > 300:
        raise RuntimeError(f"kube-state-metrics not scraped recently (age {age}) — facts would be stale")

    f = {"cp_nodes": int(one('count(kube_node_role{role="control-plane"})') or 0)}
    f["traefik"] = {
        "ready": one('kube_daemonset_status_number_ready{namespace="traefik",daemonset="traefik"}'),
        "desired": one('kube_daemonset_status_desired_number_scheduled{namespace="traefik",daemonset="traefik"}'),
        "chart": next((r["metric"].get("label_helm_sh_chart") for r in q('kube_daemonset_labels{namespace="traefik",daemonset="traefik"}')), None),
    }
    ic = q('kube_ingressclass_info{ingressclass="nginx"}')
    f["ingressclass"] = ic[0]["metric"].get("controller") if ic else None
    f["hosts"] = sorted({r["metric"]["host"] for r in q("kube_ingress_path") if r["metric"].get("host")})

    ready = {r["metric"]["deployment"]: float(r["value"][1]) for r in q('kube_deployment_status_replicas_ready{namespace="cert-manager"}')}
    spec = {r["metric"]["deployment"]: float(r["value"][1]) for r in q('kube_deployment_spec_replicas{namespace="cert-manager"}')}
    ver = {r["metric"]["deployment"]: r["metric"].get("label_app_kubernetes_io_version") for r in q('kube_deployment_labels{namespace="cert-manager"}')}
    f["cm_deploys"] = {n: {"ready": ready.get(n, 0), "desired": spec.get(n, 0), "version": ver.get(n)} for n in spec}

    f["issuers"] = {r["metric"]["name"]: float(r["value"][1]) == 1 for r in q('kube_customresource_clusterissuer_condition{type="Ready"}')}

    certs = {}
    for r in q('kube_customresource_certificate_condition{type="Ready"}'):
        certs.setdefault(f'{r["metric"]["namespace"]}/{r["metric"]["name"]}', {})["ready"] = float(r["value"][1]) == 1
    for metric, key in (("kube_customresource_certificate_not_after", "not_after"), ("kube_customresource_certificate_renewal_time", "renewal")):
        for r in q(metric):
            certs.setdefault(f'{r["metric"]["namespace"]}/{r["metric"]["name"]}', {})[key] = from_epoch(float(r["value"][1]))
    f["certs"] = [{"name": k, "ready": v.get("ready", False), "not_after": v.get("not_after"), "renewal": v.get("renewal")} for k, v in certs.items()]

    dr = one('kube_deployment_status_replicas_ready{namespace="doppler-operator-system",deployment="doppler-operator-controller-manager"}')
    ds = one('kube_deployment_spec_replicas{namespace="doppler-operator-system",deployment="doppler-operator-controller-manager"}')
    imgs = sorted({r["metric"].get("image", "") for r in q('kube_pod_container_info{namespace="doppler-operator-system",container="manager"}')})
    f["doppler"] = None if ds is None else {"ready": dr == ds, "images": imgs}

    f["dsecrets"] = [{"name": f'{r["metric"]["name"]} -> {r["metric"]["namespace"]}', "sync": float(r["value"][1]) == 1}
                     for r in q('kube_customresource_dopplersecret_condition{type="secrets.doppler.com/SecretSyncReady"}')]

    nsre = "|".join(sorted(PLATFORM_NS))
    started = {r["metric"]["pod"]: from_epoch(float(r["value"][1])) for r in q(f'kube_pod_start_time{{namespace=~"{nsre}"}}')}
    crash = {(r["metric"]["pod"], r["metric"]["container"]) for r in q(f'kube_pod_container_status_waiting_reason{{namespace=~"{nsre}",reason="CrashLoopBackOff"}} == 1')}
    f["containers"] = [{"pod": r["metric"]["pod"], "container": r["metric"]["container"], "restarts": int(float(r["value"][1])),
                        "started": started.get(r["metric"]["pod"]), "crashloop": (r["metric"]["pod"], r["metric"]["container"]) in crash}
                       for r in q(f'kube_pod_container_status_restarts_total{{namespace=~"{nsre}"}}')]
    return f


# ------------------------------------------------------------------------------------------------
target = sys.argv[1] if len(sys.argv) > 1 else ""
if target not in TARGETS:
    print(f"usage: {sys.argv[0]} <{'|'.join(TARGETS)}>"); sys.exit(2)
print(f"platform-healthy [{target}] from Grafana — {NOW:%Y-%m-%d %H:%M UTC}")
try:
    F = facts_from_grafana()
except Exception as e:
    print(f"\nFACTS NOT ESTABLISHED — could not read the cluster: {e}"); sys.exit(2)

print("\n== Traefik")
def _ds():
    t, cp = F["traefik"], F["cp_nodes"]
    verdict("DaemonSet ready == scheduled == control-plane nodes",
            t["ready"] is not None and t["ready"] == t["desired"] == cp and cp > 0,
            f"{t['ready']:.0f} ready / {t['desired']:.0f} scheduled / {cp} control-plane" if t["ready"] is not None else "no data")
guarded("DaemonSet ready", _ds)

def _tpin():
    want = "traefik-" + pin("traefik/helmChart.yml", r"^\s*version:\s*['\"]?([\w.\-]+)")
    verdict("chart matches pin", F["traefik"]["chart"] == want, f"{F['traefik']['chart']} (pin {want})")
guarded("chart matches pin", _tpin)

guarded("IngressClass nginx", lambda: verdict("IngressClass nginx -> k8s.io/ingress-nginx",
        F["ingressclass"] == "k8s.io/ingress-nginx", F["ingressclass"] or "missing"))

for h in F["hosts"]:
    def _probe(h=h):
        try:
            code = urllib.request.urlopen(urllib.request.Request(f"https://{h}/"), timeout=15, context=ssl.create_default_context()).status
        except urllib.error.HTTPError as e:
            code = e.code
        verdict(f"https://{h}/ valid TLS, status < 500", code < 500, f"HTTP {code}")
    guarded(f"https://{h}/", _probe)
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try:
            code = urllib.request.build_opener(NoRedirect).open(f"http://{h}/", timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        report(f"http://{h}/ redirects to https", "ok" if code in (301, 308) else "FINDING", f"HTTP {code}")
    except Exception as e:
        report(f"http://{h}/ redirects to https", "UNKNOWN", str(e)[:100])
if not F["hosts"]:
    verdict("at least one Ingress host to probe", False, "no Ingress hosts found")

print("\n== cert-manager")
def _cmd():
    by = F["cm_deploys"]
    bad = [n for n in CM_DEPLOYS if n not in by or by[n]["ready"] != by[n]["desired"] or by[n]["desired"] < 1]
    verdict("controller, cainjector, webhook ready", not bad, "all ready" if not bad else f"not ready: {bad}")
    want = pin("cert-manager/helmChart.yml", r"^\s*version:\s*['\"]?([\w.\-]+)")
    have = by.get("cert-manager", {}).get("version")
    verdict("version matches pin", have == want, f"{have} (pin {want})")
guarded("cert-manager deployments", _cmd)

guarded("ClusterIssuers", lambda: verdict("ClusterIssuers prod + staging Ready",
        all(F["issuers"].get(i) is True for i in ISSUERS), ", ".join(f"{i}={F['issuers'].get(i)}" for i in ISSUERS)))

for c in F["certs"]:
    def _cert(c=c):
        days = (c["not_after"] - NOW).days if c["not_after"] else None
        overdue = bool(c["renewal"] and (NOW - c["renewal"]).total_seconds() > 86400)
        detail = f"ready={c['ready']}, {days}d left" + (f", renewal {c['renewal']:%Y-%m-%d}" if c["renewal"] else "")
        verdict(f"Certificate {c['name']}", c["ready"] and days is not None and days > 14 and not overdue,
                detail + (" OVERDUE" if overdue else ""))
    guarded(f"Certificate {c['name']}", _cert)
if not F["certs"]:
    report("Certificates", "info", "none in the cluster")

print("\n== Doppler")
def _dop():
    d = F["doppler"]
    if d is None:
        verdict("operator deployment ready, image matches pin", False, "deployment missing"); return
    want = pin("doppler/playbooks/install.yml", r"releases/download/v?([\d.]+)/")
    tag_ok = any(i.endswith(":" + want) or i.endswith(":v" + want) for i in d["images"])
    verdict("operator deployment ready, image matches pin", d["ready"] and tag_ok,
            f"ready={d['ready']}, {[i.split('/')[-1] for i in d['images']]} (pin {want})")
guarded("Doppler operator", _dop)

for s in F["dsecrets"]:
    guarded(f"DopplerSecret {s['name']}", lambda s=s: verdict(f"DopplerSecret {s['name']}", s["sync"], f"sync={s['sync']}"))
if not F["dsecrets"]:
    report("DopplerSecrets", "info", "none in the cluster")

print("\n== restarts — an app that keeps restarting is not healthy")
for c in F["containers"]:
    def _restarts(c=c):
        if not c["started"]:
            verdict(f"{c['pod'][:40]}/{c['container']} restarts", None, "no pod start time"); return
        age_d = max((NOW - c["started"]).total_seconds() / 86400, 1e-6)
        if age_d < 1:
            ok, rate = c["restarts"] <= 1, f"{c['restarts']} in {age_d*24:.1f}h (young pod: at most 1)"
        else:
            ok, rate = c["restarts"] / age_d < 1, f"{c['restarts'] / age_d:.2f}/day ({c['restarts']} in {age_d:.0f}d)"
        verdict(f"{c['pod'][:40]}/{c['container']} restarts", ok and not c["crashloop"], rate + (", CrashLoopBackOff" if c["crashloop"] else ""))
    guarded(f"{c['pod']} restarts", _restarts)

# ------------------------------------------------------------------------------------------------
fails = [r for r in results if r[0] == "FAIL"]; unknown = [r for r in results if r[0] == "UNKNOWN"]
findings = [r for r in reported if r[0] == "FINDING"]
print()
if fails:
    print(f"NOT SATISFIED — {len(fails)} predicate(s) false: " + "; ".join(r[1] for r in fails)); code = 1
elif unknown:
    print(f"FACTS NOT ESTABLISHED — {len(unknown)} could not be evaluated: " + "; ".join(r[1] for r in unknown)); code = 2
else:
    print(f"SATISFIED — all {len(results)} predicates hold."); code = 0
    # Rule 6 guard for drafts. The objective file exists only in infra-objectives (the Mac), which is
    # where drafts are run; on the VPS it is absent and only approved objectives are deployed.
    objective = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "objectives", "platform-healthy.yml")
    if os.path.exists(objective) and "NOT YET APPROVED" in open(objective).read():
        print("  ...but the success block is PROPOSED, not approved (rule 6). Reporting facts not established.")
        code = 2
if findings:
    print("Reported findings (not gating): " + "; ".join(r[1] for r in findings))
print("Remediation: none." + (" Infra is south (rule 7) — read further, stop, report." if target == "infra" else ""))
sys.exit(code)
