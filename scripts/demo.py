"""Scripted demo against a running API: start a scenario, wait for a recommendation, approve it, print the decision log.

Usage: demo.py [--url http://localhost:8000] [--key demo] [--scenario S4] [--seed 1] [--speed 60]
Start the server first: QS_API_KEY=demo uvicorn --factory qs.api.app:create_app --port 8000
"""
import argparse
import json
import time
import urllib.request


def call(url, key, path, body=None):
    req = urllib.request.Request(url + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-QS-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", default="demo")
    ap.add_argument("--scenario", default="S4")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--speed", type=float, default=60)
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()
    print("start", call(a.url, a.key, "/v1/sim/scenario", {"scenario": a.scenario, "seed": a.seed, "speed": a.speed}))
    end, rec = time.time() + a.timeout, None
    while time.time() < end and rec is None:
        time.sleep(1)
        st = call(a.url, a.key, "/v1/state")
        pend = call(a.url, a.key, "/v1/recommendations?status=pending")
        print(f"t={(st['sim_t_ms'] or 0) / 1000:.0f}s state={st['state']} verdict={(st['verdict'] or {}).get('verdict')}")
        rec = pend[-1] if pend else None
    if rec is None:
        raise SystemExit("no recommendation within the timeout")
    print("recommendation", rec["rec_id"], rec["severity"], rec["verdict"], [(x["id"], x["eligible"]) for x in rec["actions"]])
    call(a.url, a.key, f"/v1/recommendations/{rec['rec_id']}/decision",
         {"decision": "approve_all", "operator": "operator1", "note": "scripted demo", "actions_selected": []})
    print(json.dumps(call(a.url, a.key, "/v1/decisions"), indent=1))
    call(a.url, a.key, "/v1/sim/control", {"action": "stop"})


if __name__ == "__main__":
    main()
