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
