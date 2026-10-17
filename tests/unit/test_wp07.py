import numpy as np
import pytest

from qs.config import GovernorConfig
from qs.core.governor import UNREACHABLE, VerificationGovernor
from qs.schema import PaymentState as PS

STATE = {"G1": {"spare_capacity": 50.0}, "G2": {"spare_capacity": 50.0}}


@pytest.mark.parametrize("seed", range(8))
def test_property_cap_and_rate(seed):
    cfg = GovernorConfig(horizon_ms=10_000_000)
    g = VerificationGovernor(cfg, seed=seed)
    rng = np.random.default_rng(seed)
    n = 300
    for i in range(n):
        g.on_timeout(f"p{i}", "G1" if i % 2 else "G2", int(rng.integers(0, 20_000)))
    checking = {}
    for t in range(0, 300_000, 1000):
        for pid, ts in list(checking.items()):
            if ts <= t:
                r = rng.random()
                st = ("CONFIRMED_SUCCESS" if r < .15 else "CONFIRMED_FAILED" if r < .3
                      else UNREACHABLE if r < .35 else "UNKNOWN")
                g.on_status_result(pid, st, t)
                del checking[pid]
        for pid in g.due_checks(t, STATE):
            checking[pid] = t + 500
            assert g.checks_of(pid) <= cfg.max_checks_per_payment
    assert all(g.checks_of(f"p{i}") <= cfg.max_checks_per_payment for i in range(n))
    # allowance: 5 per second (10 percent of 50 spare) plus the 2 s burst, per gateway
    for gw in ("G1", "G2"):
        assert sum(1 for _, x in g.sent_log if x == gw) <= 5.0 * 300 + 10.0 + 1
        for t0 in range(0, 290_000, 10_000):
            w = sum(1 for ts, x in g.sent_log if x == gw and t0 <= ts < t0 + 10_000)
            assert w <= 5.0 * 10 + 10.0


def test_can_retry_gating():
    g = VerificationGovernor(GovernorConfig(), seed=1)
    assert g.can_retry("never-seen")
    g.on_timeout("a", "G1", 0)
    assert not g.can_retry("a") and g.state_of("a") == PS.PENDING_UNKNOWN
    assert g.due_checks(20_000, STATE) == ["a"]
    assert g.state_of("a") == PS.STATUS_CHECKING and not g.can_retry("a")
    g.on_status_result("a", "CONFIRMED_FAILED", 20_100)
    assert g.can_retry("a")                                    # R2
    g.on_timeout("b", "G1", 0)
    g.due_checks(20_000, STATE)
    g.on_status_result("b", "CONFIRMED_SUCCESS", 20_100)
    assert not g.can_retry("b")                                # R3
    g.on_timeout("c", "G1", 0)
    for t in range(15_000, 100_000, 15_000):
        for pid in g.due_checks(t, STATE):
            g.on_status_result(pid, "UNKNOWN", t + 10)
    assert g.state_of("c") == PS.ESCALATED_RECONCILE and not g.can_retry("c") and g.checks_of("c") == 3  # R4


def test_first_check_not_before_delay_and_backoff_grows():
    cfg = GovernorConfig()
    g = VerificationGovernor(cfg, seed=3)
    g.on_timeout("a", "G1", 0)
    assert g.due_checks(int(cfg.first_check_delay_ms * 0.79), STATE) == []
    assert g.due_checks(int(cfg.first_check_delay_ms * 1.21), STATE) == ["a"]
    g.on_status_result("a", "UNKNOWN", 12_100)
    assert g.due_checks(12_100 + 8_000, STATE) == []           # next gap is about 10 s (min 8 s)... not yet due
    assert g.due_checks(12_100 + 12_100, STATE) == ["a"]


def test_budget_limits_release_oldest_first():
    g = VerificationGovernor(GovernorConfig(), seed=2)
    for i in range(100):
        g.on_timeout(f"p{i:03d}", "G1", i)
    out = g.due_checks(30_000, {"G1": {"spare_capacity": 10.0}})     # 10% of 10 clamps to 1/s, burst 2
    assert 1 <= len(out) <= 2


def test_breaker_pauses_checks_and_never_loses_payment():
    cfg = GovernorConfig(horizon_ms=10_000_000)
    g = VerificationGovernor(cfg, seed=4)
    g.on_timeout("a", "G1", 0)
    pid = g.due_checks(15_000, STATE)[0]
    g.on_status_result(pid, UNREACHABLE, 15_000)
    assert g.stats()["breakers"]["G1"] == 15_000 + cfg.breaker_open_ms
    g.on_timeout("b", "G1", 0)
    assert g.due_checks(16_000, STATE) == []                    # breaker open: b is rescheduled and counted
    assert g.checks_of("b") == 1
    for t in range(16_000, 400_000, 2000):
        for p in g.due_checks(t, STATE):
            g.on_status_result(p, UNREACHABLE, t)
    for x in ("a", "b"):
        assert g.state_of(x) == PS.ESCALATED_RECONCILE and g.checks_of(x) == 3
    assert set(g.reconcile) == {"a", "b"}
