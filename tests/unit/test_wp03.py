import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from qs.config import SimConfig
from qs.schema import EventType
from qs.sim import scenarios
from qs.sim.engine import Engine
from qs.sim.run import run_scenario
from qs.sim.scenarios import ScenarioParams
from qs.sim.tape import Tape

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("make_configs", ROOT / "scripts" / "make_configs.py")
make_configs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_configs)

CFG = SimConfig()


def test_tape_is_identical_across_universes_for_one_seed():
    """9.5: first 1000 arrival times and bank draws identical in every universe for the same seed."""
    p = ScenarioParams("S3", bank="B2", onset_ms=60_000, duration_ms=30_000, magnitude=5.0)
    sink_a, sink_b = [], []
    tapes = []
    for arm, sink in (("U0", sink_a), ("U3", sink_b)):
        spec = scenarios.build(p)
        tp = Tape(11, scenarios.effective_cfg(CFG, p), horizon_ms=120_000, rate_fn=spec.rate_fn,
                  rate_max_mult=spec.rate_max_mult)
        tapes.append(tp)
        run_scenario("S3", 11, arm, CFG, sink=sink.extend, params=p, horizon_ms=40_000)
    a, b = tapes
    assert len(a.arrivals) >= 1000
    assert np.array_equal(a.arrivals[:1000], b.arrivals[:1000])
    assert np.array_equal(a.bank_idx[:1000], b.bank_idx[:1000])
    # the same draws drive the same fresh arrivals in the emitted streams of both universes
    fa = [e.event_timestamp for e in sink_a if e.event_type is EventType.PAYMENT_INITIATED][:300]
    fb = [e.event_timestamp for e in sink_b if e.event_type is EventType.PAYMENT_INITIATED][:300]
    assert fa == fb


def test_surge_arrivals_follow_rate_function():
    p = ScenarioParams("S1", onset_ms=20_000)
    spec = scenarios.build(p)
    tp = Tape(5, CFG, horizon_ms=120_000, rate_fn=spec.rate_fn, rate_max_mult=spec.rate_max_mult)
    pre = np.sum(tp.arrivals < 20_000) / 20.0
    during = np.sum((tp.arrivals >= 20_000) & (tp.arrivals < 90_000)) / 70.0
    assert during / pre == pytest.approx(1.6, rel=0.15)


@pytest.mark.parametrize("sc,level,incident", [
    ("S0", None, None), ("S1", "platform", False), ("S2", "gateway", True), ("S3", "bank", True),
    ("S4", "gateway", True), ("S6", "bank", True), ("S7", "route", True)])
def test_ground_truth_entries(sc, level, incident, tmp_path):
    p = ScenarioParams(sc, gateway="G2", bank="B3", onset_ms=30_000, duration_ms=20_000,
                       magnitude={"S4": 0.6, "S7": 2.0}.get(sc, 3.0))
    res = run_scenario(sc, 2, "U0", CFG, params=p, horizon_ms=15_000, out_dir=str(tmp_path))
    gt = json.loads((tmp_path / "ground_truth.json").read_text())
    assert gt == res.ground_truth
    if level is None:
        assert gt == []
        return
    e = gt[0]
    assert set(e) == {"incident_id", "onset_ms", "end_ms", "level", "gateway_id", "bank_id", "kind", "magnitude",
                      "is_incident"}
    assert e["level"] == level and e["is_incident"] is incident


def test_s5_has_surge_and_hidden_incident_and_s7_onset():
    s5 = scenarios.build(ScenarioParams("S5", gateway="G1", onset_ms=100_000))
    assert [t["is_incident"] for t in s5.truth] == [False, True]
    s7 = scenarios.build(ScenarioParams("S7", gateway="G1", bank="B1", onset_ms=100_000, magnitude=2.2))
    assert s7.truth[0]["onset_ms"] == 100_000 + 30_000   # x1.3 reached at 0.3/1.2 of the ramp


def test_events_carry_no_ground_truth():
    p = ScenarioParams("S2", gateway="G1", onset_ms=5_000, duration_ms=5_000, magnitude=2.0)
    sink = []
    run_scenario("S2", 1, "U0", CFG, sink=sink.extend, params=p, horizon_ms=10_000)
    for e in sink[:300]:
        assert not (set(e.to_json_dict()) & {"scenario", "is_incident", "committed", "kind", "magnitude"})


def test_config_sets_disjoint_and_reproducible():
    a = make_configs.make_configs("A", 5, 0)
    b = make_configs.make_configs("B", 5, 0)
    c = make_configs.make_configs("C", 5, 0)
    assert a == make_configs.make_configs("A", 5, 0)
    key = lambda d: tuple((k, v) for k, v in sorted(d.items()) if k not in ("config_id", "seed", "set"))
    ka, kb, kc = ({key(d) for d in s} for s in (a, b, c))
    assert not ka & kb and not ka & kc and not kb & kc
    seeds = [d["seed"] for s in (a, b, c) for d in s]
    assert len(seeds) == len(set(seeds))
    assert all(1 <= d["seed"] <= 999 for d in a) and all(1000 <= d["seed"] <= 1999 for d in b) \
        and all(2000 <= d["seed"] <= 2999 for d in c)
    # things that exist only in set C
    big_c = make_configs.make_configs("C", 100, 0)
    big_a = make_configs.make_configs("A", 100, 0)
    assert any(d["utilisation"] == 0.65 for d in big_c) and not any(d["utilisation"] == 0.65 for d in big_a)
    assert any(d["client_timeout_ms"] == 2500 for d in big_c) and not any(d["client_timeout_ms"] == 2500 for d in big_a)
    assert any(d["gateway"] == "G3" for d in big_c) and not any(d["gateway"] == "G3" for d in big_a)
    for d in a + b + c:
        ScenarioParams.from_dict({k: v for k, v in d.items() if k not in ("seed", "set")})
