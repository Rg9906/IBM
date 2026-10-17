# Demo script and checklist

Everything is shown on a laptop against the simulator. No claim about LinuxONE, Telum or production. Say "in simulation" with every outcome.

## Setup (5 minutes)

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
set QS_API_KEY=demo
python -m uvicorn --factory qs.api.app:create_app --port 8000
```

Open http://localhost:8000 and paste `demo` into the API key box. `python scripts/demo.py` runs the same flow headlessly. Pre-run S4 once to check timing; at speed 4x S4 reaches its incident in about a minute.

## Four-minute script

1. **0:00 Problem (20 s).** A timeout is not a failure; retries amplify load; averages hide it.
2. **0:20 Normal (20 s).** Click Normal. Heat map stays cool, state MONITORING.
3. **0:40 Surge (30 s).** Click Surge. Load rises, no incident should be raised. If an alert appears, say so: the S1 surge gate is imperfect (RESULTS, P2).
4. **1:10 Retry storm (90 s).** Click Retry storm with human approval. Point out the heat map cell and localizer verdict for the gateway, the detector race (D0 and D3 flags, delay in seconds from the injected onset), the cascade panel with `p_stuck` rising for G1, and the recommendation card. Show a blocked `SHIFT_TRAFFIC` with its reason. Approve; show the decision-log row.
5. **2:40 Verification storm (30 s).** Click Verification storm. Show the status-check rate in the verification panel; state the offline numbers (517.8 to 13.0 checks/s).
6. **3:10 Policy arena tab (30 s).** Show the committed medians. Say where U1 or U2 win.
7. **3:40 Honest limits (20 s).** D3 does not beat D1 on delay; false alerts exceed the budget on surges; the forecast gives no lead against collapse; simulator only.

## Pre-recording checklist

- [ ] Server started with a known key; `/v1/health` returns ok
- [ ] Browser zoom set so the Live tab fits at the recording resolution
- [ ] One dry run per scenario used in the script
- [ ] Every number spoken matches results/summary.md
- [ ] Every sentence checked against docs/CLAIMS.md; no mention of Telum, z/OS, Secure Execution, production or prediction of outages
- [ ] LinuxONE performance is not shown (not measured)

## Dashboard acceptance checklist (blueprint 16.7)

Checked by tests or a scripted run only, not by eye: page and assets are local (test); a scenario starts; approving writes a decision-log row and applies actions (scripts/demo.py).

Still to confirm visually: heat-map highlight on S2, S3 and S7; detector flags (only D0 and D3 run live); the cascade panel (a `p_stuck` timeline, not the blueprint's phase plane); the degraded banner and disabled buttons after a 12 s stalled stream.
