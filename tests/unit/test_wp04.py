import math
import time

import numpy as np
import pytest

from qs.config import FeatureConfig, SimConfig
from qs.core import windows as W
from qs.core.baselines import Baselines
from qs.core.dedupe import DedupeLRU
from qs.core.features import FEATURES, FI, FeatureEngine
from qs.core.ingest import Ingestor
from qs.core.payments import PaymentTracker
from qs.schema import Event, EventType as ET, PaymentState as PS, Status as ST

_n = [0]


def mk(et, ts, pid="p-1", k=0, gw="G1", bank="B1", status=None, rt=None, q=None, act=None, conf=None, err=None,
       eid=None):
    _n[0] += 1
    return Event.model_construct(
        schema_version=1, event_id=eid or f"e{_n[0]}", event_type=et, payment_id=pid, attempt_id=f"{pid}-a{k}",
        attempt_no=k, idempotency_key=None, gateway_id=gw, bank_id=bank, event_timestamp=ts,
        received_timestamp=None, response_time_ms=rt, status=status, error_code=err, retry_count=k,
        is_retry=k > 0, queue_depth=q, active_requests=act, configured_capacity=conf, client_timeout_ms=None)


# ---------------------------------------------------------------- dedupe and ingest
def test_dedupe_lru_evicts_oldest():
    d = DedupeLRU(3)
    assert [d.seen_before(x) for x in "abca"] == [False, False, False, True]
    d.seen_before("d")
    d.seen_before("e")          # evicts the least recently used
    assert len(d) == 3
    assert d.seen_before("b") is False   # b was evicted, so it is new again


def test_ingest_rejects_duplicates_and_orders_by_watermark():
    got = []
    ing = Ingestor([lambda e, late: got.append((e.event_id, late))], watermark_ms=2000)
    good = mk(ET.ATTEMPT_SENT, 10_000, eid="x1").to_json_dict()
    bad = dict(good, event_id="x2", status="NOPE")
    missing = {k: v for k, v in good.items() if k != "gateway_id"}
    r = ing.ingest([good, bad, missing, good])
    assert (r.accepted, r.duplicates, r.rejected) == (1, 1, 2) and len(r.errors) == 2
    # out of order inside the watermark is re-ordered; one beyond the watermark is delivered late
    ing.ingest([mk(ET.ATTEMPT_SENT, 13_000, eid="b"), mk(ET.ATTEMPT_SENT, 12_000, eid="a")])
    ing.ingest([mk(ET.ATTEMPT_SENT, 20_000, eid="z")])
    assert [g[0] for g in got] == ["x1", "a", "b"]
    ing.ingest([mk(ET.ATTEMPT_SENT, 11_000, eid="old")])
    assert got[-1] == ("old", True) and ing.metrics["late"] == 1
    ing.flush()
    assert got[-2:][0] == ("old", True) and got[-1][0] == "z"


# ---------------------------------------------------------------- payment state machine
def test_payment_state_machine_and_pending_counts():
    t = PaymentTracker()
    t.apply(mk(ET.PAYMENT_INITIATED, 0))
    assert t.state("p-1") is PS.INITIATED
    t.apply(mk(ET.ATTEMPT_SENT, 1))
    assert t.state("p-1") is PS.IN_FLIGHT
    t.apply(mk(ET.ATTEMPT_OUTCOME, 3001, status=ST.CLIENT_TIMEOUT, rt=3000))
    assert t.state("p-1") is PS.PENDING_UNKNOWN
    seg = lambda k: W.SEGMENT_INDEX[k]
    assert t.pending[0] == 1 and t.pending[seg(("gateway", "G1", "*"))] == 1 and t.pending[seg(("route", "G1", "B1"))] == 1
    t.apply(mk(ET.STATUS_CHECK_SENT, 13_000))
    assert t.state("p-1") is PS.STATUS_CHECKING
    t.apply(mk(ET.STATUS_CHECK_RESULT, 13_100, status=ST.UNKNOWN))
    assert t.state("p-1") is PS.PENDING_UNKNOWN
    t.apply(mk(ET.STATUS_CHECK_SENT, 20_000))
    t.apply(mk(ET.STATUS_CHECK_RESULT, 20_100, status=ST.CONFIRMED_FAILED))
    assert t.state("p-1") is PS.CONFIRMED_FAILED and t.pending.sum() == 0
    t.apply(mk(ET.ATTEMPT_SENT, 21_000, k=1))               # re-attempt allowed after CONFIRMED_FAILED
    assert t.state("p-1") is PS.IN_FLIGHT and t.metrics["retry_while_unknown"] == 0
    t.apply(mk(ET.ATTEMPT_OUTCOME, 21_200, k=1, status=ST.SUCCESS, rt=200))
    assert t.state("p-1") is PS.CONFIRMED_SUCCESS
    t.apply(mk(ET.ATTEMPT_OUTCOME, 22_000, k=0, status=ST.CLIENT_TIMEOUT))   # stale; terminal stays
    assert t.state("p-1") is PS.CONFIRMED_SUCCESS


def test_retry_while_unknown_counted_and_escalation_terminal():
    t = PaymentTracker()
    t.apply(mk(ET.ATTEMPT_SENT, 1, pid="p-2"))
    t.apply(mk(ET.ATTEMPT_OUTCOME, 3000, pid="p-2", status=ST.CLIENT_TIMEOUT))
    t.apply(mk(ET.ATTEMPT_SENT, 4000, pid="p-2", k=1))
    assert t.metrics["retry_while_unknown"] == 1 and t.pending.sum() == 0
    t.apply(mk(ET.ATTEMPT_OUTCOME, 7000, pid="p-2", k=1, status=ST.CLIENT_TIMEOUT))
    assert t.escalate("p-2", 8000) and t.state("p-2") is PS.ESCALATED_RECONCILE and t.pending.sum() == 0
    t.apply(mk(ET.ATTEMPT_SENT, 9000, pid="p-2", k=2))
    assert t.state("p-2") is PS.ESCALATED_RECONCILE and t.metrics["attempt_after_terminal"] == 1


# ---------------------------------------------------------------- windows
def test_bucket_edge_lands_in_one_bucket_and_ring_wraps():
    w = W.Windows(120)
    w.add(mk(ET.ATTEMPT_SENT, 9_999))
    w.add(mk(ET.ATTEMPT_SENT, 10_000))
    w.add(mk(ET.ATTEMPT_SENT, 19_999))
    w.add(mk(ET.ATTEMPT_SENT, 20_000))
    w.advance(20)
    assert w.sum(W.SENT, 20, 10)[0] == 2      # [10 s, 20 s): the events at 10_000 and 19_999 only
    assert w.sum(W.SENT, 20, 11)[0] == 3
    w.advance(20 + 125)                        # wraps past the ring: stale buckets are cleared
    assert w.sum(W.SENT, 145, 120)[0] == 0
    w.add(mk(ET.ATTEMPT_SENT, 5_000))          # older than the ring: dropped and counted
    assert w.late_dropped == 1


# ---------------------------------------------------------------- features on a hand-built stream
def build_stream():
    evs = []
    # seconds 30..39 on G1/B1: 10 fresh + 5 retry sends, 12 SUCCESS, 2 TIMEOUT, 1 REJECT, 1 status check
    for i in range(10):
        evs.append(mk(ET.ATTEMPT_SENT, 30_000 + i * 1000, pid=f"f{i}"))
    for i in range(5):
        evs.append(mk(ET.ATTEMPT_SENT, 30_500 + i * 1000, pid=f"r{i}", k=1))
    for i in range(12):
        evs.append(mk(ET.ATTEMPT_OUTCOME, 30_100 + i * 700, pid=f"f{i % 10}", status=ST.SUCCESS, rt=100))
    for i in range(2):
        evs.append(mk(ET.ATTEMPT_OUTCOME, 35_000 + i, pid=f"r{i}", k=1, status=ST.CLIENT_TIMEOUT, rt=3000))
    evs.append(mk(ET.ATTEMPT_OUTCOME, 36_000, pid="r4", k=1, status=ST.REJECTED_OVERLOAD, rt=5))
    evs.append(mk(ET.STATUS_CHECK_SENT, 37_000, pid="r0", k=1))
    evs.append(mk(ET.ATTEMPT_SENT, 20_000, pid="out"))           # outside Ws, inside Wl
    evs.append(mk(ET.ATTEMPT_SENT, 40_000, pid="edge"))          # at the tick time: excluded
    for s in range(10, 40):
        for gi, g in enumerate(("G1", "G2", "G3")):
            evs.append(mk(ET.SERVICE_SNAPSHOT, s * 1000, pid="none", gw=g, q=(s - 10) if gi == 0 else 0,
                          act=20, conf=40))
    return sorted(evs, key=lambda e: e.event_timestamp)


def feed(fe, evs):
    for e in evs:
        fe.tracker.apply(e)
        fe.ingest(e)


def test_features_match_hand_computation():
    fe = FeatureEngine(FeatureConfig(), tick_ms=5000)
    feed(fe, build_stream())
    rows = {r.segment: r for r in fe.tick(40_000)}
    g1 = rows[("gateway", "G1", "*")].f
    assert g1["arrival_rate"] == pytest.approx(1.5) and g1["fresh_rate"] == pytest.approx(1.0)
    assert g1["raf"] == pytest.approx(1.5)
    assert g1["timeout_ratio"] == pytest.approx(2 / 15) and g1["reject_ratio"] == pytest.approx(1 / 15)
    assert g1["success_ratio"] == pytest.approx(12 / 15)                   # Wl = 60 s
    assert g1["status_check_rate"] == pytest.approx(0.1)
    assert math.isnan(g1["p95"])                                           # 14 completions < 20: no information
    assert g1["queue_depth"] == pytest.approx(24.5)                        # mean of q over seconds 30..39
    assert g1["queue_growth"] == pytest.approx(1.0, abs=0.05)
    assert g1["utilisation"] == pytest.approx(0.5)
    assert g1["queue_delay_est"] == pytest.approx(24.5 / 1.2)              # 12 successes in 10 s
    assert g1["pending_count"] == 2 and g1["orbit_proxy"] == 2
    assert g1["pending_frac"] == pytest.approx(2 / (1.5 * 60))
    # platform, bank and route views of the same traffic
    assert rows[("platform", "*", "*")].f["arrival_rate"] == pytest.approx(1.5)
    assert rows[("platform", "*", "*")].f["queue_depth"] == pytest.approx(24.5)   # sum over gateways
    assert rows[("bank", "*", "B1")].f["arrival_rate"] == pytest.approx(1.5)
    assert rows[("route", "G1", "B1")].f["queue_depth"] == pytest.approx(24.5)
    assert rows[("route", "G1", "B1")].flags.count("low_volume") == 1       # 15 attempts < 30
    assert "no_data" in rows[("bank", "*", "B2")].flags
    assert math.isnan(rows[("bank", "*", "B2")].f["timeout_ratio"])
    assert math.isnan(g1["expired_ratio"])


def test_latency_nan_below_20_completions_then_available():
    fe = FeatureEngine()
    evs = [mk(ET.ATTEMPT_OUTCOME, 31_000 + i, pid=f"q{i}", status=ST.SUCCESS, rt=200) for i in range(19)]
    feed(fe, evs)
    f = {r.segment: r for r in fe.tick(40_000)}[("gateway", "G1", "*")].f
    assert math.isnan(f["p50"]) and math.isnan(f["latency_ratio"])
    feed(fe, [mk(ET.ATTEMPT_OUTCOME, 32_000, pid="q99", status=ST.SUCCESS, rt=200)])
    f = {r.segment: r for r in fe.tick(40_000)}[("gateway", "G1", "*")].f
    assert f["p50"] == pytest.approx(200, rel=0.15)


def test_ratio_bounds_and_hierarchy_on_simulated_traffic():
    from qs.sim.run import run_scenario
    from qs.sim.scenarios import ScenarioParams
    fe = FeatureEngine()
    ing = Ingestor([lambda e, late: (fe.tracker.apply(e, late), fe.ingest(e, late))])
    state = {"next": 5000}

    def sink(evs):
        ing.ingest(evs)
        while ing.max_ts >= state["next"] + 2000:
            ing.release_until(state["next"])
            rows = fe.tick(state["next"])
            check(rows)
            state["next"] += 5000

    def check(rows):
        by = {r.segment: r.f for r in rows}
        for r in rows:
            for name in ("success_ratio", "timeout_ratio", "reject_ratio"):
                v = r.f[name]
                assert math.isnan(v) or 0.0 <= v <= 1.0
            if not math.isnan(r.f["raf"]):
                assert r.f["raf"] >= 1.0
        tot = by[("platform", "*", "*")]["arrival_rate"]
        assert sum(by[("gateway", g, "*")]["arrival_rate"] for g in ("G1", "G2", "G3")) == pytest.approx(tot)
        assert sum(by[("bank", "*", b)]["arrival_rate"] for b in ("B1", "B2", "B3", "B4", "B5")) == pytest.approx(tot)

    run_scenario("S2", 3, "U0", SimConfig(), sink=sink, params=ScenarioParams("S2", onset_ms=60_000, duration_ms=30_000,
                 magnitude=2.0), horizon_ms=150_000)
    assert state["next"] > 100_000


# ---------------------------------------------------------------- baselines
def test_baseline_warm_start_floor_and_freeze():
    bl = Baselines(["a", "timeout_ratio"], 2, alpha=0.02, warm_ticks=24)
    x = np.array([[10.0, 0.01], [5.0, 0.02]])
    for _ in range(24):
        bl.update(x, np.array([True, True]))
    assert not bl.warming_up().any()
    assert bl.mean[0, 0] == pytest.approx(10.0) and bl.spread[0, 0] == 0.0
    z = bl.z(np.array([[12.0, 0.01], [5.0, 0.02]]))
    assert z[0, 0] == pytest.approx(2.0 / bl.floor[0]) or z[0, 0] == 6.0   # floored spread, clipped to +-6
    assert z[1, 0] == pytest.approx(0.0)
    # freeze: an anomaly is not learned while frozen, and is learned (slowly) when not
    bl.set_frozen(np.array([True, False]))
    for _ in range(50):
        bl.update(np.array([[100.0, 0.5], [100.0, 0.5]]), np.array([True, True]))
    assert bl.mean[0, 0] == pytest.approx(10.0)
    assert bl.mean[1, 0] > 10.0
    # NaN means no information
    zz = bl.z(np.array([[np.nan, 0.01], [5.0, 0.02]]))
    assert np.isnan(zz[0, 0])


def test_feature_engine_warmup_and_freeze_flags():
    fe = FeatureEngine()
    flags = []
    for i in range(1, 30):
        t = i * 5000
        feed(fe, [mk(ET.ATTEMPT_SENT, t - 2500, pid=f"w{i}")])
        flags.append("warming_up" in {r.segment: r for r in fe.tick(t)}[("gateway", "G1", "*")].flags)
    assert flags[:24] == [True] * 24 and flags[24:] == [False] * 5
    before = fe.bl.mean.copy()
    fe.set_frozen({("gateway", "G1", "*")})
    feed(fe, [mk(ET.ATTEMPT_SENT, 150_000 + i, pid=f"x{i}") for i in range(500)])
    fe.tick(155_000)
    gi = W.SEGMENT_INDEX[("gateway", "G1", "*")]
    assert np.array_equal(fe.bl.mean[gi], before[gi], equal_nan=True)


# ---------------------------------------------------------------- performance
def test_tick_time_under_10ms_at_5000_events_per_second():
    fe = FeatureEngine()
    rng = np.random.default_rng(0)
    gws, banks = ("G1", "G2", "G3"), ("B1", "B2", "B3", "B4", "B5")
    evs = []
    for i in range(100_000):                       # 20 s at 5,000 events/s
        ts = i * 20_000 // 100_000 + 10_000
        et = ET.ATTEMPT_SENT if i % 2 == 0 else ET.ATTEMPT_OUTCOME
        evs.append(mk(et, ts, pid=f"p{i % 3000}", gw=gws[i % 3], bank=banks[(i // 3) % 5],
                      status=ST.SUCCESS if et is ET.ATTEMPT_OUTCOME else None,
                      rt=int(rng.integers(50, 600)) if et is ET.ATTEMPT_OUTCOME else None))
    t0 = time.perf_counter()
    for e in evs:
        fe.ingest(e)
    ingest_rate = len(evs) / (time.perf_counter() - t0)
    times = []
    for k in range(20):
        t0 = time.perf_counter()
        rows = fe.tick(30_000)
        times.append(time.perf_counter() - t0)
    assert len(rows) == 24
    med = sorted(times)[len(times) // 2]
    print(f"\ningest {ingest_rate:,.0f} events/s, tick median {med * 1000:.2f} ms")
    assert med < 0.010
