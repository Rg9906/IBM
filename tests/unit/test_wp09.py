import numpy as np

from qs.core.localizer import FaultLocalizer, eligibility
from qs.schema import ActionId, Verdict as V

G, B = 3, 5


def mats(hot=None, hot_extra=80.0, rng=None, base=20.0, noise=False):
    exp = np.full((G, B), 10.0)
    bad = exp.copy()
    if rng is not None and noise:
        bad = rng.poisson(exp).astype(float)
    if hot == "row":
        bad[1, :] += hot_extra
    elif hot == "col":
        bad[:, 2] += hot_extra
    elif hot == "cell":
        bad[0, 3] += hot_extra * 3
    return bad, exp


def test_single_hot_row_column_cell_flat():
    loc = FaultLocalizer()
    assert loc.raw(*mats("row")).verdict is V.GATEWAY_SIDE
    assert loc.raw(*mats("row")).primary == ("gateway", "G2", "*")
    assert loc.raw(*mats("col")).verdict is V.BANK_SIDE
    assert loc.raw(*mats("col")).primary == ("bank", "*", "B3")
    r = loc.raw(*mats("cell"))
    assert r.verdict is V.ROUTE_SPECIFIC and r.primary == ("route", "G1", "B4")
    bad, exp = mats()
    bad = bad * 1.8                        # flat matrix: excess spread in line with traffic
    assert loc.raw(bad, exp).verdict is V.PLATFORM_WIDE


def test_insufficient_and_multi():
    loc = FaultLocalizer()
    bad, exp = mats()
    assert loc.raw(bad + 0.1, exp).verdict is V.INSUFFICIENT_EVIDENCE
    bad2, exp2 = mats("row")
    bad2[:, 4] += 200.0                    # a second, independent fault in a different dimension
    assert loc.raw(bad2, exp2).verdict is V.MULTI_FAULT
    bad3, exp3 = mats("row")
    bad3[2, :] += 80.0
    assert loc.raw(bad3, exp3).verdict is V.MULTI_FAULT


def test_shares_sum_to_one():
    loc = FaultLocalizer()
    for kind in ("row", "col", "cell"):
        bad, exp = mats(kind)
        excess = np.maximum(0, bad - exp)
        t = excess.sum()
        assert abs((excess.sum(axis=1) / t).sum() - 1) < 1e-9 and abs((excess.sum(axis=0) / t).sum() - 1) < 1e-9
        r = loc.raw(bad, exp)
        assert r.total_excess == t and 0 < r.confidence <= 1


def test_noise_robustness_hot_row():
    rng = np.random.default_rng(0)
    loc = FaultLocalizer()
    ok = 0
    for _ in range(1000):
        bad, exp = mats("row", rng=rng, noise=True)
        ok += loc.raw(bad, exp).verdict is V.GATEWAY_SIDE
    assert ok >= 950


def test_hysteresis():
    loc = FaultLocalizer()
    row, col = mats("row"), mats("col")
    assert loc.localize(*row).verdict is V.GATEWAY_SIDE
    assert loc.localize(*col).verdict is V.GATEWAY_SIDE          # one contradicting tick
    assert loc.localize(*row).verdict is V.GATEWAY_SIDE
    assert loc.localize(*col).verdict is V.GATEWAY_SIDE
    r = loc.localize(*col)                                       # second consecutive: switch
    assert r.verdict is V.BANK_SIDE and r.primary == ("bank", "*", "B3")
    quiet = mats()
    assert loc.localize(quiet[0] + 0.1, quiet[1]).verdict is V.INSUFFICIENT_EVIDENCE


def test_eligibility_table():
    assert eligibility(V.BANK_SIDE)[ActionId.SHIFT_TRAFFIC] == "blocked"
    assert eligibility(V.MULTI_FAULT)[ActionId.SHIFT_TRAFFIC] == "blocked"
    assert eligibility(V.GATEWAY_SIDE)[ActionId.SHIFT_TRAFFIC].startswith("conditional")
    assert eligibility(V.INSUFFICIENT_EVIDENCE)[ActionId.THROTTLE_RETRIES] == "not_recommended"
    assert eligibility(V.INSUFFICIENT_EVIDENCE)[ActionId.VERIFY_PENDING] == "eligible"
    for v in V:
        assert eligibility(v)[ActionId.HOLD_MONITOR] == eligibility(v)[ActionId.ESCALATE_HUMAN] == "eligible"


def test_runtime_under_1ms():
    import time
    loc = FaultLocalizer()
    bad, exp = mats("row")
    t0 = time.perf_counter()
    for _ in range(200):
        loc.raw(bad, exp)
    assert (time.perf_counter() - t0) / 200 < 1e-3
