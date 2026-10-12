# etcd_leader_health

Once a minute on every etcd member: if **this** member is the leader and its WAL fsyncs have turned
slow (>= 3 over 1.024s in 2 min), hand leadership to the follower with the fewest slow fsyncs in the
last 10 min (`etcdctl move-leader`). 10-min cooldown after any leadership change. Needs >= 3 members.

Why, and the evidence it rests on: infra-objectives task `etcd-stacked-on-masters`;
`agent/stall-predict.py` and `state/evidence/stacked-etcd-soak-2026-10-11/stall-predict-2026-10-12/`.

**Ships log-only** (`etcd_leader_health_armed: false`): it prints `"action": "would_move"` and moves
nothing. Arm only after its journal has been compared with the stalls that followed.

Read what it decided:

    journalctl -t etcd-leader-health --since -1h -o cat | grep -v '"not the leader"'

Actions: `none` (with a reason), `would_move`, `moved`, `move_failed`.

Test the decision logic offline (no etcd needed): `python3 roles/etcd_leader_health/tests/test_decisions.py`

Not covered: a FOLLOWER's stall (API servers reading from it) — that is the local-only-endpoints
question, separate from this.
