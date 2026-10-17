# QueueShield v2 architecture

This describes what is implemented in the repository. The specification (including parts not built) is the blueprint in this directory; deviations are in [DEVIATIONS.md](DEVIATIONS.md).

## Big picture

```text
 simulator (qs/sim)                        decision path (qs/core, qs/forecast)
 ┌───────────────────────┐   events   ┌────────────────────────────────────────────────────────┐
 │ Engine + Tape         │──────────▶│ Ingestor ─▶ PaymentTracker ─▶ FeatureEngine (windows)   │
 │ clients (naive,       │            │      │ (watermark, dedupe)        │ per-segment features  │
 │  disciplined)         │            │      ▼                            ▼                       │
 │ gateways, banks,      │            │  CellTracker              Detectors D0 D3 (+D1 D2 offline)│
 │ status service,       │            │  (gateway×bank bad/exp)   Cascade Forecaster (p_stuck)    │
 │ server-side controls  │            │      │                            │                       │
 └──────────▲────────────┘            │      ▼                            ▼                       │
            │ approved actions        │  Fault Localizer ─▶ Lifecycle (incidents) ─▶ PolicyEngine │
            │                         │                                              │            │
            └─────────────────────────┤  ActionManager (approval, rollback) ◀────────┘            │
              (qs/api/live.py or HTTP)│  Verification Governor (status-check budget)              │
                                      └────────────────────────────────────────────────────────┘
                                                   │ SQLite store (qs/store), HTTP API + SSE (qs/api)
```

The two sides never import each other (`qs/core` must not import `qs/sim` and vice versa). `qs/api/live.py` and `qs/eval/*` are the only places that join them.

## Concepts

- **Scenarios S0 to S7** (qs/sim/scenarios.py): normal, benign surge, gateway slowdown, bank slowdown, retry-storm trigger, surge plus slowdown, verification storm (bank black-holes attempts), silent route drift. Surge entries are labelled not-an-incident.
- **Segments**: `(level, gateway, bank)` keys such as `("platform","*","*")`, `("gateway","G1","*")`, `("route","G1","B2")`. Three gateways G1 to G3, five banks B1 to B5.
- **Policy universes U0 to U3** (qs/eval/universes.py): do nothing; textbook failover on success ratio; disciplined clients with Governor-style checks and no detection; QueueShield recommendations approved by a simulated operator after a delay.
- **Detectors D0 to D3**: D0 platform success threshold; D1 per-segment fixed thresholds; D2 per-segment CUSUM on baseline z-scores; D3 QueueShield (rules plus surge gate plus Isolation Forest, with persistence). Ablation arms D3:M (model only) and D3:R (rules only). RRCF and EWS variants are implemented but not evaluated.
- **Time**: all times are simulator milliseconds. Pipeline ticks every 5 s of event time after a 2 s watermark.

## Per-event path

`Pipeline.ingest(events)` → `Ingestor` (validates, dedupes by event id, holds events until the watermark passes so handlers see an ordered stream; late events are flagged) → `_on_event`: `PaymentTracker.apply` (payment state machine, qs/core/payments.py), `FeatureEngine.ingest` (rolling windows, qs/core/windows.py), `CellTracker.on_event` (gateway×bank bad/expected counts for the localizer), Governor hooks when verification is active, and the optional SQLite event log.

## Per-tick path (`Pipeline.tick`)

1. Feature rows for every eligible segment (qs/core/features.py, baselines in baselines.py).
2. Forecast `p_stuck` per gateway (qs/forecast/model.py), then D3 and D0 alerts. Alerts fire once on the rising edge.
3. Degraded-monitoring checks (stale feed, missing snapshots, missing model vectors). The state becomes `DEGRADED_MONITORING` rather than appearing healthy.
4. Localize (qs/core/localizer.py) from the cell matrices; open or merge incidents (lifecycle.py); raise severity to CRITICAL when `p_stuck` reaches `DetectorConfig.forecast_critical` (0.5).
5. For each live incident whose (severity, verdict) changed, build a `Recommendation` with the policy engine (policy.py), supersede the previous one, queue it in `ActionManager`.
6. Rollback proposals after 6 clean ticks; Governor releases due status checks within the spare-capacity budget; baselines learn only in calm periods; features, incidents are written to SQLite if a store is attached.

## Modules

| Path | Responsibility |
| --- | --- |
| `qs/schema.py` | The only definition of enums and record types (Event, Alert, Incident, Recommendation, Action, Decision, …) |
| `qs/config.py`, `configs/*.yaml` | Frozen dataclass configuration; thresholds come from YAML |
| `qs/clock.py`, `qs/ids.py` | Simulator/wall clock; id formats (`run42-0001837`) |
| `qs/sim/` | Discrete-event simulator: `engine.py`, `entities.py`, `clients.py`, `routing.py`, `status_service.py`, `controls.py` (actions the platform can switch on), `scenarios.py`, `tape.py` (common random numbers), `run.py` |
| `qs/core/ingest.py`, `dedupe.py`, `payments.py`, `windows.py`, `features.py`, `baselines.py` | Event handling and feature engine |
| `qs/core/detectors/` | `threshold.py` (D0/D1), `adaptive.py` (D2), `rules.py`, `iforest.py`, `rrcf.py`, `persistence.py` (D3 pipeline) |
| `qs/core/localizer.py`, `cells.py` | Fault Localizer: verdicts ROUTE, GATEWAY, BANK, PLATFORM, MULTI_FAULT, INSUFFICIENT_EVIDENCE |
| `qs/core/lifecycle.py` | Incident lifecycle and degraded-monitoring triggers |
| `qs/core/governor.py` | Verification Governor: bounded, jittered status checks with escalation of unresolved payments |
| `qs/core/policy.py`, `actions.py` | Rules mapping state to eligible actions; approval, rollback and the safety invariants |
| `qs/core/pipeline.py` | Wires everything; the contract used by API, live runner and harness |
| `qs/forecast/` | Cascade Forecaster: dataset generation, stuck grid, boosted ensemble, training and evaluation |
| `qs/ui/static/` | Operator console: plain HTML, JS and CSS, no external assets. Live tab (heat map, selected-segment metrics, detector race for D0 and D3, per-gateway `p_stuck` timeline, verification panel, recommendation card, decision log) and a Policy arena tab over `results/arena.json` |
| `qs/store/` | SQLite (WAL) store, schema in `schema.sql`; writes are batched off the request path |
| `qs/api/app.py`, `live.py` | FastAPI app (HTTP API v1, SSE) and the closed-loop runner |
| `qs/eval/` | Replay, row cache, detection scoring, paired statistics, policy arena (`universes.py`), simulated operator (`arena.py`) |
| `scripts/` | Calibration, datasets, training, tuning and evaluation entry points |
| `tests/unit/` | Acceptance tests per work package (`test_wp01.py` … `test_wp11.py`, `test_sim_engine.py`) |

## Safety design

- A client timeout is an unknown outcome; retries are gated until failure is confirmed (Governor and policy invariants, tested in `test_wp07.py` and `test_wp10.py`).
- Every action needs an operator decision (`ActionManager.decide`); the simulated operator exists for evaluation only. Rollbacks also need approval.
- Verdict gates: `SHIFT_TRAFFIC` is only eligible for a single-fault verdict above the localizer confidence thresholds and when the target has spare capacity (`configs/policy.yaml`).
- Degraded monitoring is surfaced as its own state and recommendation, never as "healthy".
- Write endpoints require `X-QS-Key` (compared in constant time). If the key is unset, a random key is generated and logged once.

## HTTP API v1

Implemented in `qs/api/app.py`. Errors use `{"error", "detail"}`.

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `POST /v1/events` | key | Ingest up to 500 events; returns 202 with accepted/duplicates/rejected |
| `GET /v1/state` | none | Latest tick: per-segment features, score, p_stuck, localizer verdict |
| `GET /v1/stream` | none | SSE: `alert`, `incident`, `recommendation`, `decision`, `applied`, `snapshot` |
| `GET /v1/incidents` | none | Incidents, filter by `state`, `limit` |
| `GET /v1/recommendations` | none | Recommendations, filter by `status` |
| `POST /v1/recommendations/{rec_id}/decision` | key | Record an operator decision (409 if rejected by invariants) |
| `GET /v1/actions/pending` | key | Approved actions for the simulator to apply |
| `POST /v1/actions/{id}/applied` | key | Simulator acknowledgement |
| `POST /v1/sim/scenario` | key | Start a scenario: `scenario`, `seed`, `speed`, `arm`, `auto_operator`, `operator_delay_ms` |
| `POST /v1/sim/control` | key | `pause`, `resume` or `stop` the running scenario |
| `GET /v1/sim/truth` | none | Injected ground truth of the running scenario, for the demo console only |
| `GET /v1/decisions`, `/v1/decisions.csv` | none | Decision log as JSON and CSV |
| `GET /v1/arena` | none | Committed policy-arena runs for the console's arena tab |
| `GET /`, `/static/*` | none | The operator console |
| `GET /v1/metrics` | none | Ingest counters, model versions, detector mode |
| `GET /v1/health` | none | Liveness and global state |

Known gaps (from DEVIATIONS WP-11): wall-clock pacing is approximate, no key rotation, SSE keeps only the last 5000 events, `/v1/state` returns the latest tick only, read endpoints are unauthenticated. The blueprint's `/v1/metrics` latency histograms are not implemented.

## Configuration

| File | Content |
| --- | --- |
| `configs/sim_default.yaml` | Simulator parameters (frozen after calibration, tag `sim-frozen` in the protocol) |
| `configs/detectors_frozen.yaml`, `detectors_frozen_arms.json` | D3 parameters and per-arm tuned sensitivities, frozen before set C |
| `configs/governor.yaml` | Verification Governor budgets |
| `configs/policy.yaml` | Throttle fraction, shed/shift limits, localizer hysteresis (0.67/0.50), approval confidence (0.6), rollback ticks |
| `configs/operators.yaml` | Simulated operator delay and behaviour |
| `configs/rails/upi_like.yaml` | UPI-style analogy preset, labelled as an analogy |

Per the agent rules, frozen configs must not change after the freeze. `results/MANIFEST.json` stores their SHA-256 hashes.

## Data and artefacts

| Path | Content |
| --- | --- |
| `artifacts/models/` | `iforest.pkl`, `iforest_ews.pkl`, `forecaster/`, `registry.json` (versions loaded by the pipeline) |
| `artifacts/data/` | Cascade datasets for sets A, B, C |
| `artifacts/rows/` | Cached per-tick feature rows used for offline detector evaluation (float32; one U0 replay per configuration) |
| `artifacts/results/` | Intermediate results: forecaster evaluation, localizer evaluation, tuning trace, metastability sweeps, S6 contrast |
| `results/` | Final harness outputs (`detection.json`, `arena.json`, `s6.json`, `forecast_false_critical.json`), `summary.md`, `MANIFEST.json` |

Everything is synthetic; no real card, account or personal data exists in the repository.

## Platform notes

Nothing in the code uses a platform-specific accelerator. The agent rules assume the target may be s390x (big-endian) and require recording new dependencies; endianness-sensitive model portability (ONNX, blueprint 18.6) has not been done, and the LinuxONE environment check and benchmarks (WP-00, WP-14) have not been run.
