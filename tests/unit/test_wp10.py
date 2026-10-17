import numpy as np
import pytest

from qs.config import OperatorConfig, SimConfig
from qs.core.actions import ActionManager, ApprovalError
from qs.core.policy import GatewayInfo, PolicyContext, PolicyEngine, safe_to_shift, shift_checks
from qs.eval.arena import SimulatedOperator
from qs.schema import (ActionId as A, Decision, DecisionKind as DK, FeatureRow, ForecastState as FS, Localization,
                       OperatingState as OS, Recommendation, Verdict as V)
from qs.sim.engine import Engine
from qs.sim.tape import Tape

G1 = ("gateway", "G1", "*")
B2 = ("bank", "*", "B2")
R11 = ("route", "G1", "B1")


def gws(**over):
    d = {g: GatewayInfo(arrival_rate=40.0, est_capacity=100.0) for g in ("G1", "G2", "G3")}
    d["G1"] = GatewayInfo(arrival_rate=80.0, est_capacity=100.0, traffic_share=0.34)
    for k, v in over.items():
        setattr(d[k[:2]], k[3:], v)
    return d


def ctx(verdict, sev=OS.CRITICAL, seg=G1, primary=None, **kw):
    loc = Localization(verdict=verdict, primary=primary or seg, confidence=0.8, total_excess=200.0)
    base = dict(incident_id="inc-1", segment=seg, severity=sev, localization=loc, confidence=0.8, gateways=gws(),
                model_versions={"detector": "if-x", "forecaster": "cf-y"}, features_sha256="ab" * 32,
                reasons=["TIMEOUT_UP z=+5.1"], bank_healthy_elsewhere=True, now_ms=100_000)
    base.update(kw)
    return PolicyContext(**base)


def ids(rec, eligible=None):
    return [a.id for a in rec.actions if eligible is None or a.eligible == eligible]


E = PolicyEngine()


def test_rule_degraded_only_hold_and_escalate():                         # SI-4
    rec = E.recommend(ctx(V.GATEWAY_SIDE, degraded=True))
    assert ids(rec) == [A.HOLD_MONITOR, A.ESCALATE_HUMAN] and all(a.eligible for a in rec.actions)


def test_rule_critical_gateway_side():
    rec = E.recommend(ctx(V.GATEWAY_SIDE))
    assert ids(rec) == [A.SHED_EXPIRED, A.THROTTLE_RETRIES, A.VERIFY_PENDING, A.PRIORITIZE_FRESH, A.SHIFT_TRAFFIC]
    sh = rec.actions[-1]
    assert not sh.eligible and "spare" in sh.blocked_by or sh.eligible    # G1 shifts 25 percent of 80: targets keep spare
    rec2 = E.recommend(ctx(V.GATEWAY_SIDE, gateways=gws(**{"G2.forecast": FS.WATCH})))
    sh2 = rec2.actions[-1]
    assert not sh2.eligible and "G2 forecast WATCH" in sh2.blocked_by


def test_rule_critical_bank_side_shift_blocked_shared_bank():
    rec = E.recommend(ctx(V.BANK_SIDE, seg=B2))
    assert ids(rec) == [A.THROTTLE_RETRIES, A.SHED_EXPIRED, A.VERIFY_PENDING, A.PRIORITIZE_FRESH, A.SHIFT_TRAFFIC]
    sh = rec.actions[-1]
    assert not sh.eligible and sh.blocked_by == "shared bank"
    assert rec.actions[0].scope == B2


def test_rule_warning_light_list():
    rec = E.recommend(ctx(V.GATEWAY_SIDE, sev=OS.WARNING))
    assert ids(rec) == [A.VERIFY_PENDING, A.THROTTLE_RETRIES, A.HOLD_MONITOR]
    assert rec.actions[1].params["rate_fraction"] > 0.10                 # light setting


def test_rule_route_specific():
    rec = E.recommend(ctx(V.ROUTE_SPECIFIC, seg=R11))
    assert ids(rec) == [A.THROTTLE_RETRIES, A.VERIFY_PENDING, A.SHIFT_TRAFFIC]
    assert rec.actions[0].scope == R11
    rec2 = E.recommend(ctx(V.ROUTE_SPECIFIC, seg=R11, bank_healthy_elsewhere=False))
    assert not rec2.actions[2].eligible and "bank" in rec2.actions[2].blocked_by


def test_rule_multi_and_platform():
    for v in (V.MULTI_FAULT, V.PLATFORM_WIDE):
        rec = E.recommend(ctx(v))
        assert ids(rec) == [A.THROTTLE_RETRIES, A.VERIFY_PENDING, A.ESCALATE_HUMAN]
        assert rec.actions[0].scope == ("platform", "*", "*")


def test_rule_surge_only_hold():
    rec = E.recommend(ctx(V.INSUFFICIENT_EVIDENCE, sev=OS.WARNING, surge=True))
    assert ids(rec) == [A.HOLD_MONITOR] and "no action" in " ".join(rec.reasons)


def test_insufficient_evidence_not_recommended_but_verify_ok():
    rec = E.recommend(ctx(V.INSUFFICIENT_EVIDENCE, sev=OS.WARNING))
    by = {a.id: a for a in rec.actions}
    assert by[A.VERIFY_PENDING].eligible and by[A.HOLD_MONITOR].eligible
    assert not by[A.THROTTLE_RETRIES].eligible


def test_shift_checks_each_failing():
    base = ctx(V.GATEWAY_SIDE)
    cfg = E.cfg
    assert shift_checks(base, cfg, "G1", 0.25) is None
    assert "spare" in shift_checks(ctx(V.GATEWAY_SIDE, gateways=gws(**{"G2.arrival_rate": 90.0})), cfg, "G1", 0.25)
    assert "open incident" in shift_checks(ctx(V.GATEWAY_SIDE, gateways=gws(**{"G3.open_incident": True})), cfg, "G1", .25)
    assert "5 percent" in shift_checks(ctx(V.GATEWAY_SIDE, gateways=gws(**{"G1.traffic_share": 0.2})), cfg, "G1", 0.25)
    assert "max fraction" in shift_checks(base, cfg, "G1", 0.5)
    assert "10 s" in shift_checks(ctx(V.GATEWAY_SIDE, last_shift_ts=95_000), cfg, "G1", 0.25)
    assert shift_checks(ctx(V.BANK_SIDE), cfg, "G1", 0.25)


def test_property_si2_shift_never_eligible_unless_allowed():
    rng = np.random.default_rng(0)
    verdicts, sevs = list(V), [OS.WARNING, OS.CRITICAL]
    segs = [G1, B2, R11, ("platform", "*", "*")]
    for _ in range(3000):
        g = {n: GatewayInfo(float(rng.uniform(0, 120)), float(rng.uniform(50, 150)), FS(rng.choice([s.value for s in FS])),
                            bool(rng.random() < .2), bool(rng.random() < .9), float(rng.uniform(0, 1)))
             for n in ("G1", "G2", "G3")}
        seg = segs[rng.integers(len(segs))]
        c = ctx(verdicts[rng.integers(len(verdicts))], sev=sevs[rng.integers(2)], seg=seg, gateways=g,
                degraded=bool(rng.random() < .1), surge=bool(rng.random() < .1),
                bank_healthy_elsewhere=bool(rng.random() < .5), last_shift_ts=[None, 95_000, 1_000][int(rng.integers(3))])
        rec = E.recommend(c)
        assert safe_to_shift(rec)
        for a in rec.actions:
            if a.id is A.SHIFT_TRAFFIC and a.eligible:
                assert shift_checks(c, E.cfg, a.params["from_gateway"], a.params["fraction"]) is None
            if c.degraded:
                assert a.id in (A.HOLD_MONITOR, A.ESCALATE_HUMAN) or not a.eligible


def test_record_fields_si6_and_schema_roundtrip():
    rec = E.recommend(ctx(V.GATEWAY_SIDE, p_stuck=0.62, countdown_s=34.0))
    again = Recommendation.model_validate_json(rec.model_dump_json())
    assert again.model_versions == {"detector": "if-x", "forecaster": "cf-y"}
    assert len(again.features_sha256) == 64 and again.reasons and 0 < again.confidence <= 1
    assert again.status == "pending" and all(a.rec_id == again.rec_id for a in again.actions)


# ---------------------------------------------------------------- decisions and rollback
def mgr():
    calls = []
    return ActionManager(lambda a, rb: calls.append((a.id, rb))), calls


def test_si3_nothing_applied_without_decision_and_operator_required():
    m, calls = mgr()
    rec = E.recommend(ctx(V.GATEWAY_SIDE))
    m.add(rec)
    assert calls == [] and len(m.pending()) == 1
    with pytest.raises(ApprovalError):
        m.decide(rec.rec_id, Decision(decision=DK.APPROVE_ALL, operator=""), 1000)
    assert calls == []
    out = m.decide(rec.rec_id, Decision(decision=DK.APPROVE_ALL, operator="alice"), 1000)
    assert {a.id for a in out} == {a.id for a in rec.actions if a.eligible}
    assert len(calls) == len(out) and m.log[0].operator == "alice"
    with pytest.raises(ApprovalError):
        m.decide(rec.rec_id, Decision(decision=DK.APPROVE_ALL, operator="alice"), 2000)   # already decided


def test_blocked_action_cannot_be_approved_and_reject_applies_nothing():
    m, calls = mgr()
    rec = E.recommend(ctx(V.BANK_SIDE, seg=B2))
    m.add(rec)
    with pytest.raises(ApprovalError):
        m.decide(rec.rec_id, Decision(decision=DK.APPROVE_SELECTED, operator="a", actions_selected=["SHIFT_TRAFFIC"]), 1)
    m.decide(rec.rec_id, Decision(decision=DK.REJECT, operator="a"), 2)
    assert calls == [] and rec.status == "rejected"


def test_rollback_proposed_after_clean_ticks_and_needs_approval():
    m, calls = mgr()
    rec = E.recommend(ctx(V.GATEWAY_SIDE))
    m.add(rec)
    m.decide(rec.rec_id, Decision(decision=DK.APPROVE_SELECTED, operator="a", actions_selected=["SHED_EXPIRED"]), 0)
    props = []
    for t in range(1, 7):
        props = m.tick(t * 5000, set())
    assert [p.action.id for p in props] == [A.SHED_EXPIRED] and calls == [(A.SHED_EXPIRED, False)]   # not yet rolled back
    m.approve_rollback(props[0], "a", 40_000)
    assert calls[-1] == (A.SHED_EXPIRED, True)
    # an active segment resets the count
    m2, _ = mgr()
    rec2 = E.recommend(ctx(V.GATEWAY_SIDE))
    m2.add(rec2)
    m2.decide(rec2.rec_id, Decision(decision=DK.APPROVE_SELECTED, operator="a", actions_selected=["SHED_EXPIRED"]), 0)
    for t in range(1, 5):
        m2.tick(t * 5000, set())
    m2.tick(25_000, {G1})
    assert m2.tick(30_000, set()) == []


def test_every_recommended_action_applies_in_the_simulator():
    cfg = SimConfig()
    eng = Engine(cfg, Tape(1, cfg, horizon_ms=20_000), run_id="t", horizon_ms=20_000)
    eng.run_until(5000)
    m = ActionManager(lambda a, rb: eng.apply_action(a, rb))
    for c in (ctx(V.GATEWAY_SIDE, gateways=gws()), ctx(V.BANK_SIDE, seg=B2), ctx(V.ROUTE_SPECIFIC, seg=R11),
              ctx(V.PLATFORM_WIDE)):
        rec = E.recommend(c)
        m.add(rec)
        m.decide(rec.rec_id, Decision(decision=DK.APPROVE_ALL, operator="t"), 5000)
    for ap in m.applied:
        m.approve_rollback(ap, "t", 6000)
    eng.run_until(8000)


# ---------------------------------------------------------------- simulated operator
def test_operator_delay_and_confidence_and_blocked():
    op = SimulatedOperator(OperatorConfig(approval_delay_ms=20_000, min_confidence=0.6))
    rec = E.recommend(ctx(V.BANK_SIDE, seg=B2))
    assert op.step(0, [rec]) == [] and op.step(15_000, [rec]) == []
    (rid, d), = op.step(20_000, [rec])
    assert d.decision is DK.APPROVE_SELECTED and "SHIFT_TRAFFIC" not in d.actions_selected
    low = E.recommend(ctx(V.GATEWAY_SIDE, confidence=0.4))
    op2 = SimulatedOperator(OperatorConfig(approval_delay_ms=10_000))
    assert op2.step(0, [low]) == [] and op2.step(10_000, [low]) == [] and op2.step(15_000, [low]) == []
    low.confidence = 0.7                                          # refreshed recommendation clears the bar
    assert len(op2.step(20_000, [low])) == 1
