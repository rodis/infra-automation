#!/usr/bin/env python3
"""Offline decision tests for etcd-leader-health.py — no etcd needed. Run: python3 tests/test_decisions.py

Each scenario gives the script a stored history and a current reading, fakes the cluster, and
checks the action. Written 2026-10-12; it caught a cooldown bug (a leader change just before the
trimmed window was missed) before the script ever ran on a cluster.
"""
import contextlib, importlib.util, io, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("elh", os.path.join(HERE, "..", "files", "etcd-leader-health.py"))
L, F1, F2 = "aaa", "bbb", "ccc"


def run(stored, current, leader_now=L, local=L, armed=False, since=None):
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    st = tempfile.mktemp(); m.STATE = st; m.ARMED = armed
    now = 1_000_000.0; m.time.time = lambda: now
    state = {"samples": [{"ts": now - a, "leader": ld, "slow": {L: l, F1: f1, F2: f2}} for a, ld, l, f1, f2 in stored]}
    if since is not None:
        state["leader_since"] = now - since
    json.dump(state, open(st, "w"))
    cur = dict(zip((L, F1, F2), current))
    m.cluster = lambda: ({L: "https://l", F1: "https://f1", F2: "https://f2"}, leader_now)
    ids = {"https://l": L, "https://f1": F1, "https://f2": F2, "https://127.0.0.1:2379": local}
    m.slow_total = lambda ep: (cur[ids[ep]], ids[ep])
    moved = []; m.etcdctl = lambda *a, endpoint=None: moved.append((a, endpoint))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m.main()
    os.remove(st)
    return json.loads(out.getvalue().splitlines()[-1]), moved


S = [(700, L, 0, 0, 0), (130, L, 1, 0, 5), (60, L, 3, 0, 6)]
CASES = [
    ("healthy leader",                        dict(stored=S, current=(2, 0, 6), since=3600), "none", None, False),
    ("degraded leader, F1 clean, F2 noisy",   dict(stored=S, current=(5, 0, 6), since=3600), "would_move", F1, False),
    ("same, armed",                           dict(stored=S, current=(5, 0, 6), since=3600, armed=True), "moved", F1, True),
    ("degraded leader, every follower slow",  dict(stored=[(700, L, 0, 0, 0), (130, L, 1, 0, 0)], current=(5, 1, 2), since=3600), "none", None, False),
    ("leader took over 5 min ago",            dict(stored=S, current=(5, 0, 6), since=300), "none", None, False),
    ("leader changed since the previous run", dict(stored=[(700, F1, 0, 0, 0), (130, F1, 1, 0, 0), (60, F1, 2, 0, 0)], current=(5, 0, 0), since=3600), "none", None, False),
    ("no leader_since stored (first run)",    dict(stored=S, current=(5, 0, 6)), "none", None, False),
    ("not the leader",                        dict(stored=S, current=(5, 0, 6), local=F1, since=3600), "none", None, False),
    ("only 90s of history",                   dict(stored=[(90, L, 0, 0, 0)], current=(5, 0, 0), since=3600), "none", None, False),
]

failed = 0
for name, kw, action, target, called in CASES:
    r, moved = run(**kw)
    ok = r["action"] == action and r.get("target") == target and bool(moved) == called
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name:40s} {r['action']} {r.get('target') or ''} {r.get('reason') or ''}")
sys.exit(1 if failed else 0)
