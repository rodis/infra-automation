#!/usr/bin/env python3
"""Move etcd leadership off a member whose disk is going bad. Run once a minute on every member.

WHY (2026-10-12, infra-objectives task etcd-stacked-on-masters): the storage under every cluster
here freezes, and a freeze on the LEADER makes every write in the cluster wait — up to the 15s
election timeout, longer if the leader is slow but not dead. No etcd version can pin or prioritise
leadership (etcd-io/etcd#10201, never implemented), so this does what etcd's maintainers suggested
there: leave the election alone and transfer leadership afterwards.

The premise was tested on real logs before this was written (agent/stall-predict.py): on a dedicated
volume, fsync stalls come in bursts, and ">= 3 syncs over 1s in the last 2 minutes" preceded ~60% of
a degraded member's >4s stalls, at the cost of a sub-second pause every 1.5-3 hours. On a root disk
it predicts little — this is for multi-member clusters on volumes.

EACH RUN
  1. reads every member's WAL fsync histogram from its own /metrics (etcd's measurement of the
     exact operation that stalls — no synthetic disk writes) and the cluster's leader
  2. keeps a short history in a state file, so "slow syncs in the last N seconds" is a difference
     between two cumulative readings
  3. acts only if THIS member is the leader, it has had >= SLOW_COUNT syncs over 1.024s in the last
     WINDOW seconds, leadership has not changed for COOLDOWN seconds, and some follower had none
     in the last WINDOW and the fewest over QUIET_WINDOW — then `etcdctl move-leader` to it
  4. prints one JSON line per run to the journal (SyslogIdentifier etcd-leader-health)

ARMED=false (the default) logs "would_move" and changes nothing. Arm only after its log has been
compared with the stalls that followed.

A frozen leader cannot process a transfer, which is why this acts on the EARLY signal. Errors
reading a member make it ineligible as a target; errors reading the local member, or the cluster
status, mean no action this run.
"""
import json, os, subprocess, sys, time, urllib.request, ssl

ENV = os.environ.get
ARMED = ENV("ARMED", "false").lower() == "true"
SLOW_COUNT = int(ENV("SLOW_COUNT", "3"))          # syncs over the threshold within WINDOW
WINDOW = int(ENV("WINDOW", "120"))                # seconds
QUIET_WINDOW = int(ENV("QUIET_WINDOW", "600"))    # target choice: fewest slow syncs over this
COOLDOWN = int(ENV("COOLDOWN", "600"))            # no move if leadership changed within this
BUCKET = ENV("BUCKET", "1.024")                   # histogram edge used as "slow" (powers of two)
STATE = ENV("STATE", "/var/lib/etcd-leader-health/state.json")
HOST = ENV("ETCD_HOSTNAME") or os.uname().nodename
CA = "/etc/ssl/etcd/ssl/ca.pem"
CERT = f"/etc/ssl/etcd/ssl/admin-{HOST}.pem"
KEY = f"/etc/ssl/etcd/ssl/admin-{HOST}-key.pem"
ETCDCTL = ENV("ETCDCTL", "/usr/local/bin/etcdctl")
TIMEOUT = 5


def log(**kw):
    print(json.dumps({"host": HOST, "armed": ARMED, **kw}, sort_keys=True), flush=True)


def etcdctl(*args, endpoint="https://127.0.0.1:2379"):
    cmd = [ETCDCTL, f"--endpoints={endpoint}", f"--cacert={CA}", f"--cert={CERT}", f"--key={KEY}",
           f"--command-timeout={TIMEOUT}s", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT * 3)
    if r.returncode != 0:   # etcdctl's own words, not the command line, are what explain a failure
        raise RuntimeError(f"etcdctl {args[0]} exited {r.returncode}: {r.stderr.strip()[-250:]}")
    return r.stdout


def cluster():
    """{member_id_hex: endpoint}, leader_id_hex — from the local member's view of the cluster."""
    rows = json.loads(etcdctl("endpoint", "status", "--cluster", "-w", "json"))
    members = {f'{r["Status"]["header"]["member_id"]:x}': r["Endpoint"] for r in rows}
    leaders = {f'{r["Status"]["leader"]:x}' for r in rows}
    if len(leaders) != 1:
        raise RuntimeError(f"members disagree on the leader: {sorted(leaders)}")
    return members, leaders.pop()


def slow_total(endpoint):
    """Cumulative count of WAL fsyncs slower than BUCKET seconds, and the member's own id."""
    ctx = ssl.create_default_context(cafile=CA)
    ctx.load_cert_chain(CERT, KEY)
    body = urllib.request.urlopen(f"{endpoint}/metrics", context=ctx, timeout=TIMEOUT).read().decode()
    count = under = member = None
    for line in body.splitlines():
        if line.startswith("etcd_disk_wal_fsync_duration_seconds_count "):
            count = float(line.split()[1])
        elif line.startswith(f'etcd_disk_wal_fsync_duration_seconds_bucket{{le="{BUCKET}"}} '):
            under = float(line.split()[1])
        elif line.startswith("etcd_server_id{"):
            member = line.split('server_id="')[1].split('"')[0]
    if count is None or under is None:
        raise RuntimeError(f"no WAL fsync histogram at {endpoint}")
    return int(count - under), member


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"samples": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def delta(samples, member, now, span):
    """Slow syncs for member over the last `span` seconds, or None without enough history."""
    have = [s for s in samples if member in s["slow"]]
    old = [s for s in have if s["ts"] <= now - span]
    if not have or not old:
        return None
    return have[-1]["slow"][member] - old[-1]["slow"][member]


def main():
    now = time.time()
    state = load_state()
    try:
        members, leader = cluster()
    except Exception as e:
        log(action="none", reason=f"cluster status unavailable: {e}"[:300])
        return 0

    slow, errors = {}, {}
    for mid, ep in members.items():
        try:
            total, reported = slow_total(ep)
            if reported != mid:
                raise RuntimeError(f"{ep} reports id {reported}, status said {mid}")
            slow[mid] = total
        except Exception as e:
            errors[mid] = str(e)[:200]

    # When did the current leader take over? Kept in the state rather than inferred from the
    # samples, which are trimmed: a change just before the trimmed window must still count. Unknown
    # (first run, lost state) counts as "just now", so a fresh start waits out one cooldown.
    previous = state["samples"][-1]["leader"] if state["samples"] else None
    if previous != leader or "leader_since" not in state:
        state["leader_since"] = now
    state["samples"] = [s for s in state["samples"] if s["ts"] > now - max(QUIET_WINDOW, COOLDOWN) - 120]
    state["samples"].append({"ts": now, "leader": leader, "slow": slow})
    save_state(state)

    recent = {m: delta(state["samples"], m, now, WINDOW) for m in members}
    quiet = {m: delta(state["samples"], m, now, QUIET_WINDOW) for m in members}
    try:
        _, local = slow_total("https://127.0.0.1:2379")   # the local member's own id
    except Exception as e:
        local = None
        errors["local"] = str(e)[:200]
    base = dict(leader=leader, local=local, recent=recent, quiet=quiet, errors=errors or None)

    if local is None or local not in slow:
        log(action="none", reason="local member not identified or unreadable", **base)
        return 0
    if local != leader:
        log(action="none", reason="not the leader", **base)
        return 0
    if recent[local] is None:
        log(action="none", reason="not enough history yet", **base)
        return 0
    if recent[local] < SLOW_COUNT:
        log(action="none", reason="leader healthy", **base)
        return 0
    if now - state["leader_since"] < COOLDOWN:
        log(action="none", reason=f"leadership changed {int(now - state['leader_since'])}s ago (cooldown {COOLDOWN}s)", **base)
        return 0
    candidates = [m for m in members if m != local and m in slow and recent[m] == 0 and quiet[m] is not None]
    if not candidates:
        log(action="none", reason="leader degraded but no clean follower", **base)
        return 0
    target = min(candidates, key=lambda m: quiet[m])
    if not ARMED:
        log(action="would_move", target=target, **base)
        return 0
    try:
        etcdctl("move-leader", target, endpoint=members[local])
        log(action="moved", target=target, **base)
    except Exception as e:
        log(action="move_failed", target=target, reason=str(e)[:300], **base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
