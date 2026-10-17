"""WP-12: dashboard serving and the console endpoints (blueprint 16)."""
import time

from fastapi.testclient import TestClient

from qs.api.app import create_app

KEY = "test-key"
H = {"X-QS-Key": KEY}


def client():
    return TestClient(create_app(api_key=KEY))


def test_console_and_assets_are_served_locally():
    c = client()
    r = c.get("/")
    assert r.status_code == 200 and "QueueShield" in r.text
    for path in ("/static/app.js", "/static/style.css"):
        assert c.get(path).status_code == 200
    html = r.text + c.get("/static/app.js").text
    assert "http://" not in html and "https://" not in html      # no CDN or external assets


def test_control_requires_key_and_rejects_bad_action():
    c = client()
    assert c.post("/v1/sim/control", json={"action": "pause"}).status_code == 401
    assert c.post("/v1/sim/control", headers=H, json={"action": "nope"}).status_code == 422
    assert c.post("/v1/sim/control", headers=H, json={"action": "pause"}).json() == {"paused": True}
    assert c.post("/v1/sim/control", headers=H, json={"action": "resume"}).json() == {"paused": False}


def test_empty_state_decisions_truth_and_arena():
    c = client()
    assert c.get("/v1/decisions").json() == []
    assert c.get("/v1/sim/truth").json() == []
    assert c.get("/v1/decisions.csv").text.startswith("ts,rec_id,incident_id")
    assert isinstance(c.get("/v1/arena").json(), list)
    st = c.get("/v1/state").json()
    assert st["paused"] is False and st["scenario"] is None


def test_scenario_exposes_truth_and_pause_stops_time():
    c = client()
    assert c.post("/v1/sim/scenario", headers=H, json={"scenario": "S4", "seed": 1, "speed": 50}).status_code == 202
    time.sleep(1.5)
    assert c.get("/v1/state").json()["scenario"]["scenario"] == "S4"
    truth = c.get("/v1/sim/truth").json()
    assert truth and truth[0]["is_incident"] is True
    c.post("/v1/sim/control", headers=H, json={"action": "pause"})
    time.sleep(0.5)
    t1 = c.get("/v1/state").json()["sim_t_ms"]
    time.sleep(1.0)
    assert c.get("/v1/state").json()["sim_t_ms"] == t1
    c.post("/v1/sim/control", headers=H, json={"action": "stop"})


def test_far_future_timestamp_is_rejected_before_it_can_stall_the_clock():
    c = client()
    r = c.post("/v1/events", headers=H, json=[{"event_timestamp": 1_700_000_000_000}])
    assert r.status_code == 422 and r.json()["error"] == "timestamp_too_far_ahead"


def test_malformed_bodies_get_4xx_not_500():
    c = client()
    assert c.post("/v1/actions/A1/applied", headers=H, json=[1, 2]).status_code == 400
    assert c.post("/v1/sim/scenario", headers=H, json={"scenario": "S4", "seed": "x"}).status_code == 422
    assert c.post("/v1/sim/scenario", headers=H, json={"scenario": "S4", "speed": 1e9}).status_code == 422
    assert c.post("/v1/sim/scenario", headers=H, content=b"not json").status_code == 400
    assert c.post("/v1/sim/control", headers=H, content=b"[]").status_code == 422
    assert c.get("/v1/incidents?limit=0").status_code == 422
    assert c.post("/v1/events", headers=H, content=b"x" * 1_100_000).status_code == 400


def test_applied_report_cannot_overwrite_its_action_id():
    c = client()
    assert c.post("/v1/actions/A1/applied", headers=H, json={"action": "evil", "ts": 1}).status_code == 200
    assert c.app.state.qs.applied_reports[-1]["action"] == "A1"


def test_sse_ids_keep_increasing_across_scenarios():
    c = client()
    st = c.app.state.qs
    st.emit("x", {})
    first = st.events[-1]["id"]
    st.events.clear()
    st.emit("x", {})
    assert st.events[-1]["id"] > first


def test_rollback_proposal_needs_operator_and_is_approvable_over_http():
    from qs.core.actions import Applied
    from qs.schema import Action, ActionId
    c = client()
    st = c.app.state.qs
    ap = Applied(Action(id=ActionId.SHED_EXPIRED, scope=("gateway", "G1", "*"), rec_id="rec-0001"), "rec-0001", 1000, "op")
    st.pipe.actions.rollback_proposals["rec-0001:SHED_EXPIRED"] = ap
    assert c.get("/v1/rollbacks").json()[0]["key"] == "rec-0001:SHED_EXPIRED"
    assert c.post("/v1/rollbacks/rec-0001:SHED_EXPIRED/approve", headers=H, json={}).status_code == 422
    assert c.post("/v1/rollbacks/nope/approve", headers=H, json={"operator": "op1"}).status_code == 404
    assert c.post("/v1/rollbacks/rec-0001:SHED_EXPIRED/approve", headers=H, json={"operator": "op1"}).status_code == 200
    assert ap.rolled_back and c.get("/v1/rollbacks").json() == []
    assert c.post("/v1/rollbacks/rec-0001:SHED_EXPIRED/approve", headers=H, json={"operator": "op1"}).status_code == 409
