import math

import numpy as np

from qs.config import DetectorConfig
from qs.core.detectors import rules
from qs.core.detectors.persistence import Persistence, QueueShield
from qs.core.detectors.threshold import PlatformThreshold, SegmentThresholds
from qs.core.features import FEATURES
from qs.schema import FeatureRow

CFG = DetectorConfig()
SEG = ("gateway", "G1")
NAN = math.nan


def row(f=None, z=None, base=None, seg=SEG, tick=5000, eligible=True):
    ff = {k: 0.0 for k in FEATURES}
    ff.update(arrival_rate=50.0, success_ratio=0.98, queue_delay_est=0.1)
    ff.update(f or {})
    zz = {k: 0.0 for k in FEATURES}
    zz.update(z or {})
    bb = {k: 0.0 for k in FEATURES}
    bb.update(timeout_ratio=0.0, raf=1.0, reject_ratio=0.0, queue_delay_est=0.1)
    bb.update(base or {})
    return FeatureRow(tick_ts=tick, segment=seg, eligible=eligible, f=ff, z=zz, base=bb)


class StubModel:
    """Scores are scripted per call so persistence can be tested without a fitted forest."""

    def __init__(self, scores):
        self.scores = list(scores)

    def score(self, X):
        return np.array([self.scores.pop(0)])


# ---------------------------------------------------------------- signals
def test_each_signal_fires_and_stays_silent():
    assert rules.signals(row(), CFG) == []
    assert rules.signals(row(z={"latency_ratio": 2.6}), CFG) == ["S_lat"]
    assert rules.signals(row(z={"latency_ratio": 2.4}), CFG) == []
    assert rules.signals(row(f={"timeout_ratio": 0.05}, z={"timeout_ratio": 3.5}), CFG) == ["S_to"]
    # z is high but the absolute rise is under 0.02
    assert rules.signals(row(f={"timeout_ratio": 0.01}, z={"timeout_ratio": 9.0}), CFG) == []
    assert rules.signals(row(f={"queue_growth": 5.0, "queue_delay_est": 0.5}), CFG) == ["S_q"]
    # queue delay is high but the queue is not growing
    assert rules.signals(row(f={"queue_growth": 0.0, "queue_delay_est": 0.5}), CFG) == []
    assert rules.signals(row(f={"raf": 1.3}), CFG) == ["S_raf"]
    assert rules.signals(row(f={"raf": 1.2}), CFG) == []
    assert rules.signals(row(f={"reject_ratio": 0.03}, z={"reject_ratio": 4.0}), CFG) == ["S_rej"]
    assert rules.signals(row(f={"reject_ratio": 0.005}, z={"reject_ratio": 4.0}), CFG) == []


def test_signals_ignore_nan():
    r = row(f={"raf": NAN, "queue_growth": NAN}, z={"latency_ratio": NAN, "timeout_ratio": NAN})
    assert rules.signals(r, CFG) == []


# ---------------------------------------------------------------- surge gate
def test_surge_gate_needs_two_non_growing_ticks():
    g = rules.SurgeGate(CFG)
    surge = row(z={"arrival_rate": 3.0})
    assert g.label(surge, 0) is None          # first tick: no previous non-growing tick yet
    assert g.label(surge, 1) == "SURGE"
    assert g.label(surge, 2) is None          # two corroborating signals break the gate


def test_surge_gate_not_when_queue_grows():
    g = rules.SurgeGate(CFG)
    growing = row(f={"queue_growth": 10.0}, z={"arrival_rate": 3.0})
    assert g.label(growing, 0) is None
    assert g.label(growing, 0) is None
    assert g.label(row(z={"arrival_rate": 1.0}), 0) is None   # no arrival jump


# ---------------------------------------------------------------- persistence
def test_persistence_single_noisy_tick_never_warns():
    p = Persistence(3, 4)
    assert [p.push(h) for h in (False, True, False, False, False, True, False)] == [False] * 7
    assert p.push(True) is False
    assert p.push(True) is True              # 3 of the last 4


def test_single_noisy_tick_raises_no_alert():
    sig_row = row(z={"latency_ratio": 3.0}, f={"raf": 1.5})     # two signals true
    qs = QueueShield(CFG, model=StubModel([0.999] + [0.0] * 20))
    alerts = []
    alerts += qs.update(5000, [sig_row])
    for t in range(10000, 60000, 5000):
        alerts += qs.update(t, [row(tick=t)])
    assert alerts == []


def test_three_of_four_with_signals_warns_once():
    sig_row = row(z={"latency_ratio": 3.0}, f={"raf": 1.5})
    qs = QueueShield(CFG, model=StubModel([0.995, 0.995, 0.995, 0.995, 0.995]))
    alerts = []
    for t in (5000, 10000, 15000, 20000, 25000):
        alerts += qs.update(t, [sig_row])
    assert len(alerts) == 1 and alerts[0].ts == 15000
    assert set(alerts[0].signals) == {"S_lat", "S_raf"}


def test_high_score_without_two_signals_does_not_warn():
    one = row(z={"latency_ratio": 3.0})
    qs = QueueShield(CFG, model=StubModel([0.9999] * 6))
    assert sum((qs.update(t, [one]) for t in range(5000, 35000, 5000)), []) == []


def test_ineligible_segment_skipped():
    qs = QueueShield(CFG, mode="R")
    assert qs.update(5000, [row(z={"latency_ratio": 3.0}, f={"raf": 1.5}, eligible=False)]) == []


def test_rules_only_arm_runs_without_model():
    sig_row = row(z={"latency_ratio": 3.0}, f={"raf": 1.5})
    qs = QueueShield(CFG, mode="R")
    got = []
    for t in (5000, 10000, 15000):
        got += qs.update(t, [sig_row])
    assert len(got) == 1 and got[0].detector == "D3:R" and got[0].score is None


def test_model_nan_falls_back_to_rules_and_flags_degraded():
    sig_row = row(z={"latency_ratio": 3.0}, f={"raf": 1.5}, tick=5000)
    # arrival log-ratio, latency, queue-growth ratio and pending_frac are NaN: over half the inputs, so no vector
    for k in ("arrival_rate", "latency_ratio", "load_over_capacity", "pending_frac", "queue_delay_est"):
        sig_row.f[k] = NAN
    qs = QueueShield(CFG, model=StubModel([]))
    got = []
    for t in (5000, 10000, 15000):
        got += qs.update(t, [sig_row])
    assert SEG in qs.model_degraded
    assert len(got) == 1


# ---------------------------------------------------------------- D0 and D1
def test_d0_needs_two_consecutive_ticks():
    d0 = PlatformThreshold(CFG)
    plat = ("platform", "all")
    bad = row(f={"success_ratio": 0.5}, seg=plat)
    ok = row(seg=plat)
    assert d0.update(5000, [bad]) == []
    assert d0.update(10000, [ok]) == []
    assert d0.update(15000, [bad]) == []
    assert len(d0.update(20000, [bad])) == 1


def test_d1_needs_two_consecutive_ticks_and_skips_low_volume():
    d1 = SegmentThresholds(CFG)
    bad = row(f={"timeout_ratio": 0.2})
    assert d1.update(5000, [bad]) == []
    assert len(d1.update(10000, [bad])) == 1
    assert d1.update(15000, [bad]) == []      # already active, no re-alert
    low = row(f={"timeout_ratio": 0.9})
    low.flags.append("low_volume")
    d1b = SegmentThresholds(CFG)
    assert d1b.update(5000, [low]) == [] and d1b.update(10000, [low]) == []
