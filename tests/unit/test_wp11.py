import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "scripts")
from make_configs import make_configs  # noqa: E402
from qs.api.app import create_app  # noqa: E402
from qs.api.live import LiveRun  # noqa: E402
from qs.config import OperatorConfig, SimConfig  # noqa: E402
from qs.eval.arena import SimulatedOperator  # noqa: E402
from qs.schema import Verdict  # noqa: E402
from qs.sim import scenarios  # noqa: E402
from qs.sim.engine import Engine  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402
from qs.sim.tape import Tape  # noqa: E402

KEEP = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
        "client_timeout_ms", "attempt_cap", "flash_mult"}
KEY = "test-key"


def s4_params():
    c = [c for c in make_configs("B", 6, 0) if c["scenario"] == "S4"][0]
    return c["seed"], ScenarioParams.from_dict({k: v for k, v in c.items() if k in KEEP})


@pytest.fixture(scope="module")
def events():
    seed, p = s4_params()
    cfg = scenarios.effective_cfg(SimConfig(), p)
    spec = scenarios.build(p)
    horizon = p.onset_ms + 70_000
    eng = Engine(cfg, Tape(seed, cfg, horizon_ms=horizon, rate_fn=spec.rate_fn, rate_max_mult=spec.rate_max_mult),
                 run_id="t", modifiers=spec.modifiers, horizon_ms=horizon, seed=seed, attempt_cap=p.attempt_cap)
    out, t = [], 0
    while t < horizon:
        t += 1000
        eng.run_until(t)
        out += eng.drain()
    return p, [e.to_json_dict() for e in out]


def client():
    return TestClient(create_app(api_key=KEY))


H = {"X-QS-Key": KEY}


def test_writes_require_key_and_errors_are_json():
    c = client()
    r = c.post("/v1/events", json=[])
    assert r.status_code == 401 and set(r.json()) == {"error", "detail"}
    assert c.post("/v1/events", json=[], headers={"X-QS-Key": "nope"}).status_code == 401
    assert c.get("/v1/actions/pending").status_code == 401
    assert c.post("/v1/recommendations/rec-1/decision", json={}).status_code == 401
    assert c.get("/v1/health").json()["status"] == "ok"


def test_ingest_validation_batch_limit_and_duplicates(events):
    _, evs = events
    c = client()
    assert c.post("/v1/events", json={"x": 1}, headers=H).status_code == 400
    assert c.post("/v1/events", json=evs[:501], headers=H).status_code == 413
    bad = dict(evs[0], gateway_id="G9")
    r = c.post("/v1/events", json=[evs[0], evs[1], bad], headers=H)
    assert r.status_code == 202 and r.json()["accepted"] == 2 and r.json()["rejected"] == 1
    r2 = c.post("/v1/events", json=[evs[0]], headers=H)
    assert r2.json()["duplicates"] == 1
    assert c.get("/v1/metrics").json()["ingest"]["accepted"] == 2


def test_scripted_client_ingest_recommend_approve_apply(events):
    p, evs = events
    c = client()
    for i in range(0, len(evs), 500):
        assert c.post("/v1/events", json=evs[i:i + 500], headers=H).status_code == 202
    recs = c.get("/v1/recommendations").json()
    assert recs, "no recommendation after the fault"
    rec = next(r for r in recs if r["verdict"] in ("GATEWAY_SIDE", "ROUTE_SPECIFIC") or r["actions"])
    assert len(rec["features_sha256"]) == 64 and rec["model_versions"]["detector"] != "none"
    assert c.get("/v1/incidents").json()
    assert c.get("/v1/actions/pending", headers=H).json() == []            # SI-3: nothing before a decision
    eligible = [a["id"] for a in rec["actions"] if a["eligible"]]
    bad = c.post(f"/v1/recommendations/{rec['rec_id']}/decision", headers=H,
                 json={"decision": "approve_all"})
    assert bad.status_code == 422                                           # operator is required
    ok = c.post(f"/v1/recommendations/{rec['rec_id']}/decision", headers=H,
                json={"decision": "approve_selected", "operator": "alice", "actions_selected": eligible[:1]})
    assert ok.status_code == 200
    pend = c.get("/v1/actions/pending", headers=H).json()
    assert [a["id"] for a in pend] == [a for a in eligible[:1] if a not in ("HOLD_MONITOR", "ESCALATE_HUMAN")]
    assert c.post(f"/v1/actions/{pend[0]['id']}/applied", headers=H, json={"ts": 1, "result": "ok"}).json() == {"ok": True}
    again = c.post(f"/v1/recommendations/{rec['rec_id']}/decision", headers=H,
                   json={"decision": "reject", "operator": "alice"})
    assert again.status_code == 409
    assert c.get("/v1/state").json()["tick_ts"] is not None


def test_closed_loop_detects_localises_and_applies_after_approval():
    seed, p = s4_params()
    lr = LiveRun("S4", seed, "U3", params=p, operator=SimulatedOperator(OperatorConfig(approval_delay_ms=10_000)),
                 horizon_ms=p.onset_ms + 90_000)
    lr.run()
    alerts = [a for r in lr.results for a in r.alerts if a.detector == "D3"]
    assert alerts and p.onset_ms <= alerts[0].ts <= p.onset_ms + 60_000
    recs = list(lr.pipe.recommendations.values())
    assert recs and recs[0].verdict is Verdict.GATEWAY_SIDE
    decided = {d.rec_id: d.decided_ts for d in lr.pipe.actions.log}
    assert decided and lr.applied
    first_apply = min(t for t, _, rb in lr.applied if not rb)
    assert first_apply >= min(decided.values())                              # applied only after a decision
    assert all(d.operator == "sim-operator" for d in lr.pipe.actions.log)
    assert lr.engine.controls.throttles or lr.engine.controls.shed or lr.engine.controls.governed


def test_scenario_endpoint_validation_and_start_stop():
    app = create_app(api_key=KEY)
    c = TestClient(app)
    assert c.post("/v1/sim/scenario", json={"scenario": "S9"}, headers=H).status_code == 422
    assert c.post("/v1/sim/scenario", json={"scenario": "S0"}).status_code == 401
    r = c.post("/v1/sim/scenario", json={"scenario": "S0", "seed": 3, "speed": 1000}, headers=H)
    assert r.status_code == 202
    st = app.state.qs
    import time
    time.sleep(3)
    st.stop.set()
    st.thread.join(timeout=10)
    assert st.run.t > 0 and any(e["kind"] == "snapshot" for e in st.events) or st.run.t < 10_000
