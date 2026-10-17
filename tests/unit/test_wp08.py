import json
import math
import time
from pathlib import Path

import numpy as np
import pytest

from qs.core.features import FEATURES as FE
from qs.forecast.dataset import SEED_BASE, draw_config
from qs.forecast.grid import StuckGrid
from qs.forecast.model import CascadeForecaster, state_of
from qs.schema import FeatureRow, ForecastState

DATA = Path("artifacts/data")


def test_configs_disjoint_across_sets():
    ids = {s: {draw_config(s, i)["config_id"] for i in range(300)} for s in "ABC"}
    seeds = {s: {draw_config(s, i)["seed"] for i in range(300)} for s in "ABC"}
    assert not (ids["A"] & ids["B"] or ids["A"] & ids["C"] or ids["B"] & ids["C"])
    assert not (seeds["A"] & seeds["B"] or seeds["A"] & seeds["C"] or seeds["B"] & seeds["C"])
    assert draw_config("A", 3) == draw_config("A", 3)
    assert all(0.3 <= draw_config("A", i)["m"] <= 0.6 for i in range(50))


@pytest.mark.skipif(not (DATA / "cascade_C.json").exists(), reason="dataset not generated")
def test_generated_split_integrity():
    sets = {s: json.loads((DATA / f"cascade_{s}.json").read_text()) for s in "ABC"}
    ids = {s: {r["config"]["config_id"] for r in runs} for s, runs in sets.items()}
    assert not (ids["A"] & ids["B"]) and not (ids["A"] & ids["C"]) and not (ids["B"] & ids["C"])
    for s, runs in sets.items():
        assert len({r["config"]["config_id"] for r in runs}) == len(runs)
        assert all(r["config"]["seed"] >= SEED_BASE[s] for r in runs)


def _synthetic(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    d, o = rng.uniform(0, 2, n), rng.uniform(0, 4, n)
    y = (rng.random(n) < 1 / (1 + np.exp(-(3 * d + 1.5 * o - 4)))).astype(int)
    return d, o, y


def test_grid_monotone_after_smoothing():
    d, o, y = _synthetic()
    g = StuckGrid().fit(d, o, y, monotone=True)
    assert (np.diff(g.p, axis=0) >= -1e-12).all() and (np.diff(g.p, axis=1) >= -1e-12).all()
    assert g.predict(1.9, 3.9)[0] > g.predict(0.1, 0.1)[0]


def test_grid_roundtrip_and_speed():
    d, o, y = _synthetic()
    g = StuckGrid().fit(d, o, y, monotone=True)
    g2 = StuckGrid.from_dict(json.loads(json.dumps(g.to_dict())))
    assert np.allclose(g.p, g2.p)
    t0 = time.perf_counter()
    for _ in range(1000):
        g.predict(np.array([0.5, 1.0, 1.5]), np.array([0.2, 1.0, 3.0]))
    assert (time.perf_counter() - t0) / 1000 < 1e-3


def mkrow(seg, delay, orbit, tick, qd=100.0):
    f = {k: 0.0 for k in FE}
    f.update(queue_depth=qd, queue_delay_est=delay * 3.0, orbit_proxy=orbit * 100.0, load_over_capacity=1.0)
    return FeatureRow(tick_ts=tick, segment=seg, eligible=True, f=f, z={}, base={})


def test_forecaster_predict_uses_only_observables_and_smooths():
    d, o, y = _synthetic()
    fc = CascadeForecaster()
    fc.grid = StuckGrid().fit(d, o, y)
    seg = ("gateway", "G1", "*")
    p1 = fc.predict([mkrow(seg, 0.1, 0.1, 5000), mkrow(("bank", "*", "B1"), 1.9, 3.9, 5000)])
    assert set(p1) == {seg}                      # gateway segments only
    p2 = fc.predict([mkrow(seg, 1.9, 3.9, 10000)])
    raw = fc.grid.predict(1.9, 3.9)[0]
    assert p1[seg] < p2[seg] < raw + 1e-9        # EMA lags a jump
    assert fc.phase_point(mkrow(seg, 0.5, 0.25, 0)) == (pytest.approx(0.5), pytest.approx(0.25 * 100 / fc.cap))


def test_states_and_countdown():
    assert state_of(0.1) == ForecastState.CALM and state_of(0.4) == ForecastState.WATCH
    assert state_of(0.5) == ForecastState.CRITICAL
    fc = CascadeForecaster()
    fc.grid = StuckGrid()
    seg = ("gateway", "G1", "*")
    for i, dl in enumerate([0.2, 0.3, 0.4, 0.5]):
        fc.predict([mkrow(seg, dl, 0.0, 5000 * (i + 1))])
    cd = fc.countdown_s(seg)
    assert cd is not None and math.isclose(cd, 25.0, rel_tol=0.05)       # +0.1 per 5 s, 0.5 left
