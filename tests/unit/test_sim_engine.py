import hashlib
import json
from collections import Counter

import pytest

from qs.config import SimConfig
from qs.schema import Action, ActionId, Event, EventType, Status
from qs.sim.engine import Engine
from qs.sim.entities import Modifier
from qs.sim.tape import Tape

CFG = SimConfig()
SMALLQ = SimConfig(queue_max=300)


def run(seed=1, horizon=120_000, extra=15_000, mix=None, mods=None, cfg=CFG, hook=None):
    tape = Tape(seed, cfg, horizon_ms=horizon)
    eng = Engine(cfg, tape, run_id=f"t{seed}", profile_mix=mix, modifiers=mods, horizon_ms=horizon + extra)
    if hook:
        hook(eng)
    eng.run_until(horizon + extra)
    return eng, eng.drain()


def stream_hash(events):
    h = hashlib.sha256()
    for e in events:
        h.update(json.dumps(e.to_json_dict(), sort_keys=True).encode())
    return h.hexdigest()


def test_determinism():
    _, a = run(seed=3)
    _, b = run(seed=3)
    _, c = run(seed=4)
    assert len(a) > 1000
    assert stream_hash(a) == stream_hash(b)
    assert stream_hash(a) != stream_hash(c)


def test_events_are_valid_and_ordered():
    _, evs = run(horizon=30_000)
    ts = [e.event_timestamp for e in evs]
    assert ts == sorted(ts)
    for e in evs[:500]:
        Event.from_json_dict(e.to_json_dict())   # full validation of the unvalidated fast path
    assert len({e.event_id for e in evs}) == len(evs)


def test_one_outcome_per_attempt():
    eng, evs = run()
    sent = {e.attempt_id for e in evs if e.event_type is EventType.ATTEMPT_SENT}
    outcomes = Counter(e.attempt_id for e in evs if e.event_type is EventType.ATTEMPT_OUTCOME)
    assert sent and set(outcomes) == sent
    assert all(v == 1 for v in outcomes.values())
    c = eng.attempt_outcome_counts()
    assert c["open"] == 0


def test_normal_run_littles_law_and_utilisation():
    eng, _ = run(seed=7, horizon=300_000, extra=0)
    gt = eng.ground_truth_stats()
    for g in gt["per_gateway"]:
        assert 0.50 <= g["utilisation"] <= 0.60, g
        lam = g["arrival_rate"]
        lq = g["mean_queue"]
        lw = lam * g["mean_wait_ms"] / 1000.0
        assert lq == pytest.approx(lw, rel=0.05, abs=1e-3), g


def test_no_ground_truth_leaks_into_events():
    _, evs = run(horizon=20_000)
    for e in evs[:200]:
        keys = set(e.to_json_dict())
        assert not keys & {"committed", "commit", "duplicate", "is_incident", "scenario"}


def test_timeout_does_not_cancel_and_duplicates_counted():
    """A tiny timeout makes attempts time out while the gateway still commits them (wasted work, duplicates)."""
    cfg = SimConfig(client_timeout_ms=150)
    eng, evs = run(cfg=cfg, horizon=40_000)
    st = Counter(e.status for e in evs if e.event_type is EventType.ATTEMPT_OUTCOME)
    assert st[Status.CLIENT_TIMEOUT] > 0
    gt = eng.ground_truth_stats()
    assert gt["late_completions"] > 0
    assert gt["duplicate_commits"] > 0


def test_bounded_queue_rejects_under_overload():
    mods = [Modifier("capacity", 10_000, 60_000, v0=0.2, gateway="G1")]
    eng, evs = run(mods=mods, horizon=60_000, cfg=SMALLQ)
    rej = [e for e in evs if e.status is Status.REJECTED_OVERLOAD and e.error_code == "E_OVERLOAD"]
    assert rej and all(e.gateway_id == "G1" for e in rej)
    assert max(e.queue_depth for e in evs if e.queue_depth is not None) <= SMALLQ.queue_max


def test_controls_shed_throttle_and_shift():
    mods = [Modifier("capacity", 5_000, 50_000, v0=0.35, gateway="G1")]
    base_eng, base = run(mods=mods, horizon=50_000)

    def act(eng):
        eng.run_until(20_000)
        eng.apply_action(Action(id=ActionId.SHED_EXPIRED, scope=("gateway", "G1", "*")))
        eng.apply_action(Action(id=ActionId.THROTTLE_RETRIES, scope=("gateway", "G1", "*"),
                                params={"rate_fraction": 0.1, "retry_after_ms": 2000}))
        eng.apply_action(Action(id=ActionId.SHIFT_TRAFFIC, params={"from_gateway": "G1", "fraction": 0.25, "ramp_ms": 1000}))

    ctl_eng, ctl = run(mods=mods, horizon=50_000, hook=act)
    assert any(e.event_type is EventType.ACTION_APPLIED for e in ctl)
    assert any(e.error_code == "E_RETRY_LATER" for e in ctl)
    q_base = max(e.queue_depth for e in base if e.gateway_id == "G1" and e.queue_depth is not None)
    g1_sent_after = lambda evs: sum(1 for e in evs if e.event_type is EventType.ATTEMPT_SENT
                                    and e.gateway_id == "G1" and e.event_timestamp > 30_000)
    assert g1_sent_after(ctl) < g1_sent_after(base)
    assert q_base > 0


def test_status_service_and_naive_verify():
    mods = [Modifier("blackhole", 20_000, 40_000, v0=0.4, bank="B1", resolve_ms=70_000)]
    eng, evs = run(mix={"naive_verify": 1.0}, mods=mods, horizon=60_000, extra=60_000)
    sent = [e for e in evs if e.event_type is EventType.STATUS_CHECK_SENT]
    res = [e for e in evs if e.event_type is EventType.STATUS_CHECK_RESULT]
    assert sent and res
    assert {e.status for e in res} <= {Status.CONFIRMED_SUCCESS, Status.CONFIRMED_FAILED, Status.UNKNOWN}
    assert any(e.status is Status.UNKNOWN for e in res)


def test_disciplined_uses_idempotency_keys():
    _, evs = run(mix={"disciplined": 1.0}, horizon=10_000)
    assert all(e.idempotency_key for e in evs if e.event_type is EventType.ATTEMPT_SENT)
