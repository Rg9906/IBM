import json
import time

from qs.config import LifecycleConfig
from qs.core.lifecycle import DegradedMonitor, Lifecycle, overlaps
from qs.schema import Alert, FeatureRow, IncidentState as IS, OperatingState
from qs.store.db import Store

G1 = ("gateway", "G1", "*")
R11 = ("route", "G1", "B1")
B2 = ("bank", "*", "B2")


def alert(ts, seg=G1, kind="segment", sev=OperatingState.WARNING):
    return Alert(ts=ts, detector="D3", segment=seg, severity=sev, kind=kind)


def test_overlap():
    assert overlaps(G1, R11) and overlaps(("platform", "*", "*"), B2) and overlaps(G1, ("bank", "*", "B1"))
    assert not overlaps(G1, ("gateway", "G2", "*")) and not overlaps(R11, ("route", "G1", "B2"))


def test_merge_overlapping_within_30s():
    lc = Lifecycle()
    a = lc.on_alert(alert(0, G1))
    b = lc.on_alert(alert(20_000, R11))
    c = lc.on_alert(alert(80_000, ("gateway", "G2", "*")))
    assert a.incident_id == b.incident_id != c.incident_id
    assert R11 in a.merged_segments


def test_platform_alert_unlocalised_never_merged():
    lc = Lifecycle()
    inc = lc.on_alert(alert(0))
    assert lc.on_alert(alert(1000, ("platform", "*", "*"), kind="platform")) is None
    assert len(lc.unlocalised) == 1 and inc.merged_segments == ()


def test_full_cycle_and_reopen_within_60s_keeps_id():
    lc = Lifecycle()
    inc = lc.on_alert(alert(0))
    assert inc.state == IS.OPEN
    lc.approve(inc.incident_id)
    assert inc.state == IS.MITIGATING
    t = 0
    for i in range(3):
        t += 5000
        lc.tick(t, set())
    assert inc.state == IS.RECOVERING
    for i in range(3):
        t += 5000
        lc.tick(t, set())
    assert inc.state == IS.CLOSED and inc.closed_ts == t
    again = lc.on_alert(alert(t + 40_000))
    assert again is inc and inc.state == IS.OPEN and inc.reopened == 1
    # after 60 s a new warning opens a new incident
    for i in range(6):
        lc.tick(t + 50_000 + 5000 * i, set())
    other = lc.on_alert(alert(t + 50_000 + 30_000 + 61_000))
    assert other.incident_id != inc.incident_id


def test_deterioration_returns_to_mitigating_and_active_resets_clean_count():
    lc = Lifecycle()
    inc = lc.on_alert(alert(0))
    lc.approve(inc.incident_id)
    for t in (5000, 10000, 15000):
        lc.tick(t, set())
    assert inc.state == IS.RECOVERING
    lc.on_alert(alert(20_000))
    assert inc.state == IS.MITIGATING
    lc.tick(25_000, {G1})
    assert inc.clean_ticks == 0


def test_critical_raises_severity():
    lc = Lifecycle()
    inc = lc.on_alert(alert(0))
    lc.on_alert(alert(5000, sev=OperatingState.CRITICAL))
    assert inc.severity == OperatingState.CRITICAL and inc.state == IS.OPEN


def row(seg, flags=(), base_arr=50.0):
    return FeatureRow(tick_ts=0, segment=seg, eligible=False, flags=list(flags), f={}, z={},
                      base={"arrival_rate": base_arr})


def test_degraded_after_12s_silence_within_one_tick():
    dm = DegradedMonitor()
    assert dm.check(5000, 4000, []) == []
    assert dm.check(10_000, 4000, []) == []                      # 6 s silent: still healthy
    assert "NO_EVENTS" in dm.check(16_000, 4000, [])             # 12 s silent: flagged on this tick
    assert dm.state(["NO_EVENTS"]) == OperatingState.DEGRADED_MONITORING
    assert dm.state([]) == OperatingState.MONITORING


def test_degraded_other_triggers():
    dm = DegradedMonitor(LifecycleConfig())
    assert "SEGMENT_SILENT" in dm.check(5000, 4000, [row(G1, ["no_data"])])
    assert dm.check(10_000, 9000, [row(G1, ["no_data", "warming_up"])]) == []
    assert "MODEL_INPUTS_NAN" in dm.check(15_000, 14_000, [], {G1: False})
    dm.model_call(80.0)
    assert dm.check(20_000, 19_000, []) == []
    dm.model_call(80.0)
    assert "MODEL_FAILURE" in dm.check(25_000, 24_000, [])
    dm.model_call(1.0)
    assert dm.check(30_000, 29_000, []) == []
    assert "CLOCK_STALL_OR_BACKWARDS" in dm.check(30_000, 29_000, [])
    assert "CLOCK_STALL_OR_BACKWARDS" in dm.check(20_000, 29_000, [])


def test_store_batches_and_dedupes(tmp_path):
    s = Store(str(tmp_path / "q.db"), LifecycleConfig(batch_rows=500, batch_ms=10_000))
    for i in range(499):
        s.append("events", (f"e{i}", "ATTEMPT_SENT", "p", i, None, "{}"))
    assert s.flushes == 0
    s.append("events", ("e0", "ATTEMPT_SENT", "p", 0, None, "{}"))      # 500th row, duplicate id ignored
    assert s.flushes == 1
    assert s.query("select count(*) from events")[0][0] == 499
    assert s.query("pragma journal_mode")[0][0] == "wal"


def test_store_time_flush():
    now = [0.0]
    s = Store(":memory:", LifecycleConfig(batch_rows=500, batch_ms=250), clock=lambda: now[0])
    s.append("runs", ("r1", "S0", 1, "U0", "{}"))
    now[0] = 0.3
    assert s.flush_if_due() and s.flushes == 1


def test_store_keeps_up_with_5000_events_per_s(tmp_path):
    s = Store(str(tmp_path / "q.db"))
    n = 20_000
    payload = json.dumps({"x": 1})
    t0 = time.perf_counter()
    for i in range(n):
        s.append("events", (f"e{i}", "ATTEMPT_OUTCOME", f"p{i}", i, None, payload))
    s.flush()
    rate = n / (time.perf_counter() - t0)
    assert rate > 5000, rate
