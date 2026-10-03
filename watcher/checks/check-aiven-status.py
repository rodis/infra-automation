#!/usr/bin/env python3
"""Evaluate objectives/aiven-healthy.yml for the Aiven project behind Aware. Read-only.

  ./run.sh python3 -u checks/check-aiven-status.py [--json PATH]          # on the watcher
  ./agent/run.sh python3 <path>/check-aiven-status.py [--json PATH]       # from infra-objectives

Written 2026-10-03, after Aiven rotated project `aware` to "Project CA GEN 2" and the Kafka broker
stopped accepting client certificates from the old CA at ~08:12 UTC. Every Kafka client (the
runtime and Vector) still held an old-CA certificate; nothing reached Neon all morning, and it
looked like a database problem. The new CA had existed since 2026-08-06, so the predicate that
matters is the LEADING one: a client certificate not issued by the project's CURRENT CA. The live
handshake alone stays green for as long as the broker trusts both CAs, which was two months.

Exit codes, the contract every check-*.py shares (see infra-objectives/CLAUDE.md, "Scheduled checks"):
  0  every success: predicate holds        1  a predicate is false        2  facts not established

Two outputs from the same records. stdout is the human report, unchanged in shape. `--json PATH`
also writes a machine-readable result (schema `check-result/v1`, below) for an agent to act on:
one record per predicate with a STABLE id, the subject it is about, the verdict and the evidence.
The id is what an objective maps to a remediation verb, so ids are never renamed casually — a
consumer keyed on one would silently stop matching.

  {"schema": "check-result/v1", "check": "aiven-healthy", "target": "aware",
   "observed_at": ISO-8601, "exit_code": 0|1|2,
   "outcome": "satisfied" | "not_satisfied" | "facts_not_established",
   "error": str | null,                                    # set when facts could not be read at all
   "predicates": [{"id", "subject", "verdict": "pass"|"fail"|"unknown", "detail", "evidence": {}}],
   "reported":   [{"id", "subject", "flag", "detail", "evidence": {}}]}

The service list from the API carries each user's PASSWORD and ACCESS KEY next to the certificate.
This script reads `access_cert` and nothing else from a user, never prints or records a user object,
and parses certificates through openssl on stdin so no file is left behind. Evidence holds only
public certificate fields.
"""
import argparse, datetime, json, os, re, subprocess, sys, urllib.error, urllib.request

API = "https://api.aiven.io/v1"
PROJECT = "aware"
CHECK = "aiven-healthy"
# Services Aware and n8n depend on. A service missing from the project, or not RUNNING, is a finding.
# Anything else in the project is reported, not graded (valkey is POWEROFF as of 2026-10-03; whether
# that is intended is the owner's call — see the objective).
EXPECTED = {"kafka-13776261": "kafka", "pg-af720c5": "pg"}
CERT_MIN_DAYS = 30

results, reported = [], []
NOW = datetime.datetime.now(datetime.timezone.utc)


def verdict(pid, subject, name, ok, detail="", evidence=None):
    v = "pass" if ok is True else "fail" if ok is False else "unknown"
    results.append({"id": pid, "subject": subject, "verdict": v, "detail": detail, "evidence": evidence or {}})
    print(f"  {v.upper():8} {name:58} {detail}")


def report(pid, subject, name, flag, detail="", evidence=None):
    reported.append({"id": pid, "subject": subject, "flag": flag.lower(), "detail": detail, "evidence": evidence or {}})
    print(f"  {flag:8} {name:58} {detail}")


def guarded(pid, subject, name, fn):
    try: fn()
    except Exception as e:
        verdict(pid, subject, name, None, f"could not evaluate: {type(e).__name__}: {str(e)[:110]}")


def iso(d): return d.isoformat().replace("+00:00", "Z") if d else None


def api(path):
    token = os.environ.get("AIVEN_TOKEN") or ""
    if not token:
        raise RuntimeError("AIVEN_TOKEN not set — run through run.sh")
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"aivenv1 {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def cert_info(pem):
    """Public fields of a PEM certificate, via openssl on stdin."""
    out = subprocess.run(["openssl", "x509", "-noout", "-subject", "-issuer", "-serial", "-enddate", "-nameopt", "RFC2253"],
                         input=pem, capture_output=True, text=True, timeout=15, check=True).stdout

    def cn(line):
        m = re.search(r"CN=([^,]+)", line); return m.group(1).strip() if m else line.strip()
    f = dict(l.split("=", 1) for l in out.strip().splitlines())
    not_after = datetime.datetime.strptime(f["notAfter"].strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
    return {"subject": cn(f["subject"]), "issuer": cn(f["issuer"]), "serial": f["serial"].strip(), "not_after": not_after}


def broker_client_cas(host, port):
    """The CA names a TLS server says it accepts client certificates from. A public handshake: no key."""
    out = subprocess.run(["openssl", "s_client", "-connect", f"{host}:{port}", "-servername", host],
                         input="", capture_output=True, text=True, timeout=30).stdout
    m = re.search(r"Acceptable client certificate CA names\n(.*?)\n(?:Requested|Client Certificate Types|Shared)", out, re.S)
    if not m:
        raise RuntimeError("handshake returned no acceptable-CA list")
    return [re.search(r"CN\s*=\s*(.+)$", l).group(1).strip() for l in m.group(1).splitlines() if "CN" in l]


def finish(code, error=None):
    outcome = {0: "satisfied", 1: "not_satisfied"}.get(code, "facts_not_established")
    if ARGS.json:
        doc = {"schema": "check-result/v1", "check": CHECK, "target": PROJECT, "observed_at": iso(NOW),
               "exit_code": code, "outcome": outcome, "error": error,
               "predicates": results, "reported": reported}
        tmp = ARGS.json + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, ARGS.json)          # a reader never sees a half-written file
    sys.exit(code)


# ------------------------------------------------------------------------------------------------
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--json", metavar="PATH", help="also write the check-result/v1 document here")
ARGS = parser.parse_args()

print(f"{CHECK} [{PROJECT}] — {NOW:%Y-%m-%d %H:%M UTC}")
try:
    ca_pem = api(f"/project/{PROJECT}/kms/ca")["certificate"]
    current_ca = cert_info(ca_pem)
    services = {s["service_name"]: s for s in api(f"/project/{PROJECT}/service")["services"]}
except Exception as e:
    msg = f"could not read the Aiven API: {type(e).__name__}: {str(e)[:150]}"
    print(f"\nFACTS NOT ESTABLISHED — {msg}")
    finish(2, msg)
print(f"  current project CA: {current_ca['subject']} (until {current_ca['not_after']:%Y-%m-%d})")
CA_EVIDENCE = {"current_ca": current_ca["subject"], "current_ca_serial": current_ca["serial"],
               "current_ca_not_after": iso(current_ca["not_after"])}

print("\n== services")
for name, stype in EXPECTED.items():
    def _svc(name=name, stype=stype):
        s = services.get(name)
        verdict("service.running", name, f"{name} exists, type {stype}, RUNNING",
                bool(s) and s["service_type"] == stype and s["state"] == "RUNNING",
                f"{s['service_type']} {s['state']} ({s['plan']})" if s else "missing from the project",
                {"expected_type": stype, "type": s and s["service_type"], "state": s and s["state"], "plan": s and s["plan"]})
    guarded("service.running", name, f"{name} RUNNING", _svc)
for name, s in sorted(services.items()):
    if name not in EXPECTED:
        report("service.state.ungraded", name, f"{name} (not graded)", "ok" if s["state"] == "RUNNING" else "FINDING",
               f"{s['service_type']} {s['state']}", {"type": s["service_type"], "state": s["state"]})

print("\n== Kafka client certificates")
for name, s in sorted(services.items()):
    if s["service_type"] != "kafka":
        continue
    comp = next((c for c in s.get("components", []) if c.get("component") == "kafka"
                 and c.get("route") == "dynamic" and c.get("usage") == "primary"), None)
    accepted = None
    try:
        accepted = broker_client_cas(comp["host"], comp["port"]) if comp else None
    except Exception as e:
        verdict("kafka.broker.handshake", name, f"{name}: broker answers a TLS handshake", None,
                f"could not evaluate: {str(e)[:100]}", {"host": comp and comp["host"], "port": comp and comp["port"]})
    # A Kafka service authenticating by certificate must show at least one. A read-only token may
    # not be shown users' credentials at all, and then the loop below would grade nothing and the
    # check would PASS having looked at nothing — an empty host pattern, one layer up. That is
    # "facts not established", never a pass.
    if not any(u.get("access_cert") for u in s.get("users", [])):
        verdict("kafka.client_cert.visible", name, f"{name}: client certificates visible to this token", None,
                "could not evaluate: no user carries access_cert (token scope too narrow?)")
    for user in s.get("users", []):
        pem = user.get("access_cert")      # the ONLY field read from a user object
        if not pem:
            continue
        subject = f"{name}/{user['username']}"

        def _user(pem=pem, subject=subject):
            c = cert_info(pem)
            ev = {"issuer": c["issuer"], "serial": c["serial"], "not_after": iso(c["not_after"]), **CA_EVIDENCE}
            # The leading predicate: false from 2026-08-06, two months before anything broke.
            verdict("kafka.client_cert.current_ca", subject, f"{subject}: issued by the current project CA",
                    c["issuer"] == current_ca["subject"], f"issuer {c['issuer']!r}", ev)
            days = (c["not_after"] - NOW).days
            verdict("kafka.client_cert.expiry", subject, f"{subject}: expires more than {CERT_MIN_DAYS} days out",
                    days > CERT_MIN_DAYS, f"{c['not_after']:%Y-%m-%d} ({days} days)",
                    {**ev, "days_left": days, "min_days": CERT_MIN_DAYS})
            # The lagging predicate: is it broken right now.
            if accepted is not None:
                verdict("kafka.client_cert.broker_accepts", subject, f"{subject}: broker accepts its issuer",
                        c["issuer"] in accepted, f"broker accepts {accepted}",
                        {**ev, "broker_accepts": accepted, "broker": f"{comp['host']}:{comp['port']}"})
        guarded("kafka.client_cert.current_ca", subject, f"{subject} certificate", _user)

print("\n== reported — never gating")
for name, s in sorted(services.items()):
    for u in (s.get("maintenance") or {}).get("updates", []):
        due = u.get("deadline") or u.get("start_after") or "unscheduled"
        report("service.maintenance.pending", name, f"{name}: maintenance pending", "info",
               f"{(u.get('description') or '')[:60]} — due {due}",
               {"description": u.get("description"), "deadline": u.get("deadline"), "start_after": u.get("start_after")})
    for n in s.get("service_notifications") or []:
        report("service.notification", name, f"{name}: notification ({n.get('level')})", "info",
               (n.get("message") or "")[:100], {"level": n.get("level"), "message": n.get("message")})

failed = [r for r in results if r["verdict"] == "fail"]
unknown = [r for r in results if r["verdict"] == "unknown"]
print()
if failed:
    print(f"NOT SATISFIED — {len(failed)} predicate(s) false:")
    for r in failed: print(f"  - [{r['id']}] {r['subject']}: {r['detail']}")
    finish(1)
if unknown:
    print(f"FACTS NOT ESTABLISHED — {len(unknown)} predicate(s) could not be evaluated.")
    finish(2)
print(f"SATISFIED — all {len(results)} predicates hold.")
finish(0)
