#!/usr/bin/env python3
"""Evaluate objectives/gmail-healthy.yml: does the Gmail credential the estate depends on work? Read-only.

  ./run.sh python3 -u checks/check-gmail-status.py [--json PATH]           # on the watcher
  ./agent/run.sh python3 <path>/check-gmail-status.py [--json PATH]        # from infra-objectives

Written 2026-10-03. That day n8n's Gmail OAuth credential stopped refreshing at 13:00 UTC (a 7-day
Testing-mode expiry, inference ADR 0008) and took down the invoice approval gate and both Gmail
connectors. Nothing noticed; it was found while chasing a missing invoice.

It calls a CANARY, not Gmail: an n8n webhook (inference connectors/n8n/gmail-canary.workflow.json)
that lists Gmail labels with the shared 'Gmail account' credential and answers only a count. The
token for it lives in vault `watcher` and can make Gmail answer that one question, nothing more —
DreamHost administers this box as root, so a token that could search the mailbox has no place here.

The predicates are about Gmail, not about n8n. If n8n is ever retired, whatever holds the Gmail
credential next exposes the same probe and this check carries over with only the URL changed.

How each answer is graded — the point is never to confuse three different situations:

  200 {"ok": true}             reachable PASS   gmail PASS
  503 {"ok": false, "error"}   reachable PASS   gmail FAIL     the credential is broken (a finding)
  no answer / 502 / 504 / 404  reachable FAIL   gmail UNKNOWN  n8n down, or the canary not active
  401 / 403                    reachable PASS   gmail UNKNOWN  OUR token was refused: the canary is
                                                               misconfigured, which says nothing
                                                               about Gmail (facts not established)
  any other status             reachable PASS   gmail UNKNOWN  the canary itself failed unhandled

Exit codes, the contract every check-*.py shares (see infra-objectives/CLAUDE.md, "Scheduled checks"):
  0  every success: predicate holds        1  a predicate is false        2  facts not established
`--json PATH` writes the same check-result/v1 document as check-aiven-status.py.
"""
import argparse, datetime, json, os, sys, urllib.error, urllib.request

CHECK, TARGET = "gmail-healthy", "gmail-account"
results, reported = [], []
NOW = datetime.datetime.now(datetime.timezone.utc)


def verdict(pid, subject, name, ok, detail="", evidence=None):
    v = "pass" if ok is True else "fail" if ok is False else "unknown"
    results.append({"id": pid, "subject": subject, "verdict": v, "detail": detail, "evidence": evidence or {}})
    print(f"  {v.upper():8} {name:52} {detail}")


def finish(code, error=None):
    outcome = {0: "satisfied", 1: "not_satisfied"}.get(code, "facts_not_established")
    if ARGS.json:
        doc = {"schema": "check-result/v1", "check": CHECK, "target": TARGET,
               "observed_at": NOW.isoformat().replace("+00:00", "Z"), "exit_code": code, "outcome": outcome,
               "error": error, "predicates": results, "reported": reported}
        tmp = ARGS.json + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, ARGS.json)
    sys.exit(code)


parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--json", metavar="PATH", help="also write the check-result/v1 document here")
ARGS = parser.parse_args()

url = (os.environ.get("GMAIL_CANARY_URL") or "").strip()
token = (os.environ.get("GMAIL_CANARY_TOKEN") or "").strip()
print(f"{CHECK} [{TARGET}] — {NOW:%Y-%m-%d %H:%M UTC}")
if not (url and token):
    msg = "GMAIL_CANARY_URL / GMAIL_CANARY_TOKEN not set — run through run.sh"
    print(f"\nFACTS NOT ESTABLISHED — {msg}")
    finish(2, msg)

status, body, err = None, {}, None
try:
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"X-Canary-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        status, raw = r.status, r.read()
except urllib.error.HTTPError as e:
    status, raw = e.code, e.read()
except Exception as e:                       # DNS, TLS, refused, timeout: no answer at all
    err = f"{type(e).__name__}: {str(e)[:150]}"
    raw = b""
try:
    body = json.loads(raw or b"{}")
    body = body if isinstance(body, dict) else {}
except ValueError:
    body = {}

ev = {"url": url, "http_status": status}
print("\n== Gmail credential, via the canary")
if err or status in (502, 504, 404):
    why = err or ("the canary webhook is not registered (workflow inactive?)" if status == 404 else f"HTTP {status} from the proxy")
    verdict("n8n.webhook.reachable", url, "canary webhook answers", False, why, {**ev, "error": err})
    verdict("gmail.oauth.valid", "Gmail account", "Gmail credential works", None,
            "could not evaluate: the canary did not answer", ev)
elif status in (401, 403):
    verdict("n8n.webhook.reachable", url, "canary webhook answers", True, f"HTTP {status}", ev)
    verdict("gmail.oauth.valid", "Gmail account", "Gmail credential works", None,
            "could not evaluate: the canary refused the WATCHER's token (canary misconfigured, not Gmail)", ev)
elif status == 200 and body.get("ok") is True:
    verdict("n8n.webhook.reachable", url, "canary webhook answers", True, "HTTP 200", ev)
    verdict("gmail.oauth.valid", "Gmail account", "Gmail credential works", True,
            f"listed {body.get('labels')} labels", {**ev, "labels": body.get("labels")})
elif status == 503 and body.get("ok") is False:
    verdict("n8n.webhook.reachable", url, "canary webhook answers", True, "HTTP 503", ev)
    verdict("gmail.oauth.valid", "Gmail account", "Gmail credential works", False,
            f"Gmail refused: {str(body.get('error'))[:120]}", {**ev, "gmail_error": body.get("error")})
else:
    verdict("n8n.webhook.reachable", url, "canary webhook answers", True, f"HTTP {status}", ev)
    verdict("gmail.oauth.valid", "Gmail account", "Gmail credential works", None,
            f"could not evaluate: unexpected answer (HTTP {status}) — the canary itself failed", ev)

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
