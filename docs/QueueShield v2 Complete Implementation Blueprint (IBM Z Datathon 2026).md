# QueueShield v2: Complete Implementation Blueprint (IBM Z Datathon 2026)

Oct 17, 2026 · @RG

## 1. Read me first

This document is the single source of truth for building QueueShield v2 during the IBM Z Datathon 2026; a teammate or coding agent should be able to start work from it with no other context.

**Who reads what**

| Reader | Read these sections first | Then |
| --- | --- | --- |
| Team lead (Srijaa) | 2, 3, 5, 6, 20, 26 | 23 (risks), 24 (demo) |
| Teammate on a work package | 1, 22 (own package), 19 | The spec sections the package cites |
| Coding agent | 1, then its package in 22 | Cited specs in 8 to 16; run the acceptance tests in the package |
| Judge or reviewer (later) | 2, 5, 17, 26, 27 | 25 (Q&A) |

**Conventions used everywhere**

- MUST, SHOULD and MAY follow their usual meaning: MUST is a hard requirement, SHOULD is the default unless a reason is recorded, MAY is optional.
- Identifiers: scenarios `S0` to `S7`, simulated policy universes `U0` to `U3`, detectors `D0` to `D3`, innovations `I1` to `I3`, work packages `WP-00` to `WP-15`, stage gates `G0` to `G6`.
- Time: all simulator and event times are in milliseconds on the simulator clock unless a field says otherwise. Wall time is only used for runtime benchmarks.
- Determinism: every random draw MUST come from a seeded stream or the shared random tape (section 9). No call to `random.random()` or an unseeded NumPy generator is allowed in `qs/sim` or `qs/eval`.
- Money and identity: no real card, account or personal data exists anywhere in this project. All payments are synthetic.
- Language: Python 3.10 or newer unless the LinuxONE box dictates otherwise (record the exact version in `artifacts/env_report.json`).

**Order of authority when documents disagree**

1. The safety invariants in section 15 and the claims policy in section 27 override everything.
2. This blueprint overrides the two submitted abstracts for implementation choices.
3. The submitted abstracts still limit what we may claim: anything we promised there must be delivered or explicitly explained (see the compliance table in section 26).

**Definition of done for the whole project**

- A reproducible repository with `make eval` regenerating every reported number from a fixed seed set.
- A live demo on the supplied LinuxONE environment: event ingestion, feature computation, anomaly scoring, forecasting, localisation, recommendation and decision log all running on the platform.
- An evaluation report comparing detectors and policy universes on identical seeds, with ablations and stated limitations.
- A demo video and a README that a stranger can follow to reproduce the demo in under 30 minutes.

If any instruction in this document cannot be followed (a package will not install, a spec contradicts reality), the person or agent MUST record the deviation in `docs/DEVIATIONS.md` with a one-line reason and proceed with the stated fallback rather than stalling.

## 2. Executive summary

QueueShield v2 is an operator decision-support system that detects localised payment degradation early, forecasts whether it will turn into a self-sustaining retry storm, and recommends safe, budgeted actions that a human approves.

**The problem in three sentences.** Payment failures usually start in one gateway, bank or route while the platform-wide success rate still looks healthy. Delayed confirmations trigger retries, retries add load, and the extra load causes more delays, so a small slowdown can lock into a congestion state that outlasts its cause (a metastable failure). Operators must also avoid two traps: treating a timeout as a failed payment (duplicate charges) and verifying too aggressively (a status-check flood, which NPCI linked to the three UPI outages of March to April 2025).

**What we build.** A reproducible payment-network simulator with a real retry feedback loop, a streaming detection and decision pipeline running on IBM LinuxONE, and an evaluation harness that replays identical traffic through four policy universes.

**The three innovations beyond the submitted abstract**

1. **Verification Governor (I1).** Status inquiries for unknown-outcome payments are treated as load and budgeted per payment and per segment, so the safety step cannot become the storm.
2. **Cascade Forecaster (I2).** A small model estimates, from queue depth, retry backlog and arrival pressure, the probability that a degrading segment will lock into a self-sustaining collapse if nothing changes. It turns "alert" into "act within N seconds".
3. **Fault Localizer (I3).** An explanatory-power search over gateway and bank dimensions separates gateway-side from bank-side faults and blocks rerouting when the fault sits in a shared bank, where failover only spreads the damage.

**What we claim, and how we prove it**

| Claim | Evidence we will produce | Where |
| --- | --- | --- |
| Segment-level detection beats platform-wide and fixed-threshold monitoring | Detection delay and missed incidents at a matched false-alert budget, identical seeds | Section 17 |
| Surges do not raise false alarms | False alerts per normal hour on surge replays | Section 17 |
| Forecasting cascade risk gives usable lead time | AUC, calibration and lead time versus a queue-threshold alert | Section 13 |
| Governed verification avoids a status-check storm | Peak status-check rate and recovery time in scenario S6 versus naive verify | Section 12 |
| Localisation prevents harmful failover | Wrong-reroute count in scenario S3 | Section 14 |
| Policies reduce harm in simulation | Paired universes: peak queue, excess retries, recovery time, unique successful payments | Section 17 |
| The core workload runs on LinuxONE | Measured p50, p95 and p99 event-to-recommendation latency and events per second | Section 18 |

**Win thesis.** Most teams will ship an anomaly detector and a dashboard. We ship a closed-loop story judges can see: three detectors racing, a cascade gauge crossing its threshold, an operator approving a budgeted action, and the naive universe collapsing next to ours on the same random seed. Every number is simulation-validated and labelled as such, which keeps the work credible under questioning.

**Scope guard.** No live payment integrations, no autonomous money movement, no large language models, no claim of Telum acceleration, Machine Learning for z/OS or Secure Execution unless demonstrated on the supplied environment.

## 3. The problem statement

The problem is that payment-service degradation is detected late and amplified by well-meaning retries, while operators lack a safe, evidence-backed way to intervene.

### 3.1 Degradation starts small and hides in averages

A payment platform routes traffic through gateways to banks. A fault rarely hits everything at once: one bank slows down, one gateway loses capacity, or one route develops timeouts. Because the healthy majority of traffic keeps succeeding, the platform-wide success rate barely moves, so dashboards and fixed alert thresholds stay quiet while a specific group of users suffers.

Industry sources describe the same pattern: partial drops are masked by overall numbers, and users fail long before a dashboard spike reaches an engineer. Latency and timeouts usually rise before the success rate does.

### 3.2 The retry feedback loop

When a confirmation is delayed, the customer or the calling application retries. Each retry consumes capacity that fresh payments need.

1. A segment slows down, so responses exceed the client timeout.
2. Clients retry, so offered load rises above the number of payments the customers actually intend.
3. The queue grows, so waiting time rises, so more attempts time out.
4. More timeouts produce more retries. Work already in the queue for clients that gave up is still served (wasted work), which lowers useful throughput further.

The ratio of retry attempts to fresh attempts is the Retry Amplification Factor (RAF). A rising RAF together with a growing queue is the signature of a retry-driven loop.

### 3.3 Metastable failure: the problem outlasts its cause

Systems research calls this class metastable failure: a temporary trigger (a load spike, a short capacity loss) pushes the system into a degraded state that a sustaining effect keeps alive after the trigger is gone. A study of public cloud incidents found retry policy to be the most common sustaining effect, present in over half of the incidents examined. Whether a trigger tips the system depends on its size and duration, so the same short slowdown can be harmless one minute and catastrophic the next.

The practical consequence is a point of no return. Below a certain combination of queue length and retry backlog the system drains by itself; above it, the loop sustains itself until someone intervenes. Recent work from AWS and the Max Planck Institute shows this boundary in a queue-versus-retry-backlog picture and states that turning such analysis into runtime prediction and actionable intervention is still open. That gap is where QueueShield sits.

### 3.4 A timeout is not a failure

A client that sees no response does not know whether the payment succeeded. The request may still be queued, may complete after the client gave up, or may have completed with the confirmation lost. Blindly retrying a payment in that state risks charging the customer twice.

The safe procedure is to mark the payment as unknown, ask for its status using its idempotency key, and only re-attempt after a confirmed failure. The rule is simple to state and easy to get wrong under load.

### 3.5 The verification trap (real evidence from India)

The safe procedure has a hidden cost: every status inquiry is itself load. NPCI attributed three UPI outages between March and April 2025 to a very high number of check-transaction-status calls arriving at high rates, then restricted the behaviour: the first status check only after 90 seconds, and at most three checks within two hours of the original transaction. A design that says "always verify before retrying" without budgeting the verification can recreate the outage it is meant to prevent.

### 3.6 Industry reality: reactive routing on success rate

Large Indian gateways route with live success-rate signals. Cashfree describes a bandit-based router that keeps a small trickle of traffic on weak gateways to detect recovery, and warns that shifting traffic too fast overloads other gateways because gateways share the same issuing banks, which spreads an outage instead of containing it. Razorpay reports a production routing model with a 4 to 6 percent success-rate improvement. These systems optimise where to send the next payment; they do not forecast whether a degraded segment will lock into a retry storm, and they do not treat status checks as load.

### 3.7 Who is affected

| Group | Pain today | What they need |
| --- | --- | --- |
| Payment operations and SRE teams | Late, ambiguous alerts; unclear which segment is at fault; fear of making it worse | Early, localised, explainable alerts with safe, bounded actions |
| Merchants and service managers | Failed or delayed customer payments, support load | Fewer failed attempts, clear incident history |
| Customers and small businesses | Delays, duplicate debits, uncertainty about whether they paid | Timely confirmation, no double charges |

### 3.8 The formal problem we solve

Given a stream of timestamped payment events with gateway and bank identifiers, within seconds of onset decide: (a) is a segment degrading, (b) which segment and at which level (gateway, bank or route), (c) will it become self-sustaining if nothing changes, and (d) which proportionate, status-safe actions are eligible, all without treating an uncertain payment as a failure and without adding load through the safeguards themselves.

### 3.9 Non-goals

- Predicting outages before any degradation exists. We detect after onset and forecast cascade, and we never call that prediction of failure onset.
- Fraud detection, credit risk or anything that needs personal data.
- Autonomous money movement or re-routing without human approval.
- Proving production savings. Benefits are reported as simulation outcomes.

## 4. Hackathon context, constraints and assumptions

We are competing in IBM Z Datathon 2026, Theme 1 (Real-Time AI for Critical Decisions), as a 24-hour build, and the team already submitted two abstracts proposing QueueShield.

**What we know from the submitted abstracts**

| Item | Commitment already made |
| --- | --- |
| Theme | Theme 1: Real-Time AI for Critical Decisions |
| Problem | Localised payment-service degradation and retry overload; a timeout must never trigger an unverified repeat payment |
| Platform | Event receiver, rolling features, model inference and decision log to run on the supplied IBM LinuxONE environment |
| Stack named | Python event generator, FastAPI ingestion and scoring, scikit-learn Isolation Forest, SQLite, Streamlit dashboard (package availability to be verified first) |
| Baselines | Fixed thresholds and an adaptive statistical detector |
| Simulator | Three gateways with independently varied arrivals, capacity, retry delays and recovery times; incident start times and segments kept as ground truth |
| Public data | One Numenta Anomaly Benchmark subset, reported separately; AIOps 2020 and Loghub OpenStack only if time allows |
| Safeguards | Human approval for every action; degraded-monitoring state when data is stale or the model fails; no live credentials or real money |
| Honesty | No Telum, Machine Learning for z/OS or Secure Execution claims unless available and demonstrated; no accuracy or savings figures claimed at proposal stage |

**What we do not know yet (confirm in the first hour)**

- [ ] How the LinuxONE environment is accessed (LinuxONE Community Cloud virtual server, a dedicated instance, OpenShift) and for how long.
- [ ] CPU architecture details and accelerator flags (run the environment check in section 18).
- [ ] Whether the machine has outbound internet access for `pip` and `apt`, or needs an internal mirror.
- [ ] Whether Docker or Podman is allowed and whether IBM AI Toolkit containers can be pulled.
- [ ] The judging rubric and weightings (innovation, IBM platform use, demo, feasibility, impact).
- [ ] The exact submission format: repository link, video length limit, report length, slide deck.
- [ ] Team size and who is available for how many of the 24 hours.
- [ ] Whether mentors can answer platform questions during the event.
- [ ] Whether the datathon supplies any datasets we are expected to use.

**Working assumptions until answered**

1. The box is an s390x Linux virtual server with root or sudo access and a working Python, reached over SSH, with at least 4 GB of memory.
2. Telum on-chip AI acceleration is not available. A community post describes the Community Cloud as LinuxONE III, which as far as we know predates Telum, so treat acceleration as unavailable until the environment check shows otherwise.
3. Some Python packages (for example `pyarrow`, `scikit-learn`, `onnxruntime`) may lack prebuilt s390x wheels. The package matrix and fallbacks in section 18 handle this.
4. s390x is big-endian. Never move pickled models or binary arrays between a laptop and the box without a round-trip load test.
5. We have between 3 and 6 people. Section 22 gives a split for 3, 4 and 5 or more.

**Constraints that shape every decision**

- Time: 24 hours total, including video and submission. The plan reserves the last 3 hours for evaluation freeze, writing and recording.
- Reproducibility: results must regenerate from seeds so that a judge can rerun them.
- Honesty: simulation results are never presented as production evidence. Public benchmark results are never presented as payment results.
- Platform: the core path (ingest, features, scoring, forecasting, governor, logging) must run on the supplied platform; only the offline evaluation and figure generation may run elsewhere, and the report must say which was which.

## 5. Solution overview: QueueShield v2

QueueShield v2 watches every gateway, bank and gateway-bank route against its own baseline, tells operators which segment is degrading and whether it is about to lock into a retry storm, and proposes budgeted, status-safe actions that only a human can approve.

### 5.1 Design principles

1. **Localise before you act.** Every alert names a segment and a level (gateway, bank or route). Averages hide the problem.
2. **Leading indicators over success rate.** Latency, timeouts, retry ratio, pending count and queue growth move before the success rate does.
3. **Separate surge from degradation.** A demand surge raises arrivals with stable latency and a queue that drains. Degradation raises latency and timeouts together with queue growth.
4. **A timeout is an unknown, not a failure.** Unknown-outcome payments are resolved by status inquiry before any re-attempt.
5. **Safeguards are load.** Status checks, retries and rerouted traffic all consume capacity and are budgeted.
6. **Right-size the response.** Prefer the least disruptive action that works: slow retries first, verify pending payments, shift traffic last and only gradually.
7. **Human in the loop.** The system recommends. A person approves or rejects. Nothing moves money or traffic on its own.
8. **Honest evaluation.** Identical seeds, held-out incident configurations, a fixed false-alert budget and paired comparison. Simulation results are labelled as such.

### 5.2 Capability map

| Capability | What it does | Where specified |
| --- | --- | --- |
| Event ingestion | Validates, deduplicates and event-time orders payment events | Section 8 |
| Feature engine | Rolling per-segment statistics over short and long windows | Section 10 |
| Detection stack | Baselines, rule gates and a multivariate anomaly model with persistence | Section 11 |
| Verification Governor (I1) | Budgets status inquiries and gates retries on confirmed status | Section 12 |
| Cascade Forecaster (I2) | Probability that a segment becomes self-sustaining if nothing changes | Section 13 |
| Fault Localizer (I3) | Gateway-side versus bank-side versus route-specific attribution | Section 14 |
| Policy engine | Maps evidence to eligible, ranked, bounded actions with reason codes | Section 15 |
| Operator console | Evidence, recommendation, approve or reject, decision log | Section 16 |
| Simulator and arena | Reactive retry-loop network and four policy universes on one seed | Sections 9 and 17 |

### 5.3 What QueueShield is and is not

| It is | It is not |
| --- | --- |
| A detector and decision-support tool for operators | An autonomous router or payment processor |
| A forecaster of whether a degradation becomes self-sustaining | A predictor of failure before any degradation exists |
| A policy advisor with explicit eligibility rules | A black box that changes traffic on its own |
| Evaluated in a reactive simulator and on one public benchmark | Validated on real bank traffic |
| Built to run its core workload on IBM LinuxONE | A claim that Telum acceleration or ML for z/OS is in use |

### 5.4 Operating states

The system is always in exactly one of these per segment and one globally.

| State | Meaning | Allowed output |
| --- | --- | --- |
| `MONITORING` | Healthy and receiving fresh data | Scores only |
| `WARNING` | Persistent deviation with corroborating signals | Alert and recommendations |
| `CRITICAL` | Warning plus cascade probability at or above the critical threshold | Alert, priority recommendations, countdown |
| `RECOVERING` | An approved action is active and indicators are improving | Monitoring of the action, rollback conditions |
| `DEGRADED_MONITORING` | Data stale, volume too low or model failure | "Monitoring impaired" and operator review only; no action recommendations beyond hold and escalate |

### 5.5 The three-detector race (the demo idea)

The same event stream feeds three detectors: a fixed-threshold monitor on platform-wide success rate (D0), an adaptive per-segment statistical detector (D2), and the QueueShield detector (D3). The dashboard shows each detector's first-alert time on a shared timeline. The visual argument is immediate: the fixed threshold stays quiet, the adaptive detector fires late, QueueShield fires early and names the right segment.

## 6. How it works end to end

Every payment event flows through one deterministic pipeline, and every five seconds the pipeline turns the latest windows into a verdict per segment, a cascade probability and a ranked list of eligible actions.

### 6.1 The per-event path

1. The simulator (or a replay file) emits an event such as `ATTEMPT_OUTCOME` with gateway, bank, payment, attempt, response time and queue snapshot.
2. `POST /v1/events` receives a batch (up to 500 events) and validates it against the schema.
3. The ingest layer rejects duplicates by `event_id`, orders events by `event_timestamp` using a 2-second lateness watermark, and drops or flags malformed events without crashing.
4. The payment state machine updates the payment: fresh, retry, pending-unknown, confirmed success, confirmed failure, or escalated for manual reconciliation.
5. Rolling windows (10 s and 60 s) update counters, latency reservoirs and queue observations for each segment level: platform, gateway, bank and gateway-bank route.
6. Raw events and state changes are appended to SQLite in batches, off the hot path.

### 6.2 The per-tick path (every 5 seconds of simulator time)

1. **Features** are computed per segment from the windows (section 10).
2. **Baselines** compare each feature to the segment's own recent normal value (section 11).
3. **Detection** scores each eligible segment, applies persistence and a surge-versus-degradation gate, and opens, updates or closes incidents.
4. **Localizer** attributes the excess bad outcomes to gateway, bank or route (section 14).
5. **Forecaster** estimates the probability that the segment becomes self-sustaining if nothing changes (section 13).
6. **Governor** decides which pending payments may be status-checked now, within budget (section 12).
7. **Policy engine** produces ranked recommendations with reasons, safety checks and rollback conditions (section 15).
8. The dashboard receives a snapshot over server-sent events. A human approves or rejects. The approved action is queued for the simulator, which applies it and the effect shows up in later ticks.

### 6.3 Worked example: retry storm on gateway G1 (scenario S4)

Setup: three gateways at about 55 percent utilisation, naive clients (immediate retries, up to five attempts, no idempotency check), no intervention except what QueueShield recommends. At t = 120 s gateway G1 loses 65 percent of its capacity for 75 seconds. The times below are illustrative; section 17 measures the real ones.

| t (s) | What happens in the network | What QueueShield sees | What QueueShield outputs |
| --- | --- | --- | --- |
| 0 to 120 | Normal traffic, RAF about 1.02 | Stable features, anomaly scores low | State `MONITORING` for all segments |
| 120 | G1 capacity drops; service slows | Nothing yet (events lag) | None |
| 125 | Queue on G1 starts to grow | p95 latency on G1 up, queue growth positive | Scores rising, below alert level |
| 130 | Latency about 3x baseline; first timeouts | Three of the last four windows above threshold with two corroborating signals | `WARNING` on G1. Localizer: gateway-side (about 90 percent of excess on G1, flat across banks). Platform success rate still above 90 percent, so the fixed monitor is silent |
| 140 | Retries begin to stack on top of fresh traffic | RAF 1.3 and rising, pending count growing | Forecaster: probability of lock-in 0.35. Adaptive per-segment baseline (D2) fires here |
| 145 | Queue delay approaches the client timeout | RAF 1.9, queue growth accelerating | `CRITICAL`. Forecaster 0.62. Recommendations listed, with a countdown |
| 165 | Operator approves (20 s modelled delay) | Decision recorded with operator, time, reasons | Actions applied: throttle retries on G1, shed expired requests, governed verification, 25 percent traffic step to G2 and G3 |
| 170 to 195 | Retry load falls; queue drains while trigger persists | RAF falls toward 1.1, forecaster falls | State `RECOVERING` |
| 195 | Trigger ends; capacity returns | Goodput climbing | Rollback conditions checked; ramp-back of shifted traffic in small jittered steps |
| 215 | Goodput at baseline | Six clean windows in a row | Incident `CLOSED`; decision log entry completed |
| Same seed, universe U0 | No action: queue stays full after the trigger ends, goodput below 30 percent for minutes | Same alerts, nobody acts | This is the comparison the arena measures |

### 6.4 Why the two safeguards matter in that example

- Without the Governor, the 1,000 or more pending payments created during the slowdown would be status-checked immediately and repeatedly, adding load to the very segment that is recovering (section 12).
- Without the Localizer, a bank-side fault would look the same on a gateway dashboard and the tempting response, shifting traffic to the other gateways, would push the same slow bank traffic through all three gateways (section 14).

## 7. System architecture

QueueShield is one Python service on LinuxONE made of eight components that run in a fixed order, fed by a reactive simulator and closed by a human operator.

&#91;embedded content: architecture · 8 components, 1 human loop\]

Events enter the ingest API and pass through the numbered components, top to bottom, and surface as ranked recommendations; nothing changes in the simulator until an operator approves. The Governor's due-check list and the forecaster and localizer outputs all feed the policy engine.

### 7.1 Process model

| Process | Role | Notes |
| --- | --- | --- |
| `qs-core` | FastAPI app with the ingest endpoint, the tick loop, the policy engine, the store and the SSE stream | One process for the demo. Shard by gateway across processes only if the benchmark shows a single core saturating |
| `qs-sim` | Live simulator posting events and polling for approved actions | May run on the same box or a second machine; always talks over HTTP so the ingest path is exercised |
| Browser | Static dashboard served by `qs-core` | Reached directly or through an SSH tunnel if the box is not exposed |
| Batch mode | No processes; the evaluation harness calls `Pipeline` and the simulator as libraries | Used for datasets, tuning and all reported evaluation numbers |

Both modes use the same `Pipeline` class, so the code that is evaluated is the code that is demonstrated.

### 7.2 Clocks and scheduling

- In batch mode the simulator clock drives everything and the tick fires when the event-time watermark passes each 5-second boundary.
- In live mode the simulator clock runs at wall-clock speed times a speed factor (1x, 2x or 4x), and the tick fires from the same clock.
- The tick never sleeps or waits on the network. Anything slow (storage, SSE delivery) happens on separate tasks with bounded buffers.

### 7.3 Failure isolation

- Model scoring is wrapped. An exception or a 50 ms overrun switches the segment to rule signals and shows `DEGRADED_MONITORING`.
- Storage never blocks ingest. Writes go through a bounded queue of 50,000 rows; if it fills, the oldest rows are dropped and a metric counts them.
- A disconnected dashboard client is ignored. A crash in the simulator is visible as missing events and triggers degraded monitoring after 10 s.
- Write endpoints require the API key (section 16.5).

### 7.4 Deployment notes

- Bind `uvicorn` to `127.0.0.1:8000` by default and use an SSH tunnel for the browser. Open the port externally only if the environment allows it and the key is set.
- Run services under `systemd --user` if available, otherwise in named `tmux` sessions (`qs-core`, `qs-sim`) recorded in the README.
- Keep the SQLite file on local disk with owner-only permissions.

### 7.5 Architecture rules

1. `qs/core` is deterministic: the same event stream and configuration produce the same alerts and recommendations.
2. Ground truth never crosses the schema boundary into `qs/core` or the forecaster at inference.
3. Every output record carries the model and rule versions that produced it.

## 8. Data model and event schema

Every event is a small JSON object with identifiers, timing, outcome and a service-state snapshot, and no personal data of any kind.

### 8.1 Event envelope (schema version 1)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | int | yes | Always `1` for now |
| `event_id` | string | yes | Globally unique, used for deduplication. Deterministic in the simulator: `{run_id}-{counter}` |
| `event_type` | enum | yes | See section 8.2 |
| `payment_id` | string | yes | One intended payment; links all attempts |
| `attempt_id` | string | yes | One attempt; `{payment_id}-a{n}` |
| `attempt_no` | int | yes | 0 for the fresh attempt, 1 and up for retries |
| `idempotency_key` | string | no | Opaque key; present when the client or platform attached one |
| `gateway_id` | string | yes | `G1`, `G2`, `G3` |
| `bank_id` | string | yes | `B1` to `B5` |
| `event_timestamp` | int (ms) | yes | When it happened, simulator clock |
| `received_timestamp` | int (ms) | no | When the platform received it; set by ingest |
| `response_time_ms` | int | no | Present on outcome events; time from send to outcome |
| `status` | enum | no | `SUCCESS`, `DECLINED`, `REJECTED_OVERLOAD`, `CLIENT_TIMEOUT`, `PENDING`, `UNKNOWN`, `CONFIRMED_SUCCESS`, `CONFIRMED_FAILED` |
| `error_code` | string | no | Short code such as `E_OVERLOAD`, `E_BANK_TIMEOUT`, `E_STATUS_UNREACHABLE` |
| `retry_count` | int | yes | Number of earlier attempts for this payment |
| `is_retry` | bool | yes | `retry_count > 0` |
| `queue_depth` | int | no | Gateway queue length at the event |
| `active_requests` | int | no | Gateway workers busy |
| `configured_capacity` | int | no | Gateway worker count |
| `client_timeout_ms` | int | no | Client's timeout for this attempt if the gateway can see it |

Response time and final status are only usable after the outcome event arrives. An attempt and its outcome are separate events. Never write a payment amount, account, card or personal identifier.

```json
{
  "schema_version": 1,
  "event_id": "run42-0001837",
  "event_type": "ATTEMPT_OUTCOME",
  "payment_id": "p-000912",
  "attempt_id": "p-000912-a1",
  "attempt_no": 1,
  "idempotency_key": "ik-000912",
  "gateway_id": "G1",
  "bank_id": "B3",
  "event_timestamp": 183450,
  "response_time_ms": 3000,
  "status": "CLIENT_TIMEOUT",
  "error_code": "E_CLIENT_TIMEOUT",
  "retry_count": 1,
  "is_retry": true,
  "queue_depth": 311,
  "active_requests": 40,
  "configured_capacity": 40
}
```

### 8.2 Event types

| `event_type` | Emitted when | Key fields |
| --- | --- | --- |
| `PAYMENT_INITIATED` | A new payment begins | payment, bank, gateway chosen |
| `ATTEMPT_SENT` | An attempt reaches the gateway | attempt, retry\_count, queue snapshot |
| `ATTEMPT_OUTCOME` | The attempt ends (success, decline, overload rejection, or client timeout) | status, response\_time\_ms, error\_code |
| `RETRY_SCHEDULED` | A client schedules a retry | retry\_count, delay\_ms |
| `STATUS_CHECK_SENT` | A status inquiry is issued | payment, check\_no |
| `STATUS_CHECK_RESULT` | The inquiry returns | status (`CONFIRMED_SUCCESS`, `CONFIRMED_FAILED`, `UNKNOWN`), error\_code |
| `SERVICE_SNAPSHOT` | Every 1 s per gateway | queue\_depth, active\_requests, configured\_capacity |
| `ACTION_APPLIED` | An approved action takes effect in the simulator | action id, parameters |

### 8.3 Payment state machine (platform view)

The platform tracks each payment by `payment_id`. The state is derived only from events, never from simulator ground truth.

| State | Entered when | Leaves to |
| --- | --- | --- |
| `INITIATED` | `PAYMENT_INITIATED` | `IN_FLIGHT` |
| `IN_FLIGHT` | `ATTEMPT_SENT` | `CONFIRMED_SUCCESS`, `CONFIRMED_FAILED`, `PENDING_UNKNOWN` |
| `PENDING_UNKNOWN` | `CLIENT_TIMEOUT` with no confirmed outcome | `STATUS_CHECKING` |
| `STATUS_CHECKING` | Governor releases a check (section 12) | `CONFIRMED_SUCCESS`, `CONFIRMED_FAILED`, back to `PENDING_UNKNOWN` (still unknown), `ESCALATED_RECONCILE` |
| `CONFIRMED_FAILED` | Declined, or status says failed | `IN_FLIGHT` (re-attempt allowed), `ABANDONED` |
| `CONFIRMED_SUCCESS` | Success or status says success | Terminal |
| `ESCALATED_RECONCILE` | Check budget exhausted while still unknown | Terminal in simulation; operator reconciliation list |
| `ABANDONED` | Client gave up after the retry cap | Terminal |

Invariant: a payment in `PENDING_UNKNOWN` or `STATUS_CHECKING` MUST NOT receive a new attempt event from the platform's own retry logic. Violations are counted as safety violations in the test suite and in the evaluation.

### 8.4 Segment keys and levels

A segment key is a tuple `(level, gateway_id, bank_id)` with `*` for the wildcard dimension.

| Level | Key example | Used for |
| --- | --- | --- |
| Platform | `(platform,*,*)` | Fixed-threshold baseline D0 and context |
| Gateway | `(gateway,G1,*)` | Gateway-side faults; capacity and queue |
| Bank | `(bank,*,B3)` | Bank-side faults |
| Route | `(route,G1,B3)` | Most specific; low volume, so a minimum-sample rule applies |

### 8.5 Ingest rules

- Reject (HTTP 422 per event, batch continues) any event missing required fields or with unknown enum values. Count rejects in a metric.
- Deduplicate by `event_id` using an in-memory LRU of the last 200,000 ids plus a unique index in SQLite.
- Order by `event_timestamp` with a watermark of 2,000 ms: events later than the watermark are processed but flagged `late` and excluded from the latency windows they missed.
- If no event has arrived for 10 s of simulator time, the platform enters `DEGRADED_MONITORING` globally.

### 8.6 Persistence tables (SQLite, WAL mode)

| Table | Purpose | Key columns |
| --- | --- | --- |
| `events` | Raw accepted events | `event_id` (unique), `event_type`, `payment_id`, timestamps, `payload_json` |
| `payments` | Latest state per payment | `payment_id`, `state`, `attempts`, `status_checks`, `updated_ts` |
| `segment_features` | Per tick per segment feature vector | `tick_ts`, `segment_key`, features JSON, `anomaly_score`, `p_stuck` |
| `incidents` | Opened and closed incidents | `incident_id`, `segment_key`, `state`, `opened_ts`, `closed_ts`, `severity` |
| `recommendations` | Proposed actions | `rec_id`, `incident_id`, `action`, `params_json`, `reasons_json`, `confidence`, `status` |
| `decisions` | Operator decisions | `rec_id`, `decision`, `operator`, `decided_ts`, `note` |
| `actions_applied` | What the simulator did | `rec_id`, `applied_ts`, `params_json`, `result` |
| `model_registry` | Model and rule versions | `model_name`, `version`, `sha256`, `trained_on_seed_set`, `created_ts` |
| `runs` | One row per simulation or evaluation run | `run_id`, `scenario`, `seed`, `arm`, `config_json` |

The hot path keeps rolling state in memory and writes to SQLite in batches of up to 500 rows or 250 ms, whichever comes first.

## 9. Simulator specification

The simulator is a reactive discrete-event model of three gateways, five banks and a population of retrying clients, built so that a recommended action actually changes what happens next.

A replay of fixed events cannot show that throttling retries reduces a queue, because nothing responds. The simulator must therefore contain the retry feedback loop, wasted work on abandoned requests, shared bank dependencies and a status-check service that consumes capacity. It is a qualitative model for comparing policies on identical traffic, not a calibrated model of any real gateway, and every report must say so.

### 9.1 Topology and entities

- **Gateways G1, G2, G3.** Each has `c` workers (parallel service slots) and a bounded queue of size `Qmax`. A gateway holds a worker for the full service time, including the bank's response time.
- **Banks B1 to B5.** A bank adds a latency component to every attempt routed to it. A bank slowdown therefore holds workers on every gateway that sends it traffic, which is how bank-side faults produce gateway-side symptoms.
- **Clients.** One client per payment. A client sends an attempt, waits up to the client timeout, then either accepts the outcome or retries according to its profile.
- **Routing.** Each new attempt picks a gateway from the current routing weights and a bank from the payment's bank. Weights default to one third each and can be changed by the `shift_traffic` action.
- **Status service.** A status inquiry is a small request that passes through the same gateway queue with a cost of about a quarter of a normal request.

### 9.2 Lifecycle of one payment

1. A payment arrives from a Poisson process with a time-varying rate and picks a bank by the traffic shares.
2. The client sends attempt 0. The router picks a gateway.
3. If the gateway queue is full, the attempt is rejected at once (`REJECTED_OVERLOAD`, about 5 ms). Otherwise it joins the queue.
4. When a worker frees up, the attempt starts service. Service time is the gateway part plus the bank part, each drawn from a lognormal distribution and scaled by the multipliers active at that moment.
5. If the client timeout elapses first, the client records `CLIENT_TIMEOUT`. **The attempt is not cancelled.** By default the gateway still serves it and, with probability `1 - decline_rate`, commits the payment. This is the wasted work that sustains metastability, and it is also how a timed-out payment can succeed later.
6. On completion the attempt ends as a commit (money moved) or a decline. Late completions after a client timeout are real commits the client never saw.
7. After a timeout, rejection or overload response, the client retries with probability `p_retry` after a delay drawn from its profile, up to the attempt cap, otherwise the payment is `ABANDONED`.
8. **Duplicate charge** = the number of committed attempts for one payment minus one, when it is greater than zero. This is ground truth available only to the evaluator.

### 9.3 Client profiles

| Profile | Retry delay | Attempt cap | Idempotency key | Status check before retry | Used in |
| --- | --- | --- | --- | --- | --- |
| `naive` | Uniform 500 to 2,000 ms, no growth | 5 | No | No | U0, U1, U3 baseline population |
| `disciplined` | Full-jitter exponential, base 1,000 ms, cap 16,000 ms | 3 | Yes | After a timeout, via the Governor | U2 and the QueueShield-governed path |
| `naive_verify` | As `naive` | 5 | No | Yes, every 1 s until resolved, up to 60 times | Scenario S6 baseline |

The client population in U3 is still mostly `naive`. QueueShield changes platform behaviour (throttling, shedding, verification, routing), because operators cannot instantly rewrite every client.

### 9.4 Server-side controls that actions can switch on

| Control | Effect in the simulator | Cost or side effect |
| --- | --- | --- |
| `retry_throttle(segment, rate_fraction, retry_after_ms)` | Token bucket for attempts with `retry_count > 0`. Over-budget retries get an immediate `E_RETRY_LATER` response; 70 percent of clients honour a jittered Retry-After, the rest use their normal delay. Throttled attempts count toward the attempt cap | Some payments abandon earlier |
| `shed_expired(gateway)` | A request whose age exceeds its client timeout is dropped when it reaches the front of the queue (1 ms cost). The attempt ends as a definitive non-commit | None for payments, removes wasted work |
| `fresh_first(gateway)` | Strict priority for fresh attempts with a starvation guard (a retry waiting longer than 2 s is served next) | Retries wait longer |
| `shift_traffic(from, fraction, ramp)` | Lowers the routing weight of a gateway in jittered steps of at most 25 percentage points, keeping a 5 percent trickle | Target gateways take extra load |
| `govern_verification(segment)` | Enables the Verification Governor (section 12) for that segment | Pending payments wait longer for a first check |

### 9.5 Random tape and common random numbers

Every policy universe for a given seed MUST consume the same random numbers. For each seed, generate a tape once:

- Arrival times from a thinned Poisson process using the scenario's rate function.
- Per payment: bank draw, decline flag, `p_retry` draws, retry-delay draws, Retry-After compliance flag, and for each possible attempt a gateway-routing uniform and two service-time quantiles (gateway part, bank part).
- Service time is `F_inverse(quantile) * multiplier(t)`, so a different policy changes when an attempt is served and by how much it is scaled, but never the underlying draw.

The tape makes paired comparison valid and removes most noise from policy differences. A test MUST assert that the first 1,000 arrival times and bank draws are identical across universes for the same seed.

### 9.6 Default parameters (starting values, to be frozen after calibration)

| Parameter | Default | Note |
| --- | --- | --- |
| Gateways, banks | 3, 5 | Bank traffic shares 0.30, 0.25, 0.20, 0.15, 0.10 |
| Workers per gateway `c` | 40 |  |
| Queue bound `Qmax` | 400 | Large enough for delay beyond the client timeout |
| Service time, gateway part | Lognormal, mean 100 ms, CV 0.5 |  |
| Service time, bank part | Lognormal, mean 300 ms, CV 0.6, times bank multiplier | Mean total about 400 ms, so capacity about 100 attempts/s per gateway |
| Fresh arrival rate | 165 payments/s total (55 per gateway) | Baseline utilisation about 0.55 |
| Client timeout | 3,000 ms | Test also 2,500 and 4,000 |
| `p_retry` | 0.9 |  |
| Hard decline rate | 2 percent | Never retried |
| Overload rejection time | 5 ms |  |
| Status check cost | 0.25 of mean service, about 100 ms |  |
| Run length | 600 s | 120 s warm-up, incident in the middle, 120 s tail |
| Tick (feature) interval | 5 s | Detection runs per tick |

### 9.7 Scenarios

| ID | Name | What changes | Ground-truth label | Purpose |
| --- | --- | --- | --- | --- |
| S0 | Normal | Nothing | No incident | False-alert rate, baseline fitting |
| S1 | Demand surge | Arrivals x1.6 for 180 s on all gateways, plus a 20 s flash spike x2.5 | Not an incident (`surge`) | Alerts here are false alarms |
| S2 | Gateway slowdown | One gateway service time x1.6 to x3.0 for 100 to 150 s | Incident, gateway level | Localised detection |
| S3 | Bank slowdown | One bank's bank-part latency x4 to x8 for 100 to 150 s, on all gateways | Incident, bank level | Shared-fault localisation; failover trap |
| S4 | Retry-storm trigger | One gateway loses 50 to 70 percent of capacity for 40 to 90 s | Incident, gateway level | Metastability, forecaster, policy arena |
| S5 | Surge plus slowdown | S1 surge on all plus S2 on one gateway, overlapping | Incident hidden inside a surge | Surge discrimination |
| S6 | Verification storm | One bank black-holes 40 percent of attempts for 60 s, resolving after 90 s | Incident, bank level | Governor versus naive verification |
| S7 | Silent drift | One route's latency ramps x1 to x2.2 over 120 s with no timeouts | Incident, route level, onset when multiplier first reaches x1.3 | Early leading-indicator detection |

Each scenario run writes `ground_truth.json` with entries `{incident_id, onset_ms, end_ms, level, gateway_id, bank_id, kind, magnitude, is_incident}`. Surge entries carry `is_incident: false` so the evaluator can score alerts raised during them as false.

### 9.8 Configuration sets and held-out rule

A configuration is a draw of scenario parameters (affected segment, onset time, magnitude, duration, baseline utilisation, client timeout, retry cap). `make_configs(set, n, seed)` builds three disjoint sets. The counts below are targets; section 17.4 gives the minimum viable counts to use if simulation turns out to be slow.

- **Set A (train):** about 100 configurations per scenario, used to fit models and baselines.
- **Set B (validation):** about 50 per scenario, used to tune thresholds and the false-alert budget.
- **Set C (test):** about 100 per scenario with segments, onset times and some magnitude ranges not seen in A or B (for example utilisation 0.65 and timeout 2,500 ms appear only here). Touched once, after the evaluation freeze.

### 9.9 Metastability calibration (mandatory before dataset generation)

The retry-storm result is only meaningful if the simulator really shows a trigger-size-dependent tipping point under naive clients.

1. Run `scripts/tune_metastability.py`. Sweep capacity multiplier `m` in {0.5, 0.4, 0.35, 0.3} and duration `d` in {20, 40, 60, 90, 120} s with the `naive` profile, no actions, at least 20 seeds each.
2. Mark a run **stuck** when goodput in the window from 60 to 120 s after the trigger ends is below 50 percent of baseline goodput.
3. **Acceptance:** at some `m` there is a short duration that recovers in at least 80 percent of seeds and a longer duration that sticks in at least 80 percent of seeds.
4. If not accepted, adjust in this order: lower the client timeout, raise the attempt cap, raise baseline utilisation to 0.6 or 0.65, raise `Qmax`. Keep `shed_expired` off by default.
5. Save the chosen parameters to `configs/sim_default.yaml`, plot the stuck-probability heat map, and tag the commit `sim-frozen`. No later change to simulator dynamics is allowed without regenerating all datasets and results.

### 9.10 Implementation guidance

- Use a plain `heapq` event loop with integer milliseconds and a sequence counter for ties. Avoid SimPy so the dependency footprint stays minimal on s390x.
- Event kinds: `ARRIVAL`, `ATTEMPT_ARRIVE`, `SERVICE_DONE`, `CLIENT_TIMEOUT`, `RETRY_DUE`, `STATUS_ARRIVE`, `STATUS_DONE`, `TICK`, `SNAPSHOT`, `ACTION_APPLY`, `TRIGGER_START`, `TRIGGER_END`.
- Two run modes share one engine: `batch` (in process, fastest, deterministic, used for evaluation and dataset generation) and `live` (emits events to the HTTP ingest endpoint and polls for applied actions, used for the demo and latency benchmark).
- The emitted event stream MUST contain only what a real platform could observe. Ground truth (which attempt truly committed) stays inside the simulator and the evaluator.

### 9.11 Simulator tests

- Determinism: the same seed and arm produce an identical event-stream hash.
- Conservation: every attempt has exactly one terminal outcome by the end of the run.
- Little's law check in S0: mean queue depth within 5 percent of arrival rate times mean waiting time.
- Utilisation in S0 is between 0.50 and 0.60 per gateway.
- The common-random-number test in section 9.5 passes.
- The metastability acceptance in section 9.9 passes before gate G1.

## 10. Feature engine

The feature engine turns the ordered event stream into one numeric row per segment every 5 seconds, using two sliding windows (10 s and 60 s) and each segment's own baseline.

### 10.1 Windows and data structures

- **Tick:** every 5,000 ms of simulator time the engine emits a feature row for every eligible segment.
- **Short window `Ws` = 10 s** captures onset; **long window `Wl` = 60 s** gives stability and trend.
- **Buckets:** keep counters in 1-second buckets in a ring buffer shaped `(segments, 120 buckets)`. A window sum is a slice sum, so updates are O(1) per event and a tick is a few vectorised NumPy operations.
- **Latency quantiles:** keep a log-spaced histogram per segment per bucket (64 bins from 10 ms to 60 s). Merge bins over the window and read p50, p95, p99. Do not store raw latencies.
- **Segments computed:** 1 platform, 3 gateway, 5 bank, 15 route, which is 24 rows per tick.
- **Late events:** an event older than the 2 s watermark updates counters only if its bucket is still in the ring; otherwise it is counted in the `late_dropped` metric.

### 10.2 Feature definitions

All ratios are computed over the stated window. "Attempts" are `ATTEMPT_SENT` events; "outcomes" are `ATTEMPT_OUTCOME` events.

| Feature | Definition | Window | Why it matters |
| --- | --- | --- | --- |
| `arrival_rate` | Attempts per second (fresh plus retry) | Ws | Load; surge indicator |
| `fresh_rate` | Attempts with `retry_count = 0` per second | Ws | True demand |
| `raf` | Retry attempts divided by fresh attempts | Ws | Retry amplification; returns 1.0 plus retry share |
| `success_ratio` | `SUCCESS` outcomes divided by all terminal outcomes | Wl | Lagging indicator, used by D0 and D1 |
| `timeout_ratio` | `CLIENT_TIMEOUT` outcomes divided by all outcomes | Ws | Leading indicator |
| `reject_ratio` | `REJECTED_OVERLOAD` plus `E_RETRY_LATER` outcomes divided by outcomes | Ws | Saturation |
| `p50`, `p95`, `p99` | Response-time quantiles of completed attempts | Ws | Latency |
| `latency_ratio` | `p95` divided by the segment baseline `p95` | Ws | Normalised latency drift |
| `pending_count` | Payments in `PENDING_UNKNOWN` or `STATUS_CHECKING` attributed to the segment of their latest attempt | instant | Uncertainty backlog |
| `pending_frac` | `pending_count` divided by attempts per second times 60 | instant | Scale-free backlog |
| `queue_depth` | Mean of gateway snapshots in the window (gateway and platform levels; route and bank levels inherit the gateway value weighted by traffic) | Ws | Congestion |
| `queue_growth` | Least-squares slope of `queue_depth` over the last 30 s, per second | 30 s | Early congestion signal |
| `utilisation` | `active_requests` divided by `configured_capacity` | Ws | Saturation |
| `queue_delay_est` | `queue_depth` divided by completions per second (Little's law) | Ws | Expected waiting time |
| `orbit_proxy` | Payments with a timeout in the last 15 s that are not yet confirmed successful, per segment | 15 s | Observable stand-in for the retry backlog |
| `status_check_rate` | `STATUS_CHECK_SENT` per second | Ws | Verification load (section 12) |
| `load_over_capacity` | `arrival_rate` divided by estimated capacity (workers divided by baseline mean service time) | Ws | Pressure against capacity |
| `expired_ratio` | Share of attempts served after their client timeout (only when `client_timeout_ms` is present) | Ws | Wasted work |

### 10.3 Per-segment baselines

Each segment keeps its own baseline for every feature so that a naturally slow bank is not flagged against a fast one.

- Baseline value is an exponentially weighted mean with smoothing `alpha = 0.02` per tick. Baseline spread is the exponentially weighted mean absolute deviation, floored at a feature-specific minimum so that a zero-variance baseline cannot produce infinite z-scores.
- **Warm start:** initialise from the first 120 s of the run; before that the segment is `warming_up` and produces no alerts. Fall back to training-set per-segment priors when warm-up data are sparse.
- **Freeze rule:** while an incident is open on a segment, its baseline is frozen. The system must not learn the anomaly as the new normal.
- **Deviation score:** `z = (x - baseline) / spread` for each feature, clipped to the range from -6 to 6.

### 10.4 Minimum samples and parent fallback

| Condition | Rule |
| --- | --- |
| Route segment with fewer than 30 attempts in `Ws` | Mark `low_volume`; do not alert on the route alone; the parent gateway and bank rows still alert |
| Any segment with no events in `Ws` | Mark `no_data`; its state becomes `DEGRADED_MONITORING` if it normally has traffic |
| Latency quantiles with fewer than 20 completions | Report `NaN`; the detector must treat `NaN` as "no information" and not as zero |

### 10.5 Model input vector

The multivariate anomaly model (section 11) receives eight scaled features per segment: `log(arrival_rate / baseline_arrival_rate)`, `timeout_ratio`, `reject_ratio`, `log(latency_ratio)`, `raf`, `queue_growth / capacity`, `queue_delay_est / client_timeout`, `pending_frac`. Scaling statistics (mean and standard deviation) are computed on training windows only and stored in the model registry.

### 10.6 Interface

```python
@dataclass
class FeatureRow:
    tick_ts: int            # ms, simulator clock
    segment: tuple          # (level, gateway_id, bank_id)
    eligible: bool
    flags: list             # e.g. ["low_volume", "warming_up", "no_data"]
    f: dict                 # feature name -> float (NaN allowed)
    z: dict                 # feature name -> clipped z-score vs own baseline

class FeatureEngine:
    def ingest(self, event: Event) -> None: ...
    def tick(self, now_ms: int) -> list[FeatureRow]: ...   # called every 5 s
```

### 10.7 Tests

- Hand-built micro streams where every feature can be computed by hand and compared with the engine output.
- Window boundary tests: an event exactly at a bucket edge lands in one bucket only.
- Ratio bounds: all ratios stay within 0 and 1 (except `raf`, which is at least 1 when fresh attempts exist).
- Freeze test: the baseline does not move while an incident is open.
- Performance: 24 segments, 5,000 events per second, tick time below 10 ms on the development machine; the same benchmark runs on LinuxONE (section 18).

## 11. Detection stack

We run four detectors on the same feature stream, tune them to the same false-alert budget, and let the QueueShield detector combine own-baseline deviations, a surge gate, a multivariate anomaly score and persistence.

### 11.1 The four detectors

| ID | Name | Rule | Tuned parameter |
| --- | --- | --- | --- |
| D0 | Platform fixed threshold | Alert when platform `success_ratio` (60 s) is below `theta0` for 2 consecutive ticks | `theta0` |
| D1 | Segment fixed thresholds | Alert on a segment when `timeout_ratio` above `a` or `success_ratio` below `b` for 2 consecutive ticks, with `a` and `b` scaled by one sensitivity knob | Sensitivity knob |
| D2 | Adaptive statistical | Per segment, CUSUM on `timeout_ratio` and on `log(latency_ratio)` against the segment's own EWMA baseline | CUSUM threshold `h` |
| D3 | QueueShield | Pipeline in section 11.2 | Model threshold `tau` |

D0 mirrors typical platform dashboards. D1 mirrors per-route static alert rules. D2 is the stronger statistical baseline promised in the abstract.

### 11.2 QueueShield detector (D3) pipeline

1. **Eligibility.** Skip segments that are `warming_up`, `no_data` or `low_volume` (section 10.4).
2. **Corroborating signals.** For each eligible segment compute five boolean signals from the z-scores:
   - `S_lat`: `z(latency_ratio)` at least 2.5.
   - `S_to`: `timeout_ratio` above baseline by at least 3 spreads and at least 0.02 absolute.
   - `S_q`: positive `queue_growth` and `queue_delay_est` above 1.5 times baseline.
   - `S_raf`: `raf` at least 1.25 times baseline.
   - `S_rej`: `reject_ratio` above baseline by at least 3 spreads and at least 0.01 absolute.
3. **Surge gate.** Label the segment `SURGE` (no alert, an informational annotation) when `z(arrival_rate)` is at least 2.5, at most one corroborating signal is true, and `queue_growth` is not positive for two consecutive ticks. A surge that drains is legitimate demand.
4. **Multivariate score.** Score the eight-feature vector (section 10.5) with the anomaly model and map it to `[0, 1]` through the empirical cumulative distribution of training scores, so 0.99 means "more unusual than 99 percent of normal training windows".
5. **Persistence.** Raise a `WARNING` when the score is at least `tau` in 3 of the last 4 ticks and at least two corroborating signals are true. Fast path: a score at or above `tau_hi` (0.999) with three or more signals for 2 consecutive ticks.
6. **Severity and confidence.** `CRITICAL` when a warning coincides with a forecaster probability at or above 0.5 (section 13). Confidence is a transparent function: `0.4 * score + 0.3 * (signals / 5) + 0.2 * persistence_fraction + 0.1 * min(1, samples / 200)`.
7. **Reason codes.** Rank features by absolute z-score and emit codes such as `TIMEOUT_UP`, `LATENCY_UP`, `RAF_UP`, `QUEUE_GROWING`, `PENDING_BACKLOG`, each with observed and baseline values. These are supporting signals, not proof of root cause, and the dashboard says so.

### 11.3 The anomaly model

- **Primary:** `sklearn.ensemble.IsolationForest` with 200 trees and `max_samples=256`, trained on normal windows only (S0, plus pre-incident ticks of every training scenario). Isolation Forest is a batch method, so the live system scores each window against a model fitted offline and refits nightly in a production setting; our demo uses the offline fit.
- **Alternative for the ablation:** Robust Random Cut Forest (`rrcf`, pure Python) or Half-Space Trees, both designed for streams.
- **Endianness rule:** train the model on the LinuxONE box (`make train`). Do not copy a pickled scikit-learn model from a little-endian laptop to the big-endian box. If a copy is unavoidable, run the round-trip load test in section 18 first.
- **Failure handling:** wrap scoring in a guard. If the model raises an error or returns `NaN`, fall back to the rule signals only, set the segment flag `model_degraded`, and show `DEGRADED_MONITORING` on the dashboard.

### 11.4 Incident lifecycle

| State | Entered when | Leaves when |
| --- | --- | --- |
| `OPEN` | A warning is raised on a segment with no open incident | An operator decision is recorded, or severity rises to `CRITICAL` |
| `MITIGATING` | An operator approves at least one action | Indicators improve for 3 ticks |
| `RECOVERING` | Indicators improving after an action | Six consecutive clean ticks (30 s), or deterioration, which returns to `MITIGATING` |
| `CLOSED` | Six clean ticks | A new warning on the same segment within 60 s reopens the same incident |

Alert deduplication: warnings on overlapping segments within 30 s merge into one incident. The incident's primary segment comes from the Fault Localizer (section 14). Platform-level alerts from D0 are never merged into localised incidents; they are reported separately as unlocalised.

### 11.5 Degraded-monitoring triggers

- No events for 10 s of simulator time (global).
- A segment that normally has traffic receives none in `Ws`.
- More than half of the model input features are `NaN`.
- The model call fails or exceeds a 50 ms budget twice in a row.
- The simulator clock jumps backwards or stalls.

In this state the dashboard says monitoring is impaired and recommends only `HOLD_MONITOR` or `ESCALATE_HUMAN`. The system never silently reports "healthy" when it cannot see.

### 11.6 Matching alerts to ground truth

| Term | Definition |
| --- | --- |
| Covering segment | An alert segment covers an incident if it equals the incident segment or is an ancestor (route inside gateway or bank, any inside platform) |
| Valid alert | The first alert whose covering segment covers the incident and whose timestamp is in the window from onset to onset plus 300 s |
| Detection delay | Valid alert timestamp minus onset |
| Missed incident | No valid alert in the 300 s window |
| False alert | An alert not overlapping any incident window (with 30 s grace), including alerts raised during surge scenarios |
| Correct localisation | The alert's primary segment equals the ground-truth segment (top-1), or is among the top 2 ranked candidates (top-2) |

### 11.7 Equal false-alert budget (fairness rule)

Every detector gets one sensitivity parameter. On validation set B (all S0 and S1 runs plus pre-incident ticks of incident runs) bisect each parameter until the detector raises no more than **1 false alert per simulated hour** of normal or surge traffic, aggregated across segments. Freeze the parameters, record them in `configs/detectors_frozen.yaml`, and only then run the test set. Detection delay comparisons are valid only at this matched budget.

### 11.8 Ablations (to prove the model is needed)

| Arm | Components | Question it answers |
| --- | --- | --- |
| R | Rule signals plus persistence, no model | Is the model needed at all? |
| M | Isolation Forest score plus persistence, no rule gate | Does the gate remove false alarms on surges? |
| RM | Rules plus Isolation Forest plus persistence (D3 default) | The full detector |
| RM-RRCF | Rules plus RRCF instead of Isolation Forest | Does a streaming model help? |
| RM-EWS | RM plus rolling variance and lag-1 autocorrelation of `queue_depth` as extra features | Do critical-slowing-down signals add value in this setting? Report honestly even if they do not |

### 11.9 Optional guaranteed alert budget (stretch goal)

Convert each segment's score to a p-value using the training score distribution, then use a sequential e-detector so the long-run false-alarm rate is controlled across the 24 segments.

```latex
e(p) = \kappa\, p^{\kappa - 1}, \quad \kappa = 0.5, \qquad R_t = (R_{t-1} + 1)\, e(p_t), \qquad \text{alert if } R_t \ge \frac{24}{\alpha}
```

The guarantee needs exchangeable, independent p-values, and consecutive windows overlap, so this assumption is violated. Report the empirically realised false-alert rate, not the theoretical bound, and call it a calibrated alert budget, not a proof.

### 11.10 Tests

- Each signal fires on a hand-built stream where it should and stays silent where it should not.
- The surge gate labels S1 surges as `SURGE` for at least 95 percent of surge ticks in set A.
- Persistence: one noisy tick never raises a warning.
- Lifecycle: reopen within 60 s keeps the same incident id.
- Degraded monitoring: cutting the event stream for 12 s triggers the state within one tick.

## 12. Innovation 1: Verification Governor

The Governor resolves unknown-outcome payments safely while treating every status inquiry as load, so the safeguard against duplicate payments cannot itself cause an overload.

### 12.1 Why it exists

The abstract's rule, "a timeout never triggers an unverified repeat payment", is correct but incomplete. If every client or back-office process asks for status immediately and repeatedly, the verification traffic can overload the segment it is trying to understand. NPCI's 2025 rules (first status check after 90 seconds, at most three checks within two hours) exist for this reason. The Governor makes that discipline explicit, measurable and adaptive.

### 12.2 Parameters

| Parameter | Simulator default | Meaning |
| --- | --- | --- |
| `first_check_delay_ms` | 10,000 | Earliest first status check after a timeout. A scaled-down analogue of the 90 s rule on UPI |
| `check_backoff_factor` | 1.5 | Spacing between checks grows by this factor, so checks land at about 10 s, 15 s and 22 s after the previous one |
| `jitter` | plus or minus 20 percent | Applied to every scheduled check |
| `max_checks_per_payment` | 3 | Hard cap within the horizon |
| `horizon_ms` | 120,000 | Window in which the cap applies |
| `segment_budget_rate` | 10 percent of estimated spare capacity, clamped to 1 to 10 checks per second | Token-bucket refill rate per gateway; shrinks automatically when the segment is saturated |
| `segment_budget_burst` | 2 seconds of refill | Bucket size |
| `breaker_open_ms` | 15,000 | Pause on a gateway after the status path itself times out or returns `E_STATUS_UNREACHABLE` |
| `stop_codes` | empty | Error codes that a rail defines as definitive. Empty by default because we never invent rail rules |

A preset file `configs/rails/upi_like.yaml` documents the real UPI numbers and the 9-to-1 time compression used in the demo. It must say that this is an analogy and not a UPI implementation.

### 12.3 Behaviour

1. A client timeout moves the payment to `PENDING_UNKNOWN`. The Governor records `timeout_ts` and schedules check number 1 at `timeout_ts + first_check_delay_ms`, with jitter.
2. On each tick the Governor lists payments whose scheduled check is due. For each gateway it releases at most as many as the token bucket allows, oldest first.
3. A released payment becomes `STATUS_CHECKING`, and a `STATUS_CHECK_SENT` event is emitted. The result is one of `CONFIRMED_SUCCESS`, `CONFIRMED_FAILED` or `UNKNOWN`.
4. `UNKNOWN` schedules the next check using the backoff factor, unless `max_checks_per_payment` is reached.
5. When the cap is reached with the payment still unknown, the payment moves to `ESCALATED_RECONCILE` and is placed on the operator's reconciliation list. No retry is allowed.
6. If the status path fails, the Governor opens a per-gateway breaker for `breaker_open_ms`. Checks that cannot be sent are rescheduled and still count against the payment's cap.

&#91;embedded content: payment states · 3 outcomes, 1 loop\]

Payments that get a normal answer never enter this path. A timed-out payment waits as unknown, is checked only when the budget allows, and can loop back for another check until the cap sends it to the reconciliation list.

### 12.4 Retry gating rules (safety invariants)

| Rule | Statement |
| --- | --- |
| R1 | A payment in `PENDING_UNKNOWN` or `STATUS_CHECKING` MUST NOT receive a new attempt |
| R2 | After `CONFIRMED_FAILED`, a retry is allowed within the attempt cap, with jittered backoff |
| R3 | After `CONFIRMED_SUCCESS`, the payment closes and no retry is allowed |
| R4 | After `ESCALATED_RECONCILE`, no retry is allowed; the customer-facing message is "payment pending, do not retry" |
| R5 | A retry that reuses the idempotency key is safe only if the gateway deduplicates on it; the simulated gateways do for the `disciplined` profile |

The simulated status service answers `CONFIRMED_SUCCESS` if any attempt of the payment committed, `CONFIRMED_FAILED` only if every attempt has definitively ended without a commit (declined, rejected or shed), and `UNKNOWN` if any attempt is still queued or in service.

### 12.5 Scenario S6 and baselines

In S6 one bank black-holes 40 percent of attempts for 60 s and resolves them 90 s later. Thousands of payments become pending.

| Arm | Verification behaviour |
| --- | --- |
| Naive verify | Check every 1 s until resolved, up to 60 checks |
| No verification | Clients retry blindly (duplicate-prone) |
| Governed (QueueShield) | Schedule and budget as above |

Report for each arm: peak `status_check_rate`, peak queue depth on the affected gateways, share of gateway capacity consumed by status checks at peak, time to recovery of goodput, median and 95th-percentile time to resolve pending payments, number escalated for reconciliation, and duplicate commits. Hypothesis P5 is that naive verification amplifies the incident, while governed verification trades slower resolution for lower load; report the result whichever way it falls. State the trade-off honestly in the report.

### 12.6 Interface

```python
class VerificationGovernor:
    def __init__(self, cfg: GovernorConfig): ...
    def on_timeout(self, payment_id: str, gateway_id: str, now_ms: int) -> None: ...
    def on_status_result(self, payment_id: str, status: str, now_ms: int) -> None: ...
    def due_checks(self, now_ms: int, gateway_state: dict) -> list[str]: ...   # payment_ids to check now
    def can_retry(self, payment_id: str) -> bool: ...                          # enforces R1 to R4
    def stats(self) -> dict: ...                                                # rates, bucket levels, escalations
```

### 12.7 Tests

- Property test (random event streams): the number of checks for any payment never exceeds `max_checks_per_payment`, and the per-gateway check rate never exceeds the bucket's allowance.
- `can_retry` is false for every payment in `PENDING_UNKNOWN`, `STATUS_CHECKING` and `ESCALATED_RECONCILE`.
- With the naive-verify profile on S6, peak `status_check_rate` is reported next to the governed peak; if the ratio is below 2, review the S6 calibration before continuing.
- Breaker test: an unreachable status path pauses checks for the configured time and never loses a payment from the reconciliation list.

## 13. Innovation 2: Cascade Forecaster

The Cascade Forecaster estimates, every tick and for every gateway, the probability that the current queue and retry backlog will lock into a self-sustaining collapse if nothing changes.

### 13.1 Purpose and honest claim

An alert says something is wrong. The forecaster says how urgently to act. The claim is limited and testable: in our simulator, a small model fed observable queue and retry-backlog features forecasts lock-in earlier and with fewer false alarms than a plain queue-threshold alert. It forecasts the cascade, not the onset of the fault, and the probability is conditional on the simulator's dynamics and on no intervention. The idea follows the queue-versus-retry-backlog state picture used in recent AWS and Max Planck work on metastable failures, which they use for design-time analysis; we learn the same boundary from Monte Carlo simulation and use it at runtime.

### 13.2 Definitions

| Term | Definition |
| --- | --- |
| Trigger | The scenario's temporary fault (S4 capacity loss) with start `t_s` and end `t_e` |
| Stuck run | A run whose goodput in the window from `t_e + 60 s` to `t_e + 120 s` is below 50 percent of baseline goodput, with no action taken |
| Collapse time `t_c` | First tick where `queue_delay_est` is at least the client timeout `T` and stays so for 3 ticks |
| Forecast alarm time `t_f` | First tick where `p_stuck` is at least the critical threshold |
| Lead time | `t_c - t_f`; positive means early. Only defined for stuck runs |

### 13.3 Dataset generation (`scripts/make_cascade_dataset.py`)

1. Draw 600 or more configurations from set A: capacity loss `m` in 0.3 to 0.6, duration 20 to 150 s, baseline utilisation 0.45 to 0.65, client timeout 2,500 to 4,000 ms, attempt cap 3 to 6, retry delay profile (fixed or uniform).
2. Run each configuration in `batch` mode with the `naive` profile and no actions, so the label reflects the system's natural dynamics.
3. For the affected gateway, log one sample per tick from `t_s` to `t_e + 60 s`: the feature vector in 13.4, the run's `stuck` label, the run id and the configuration id.
4. Aim for roughly 40 to 60 percent stuck runs by oversampling configurations near the boundary found in section 9.9. Keep class weights in the loss, not a resampled test set.
5. Split by configuration, never by tick: all samples of one run stay in the same split. Validation uses set B and test uses set C.

### 13.4 Features (all observable from events)

| Feature | Definition |
| --- | --- |
| `q_frac` | `queue_depth / Qmax` |
| `delay_frac` | `queue_delay_est / client_timeout` |
| `orbit_frac` | `orbit_proxy / gateway_capacity_per_second` (retry backlog proxy from section 10) |
| `load_ratio` | `arrival_rate / estimated_capacity` |
| `raf` | Retry amplification factor |
| `timeout_ratio` | Share of attempts timing out |
| `d_q`, `d_orbit` | Change of `q_frac` and `orbit_frac` over the last 15 s |
| `expired_ratio` | Share of attempts served after client timeout, when known |

No feature uses simulator ground truth, trigger times or scenario names.

### 13.5 Models

- **Explainable grid (always build this first).** Bin `delay_frac` and `orbit_frac` into a 20 by 20 grid. For each cell store the smoothed share of training samples from stuck runs, using a Beta(1, 1) prior. Apply an optional monotone smoothing so probability does not fall as either axis rises. Inference is two array lookups and takes microseconds. This grid is also the heat map shown on the dashboard.
- **Small tree ensemble (second).** `HistGradientBoostingClassifier` or a `RandomForestClassifier` with at most 100 trees and depth 6, trained on all features, calibrated with isotonic regression on validation. Choose between grid and ensemble by validation Brier score and area under the ROC curve, and keep the grid as the fallback and explanation.
- **Export:** save the ensemble as ONNX through `skl2onnx` if it installs, and test loading on LinuxONE (section 18). Model files carry a SHA-256 in the model registry.

### 13.6 Runtime behaviour

- Smooth with an exponential moving average (weight 0.5) to avoid single-tick spikes.
- States: `p_stuck` below 0.3 is `CALM`, 0.3 to 0.5 is `WATCH`, at or above 0.5 is `CRITICAL`. Tune the two thresholds on validation for the best lead time at no more than 1 false critical per simulated hour of surge and recovering-trigger runs.
- Countdown: when in `WATCH` or `CRITICAL`, extrapolate `delay_frac` linearly from the last four ticks and show "estimated time until queue delay exceeds the client timeout". This is a rough guide, not a model output, and the dashboard labels it so.
- The forecaster never triggers an action. It raises severity and sorts recommendations.

### 13.7 Evaluation protocol

| Metric | Definition | Comparison |
| --- | --- | --- |
| ROC AUC and Brier score | Per sample on set C, with 95 percent bootstrap intervals over runs | Grid versus ensemble versus queue-only logistic model |
| Calibration | Reliability curve in 10 bins | The grid and the ensemble |
| Lead time | `t_c - t_f` over stuck runs in set C | Baseline A: alert when `delay_frac >= 0.8`. Baseline B: first D2 alert |
| False critical rate | Critical states during S1, S5 and short non-sticking triggers | Per simulated hour |
| Value of the retry backlog | Re-train without `orbit_frac`, `raf`, `timeout_ratio` | Shows whether retry state adds information beyond the queue |

Report negative results honestly. If the queue-only model matches the full model, say so and keep the simpler one.

### 13.8 Dashboard artefact: the point-of-no-return map

A phase plane with `delay_frac` on the horizontal axis and `orbit_frac` on the vertical axis, coloured by the grid's `p_stuck`, with the 0.5 contour drawn as the "point of no return". The selected segment's last 60 s trajectory is drawn as a trail with a moving dot. When the dot crosses the contour the segment turns red. This single picture explains the whole idea to a judge in ten seconds.

### 13.9 Fallback if metastability cannot be reproduced

If the calibration in section 9.9 fails and cannot be fixed within 90 minutes, change the label to "queue delay at or above `T` within the next 60 s" and rename the feature a **congestion forecaster** everywhere in code, dashboard and report. The method, features and evaluation stay the same. Do not use the words "metastable" or "self-sustaining" for results that do not show it.

### 13.10 Interface and tests

```python
class CascadeForecaster:
    def load(self, path: str) -> None: ...
    def predict(self, rows: list[FeatureRow]) -> dict[tuple, float]: ...   # gateway segment -> p_stuck
    def phase_point(self, row: FeatureRow) -> tuple[float, float]: ...     # (delay_frac, orbit_frac)
```

- Split integrity: no configuration id appears in more than one split.
- Monotonicity check on the grid: probability does not decrease along either axis beyond a small tolerance.
- Inference time per tick for 3 gateways is below 1 ms for the grid and below 5 ms for the ensemble.
- Model load test passes on LinuxONE before gate G4.

## 14. Innovation 3: Fault Localizer

The Fault Localizer attributes the excess bad outcomes in the gateway-by-bank grid to a gateway, a bank or a single route, and its verdict decides which actions are allowed.

### 14.1 Why it exists

Gateways share the same issuing banks. If a bank is slow, every gateway that routes to it slows down, so the symptom looks like a gateway problem on each dashboard. Moving traffic away from one gateway then pushes the same slow-bank traffic through the others and spreads the outage. Cashfree's engineering team describes exactly this risk when traffic is shifted too fast. The localizer separates gateway-side from bank-side faults so the policy engine can block a harmful failover.

The method follows the multi-dimensional root cause localisation literature (Adtributor, HotSpot, Squeeze and its probabilistic successor), which scores attribute values by how much of an anomaly they explain. We implement a small, fast version for a 3 by 5 grid instead of integrating a heavy research tool: the published PSqueeze code takes roughly ten seconds per case and targets old Python versions, which is too slow and too risky for a live loop on s390x.

### 14.2 Inputs and the additive measure

For each route cell `(g, b)` over the short window `Ws`:

- `bad(g,b)` = attempts that timed out, were rejected for overload or retry throttling, or completed slower than 1.5 times the cell's baseline p95.
- `expected(g,b)` = `attempts(g,b)` times the cell's baseline bad rate (floored at 0.005).
- `excess(g,b)` = `max(0, bad - expected)`.

Because `bad` is a count, it adds up across cells, which the explanatory-power calculation needs.

### 14.3 Algorithm

1. `total = sum(excess)`. If `total < 20`, return `INSUFFICIENT_EVIDENCE` and stop.
2. Gateway shares: `EP_g[g] = sum_b excess(g,b) / total`. Bank shares: `EP_b[b] = sum_g excess(g,b) / total`. Each vector sums to 1.
3. For each dimension, choose the smallest set of values whose cumulative share reaches 0.67, keeping only values with share at least 0.10 (candidate set).
4. Compute the surprise of each dimension as the sum over its values of the Jensen-Shannon divergence between the value's share of `bad` and its share of `expected`. A bank dimension that merely follows traffic share has low surprise.
5. Apply the verdict rules below. Require the same verdict on 2 consecutive ticks before changing the published verdict (hysteresis).
6. Confidence is `top_share * min(1, total / 100)`. Publish a ranked candidate list with share and surprise for each candidate.

### 14.4 Verdict rules

| Verdict | Condition | Typical cause |
| --- | --- | --- |
| `GATEWAY_SIDE(g*)` | One gateway has `EP_g` at least 0.67 and no bank has `EP_b` above 0.50 | Capacity loss or slowdown inside the gateway (S2, S4) |
| `BANK_SIDE(b*)` | One bank has `EP_b` at least 0.67 and no gateway has `EP_g` above 0.50 | Slow or black-holing bank seen through every gateway (S3, S6) |
| `ROUTE_SPECIFIC(g*, b*)` | Both `EP_g[g*]` and `EP_b[b*]` at least 0.67 and cell `(g*,b*)` holds at least half of the excess | Single bad path (S7) |
| `MULTI_FAULT` | Two or more candidates in either dimension, or both dimensions concentrated but not on one cell | Overlapping incidents (S5) |
| `PLATFORM_WIDE` | Excess spread in line with traffic shares across both dimensions | Global surge, shared dependency |
| `INSUFFICIENT_EVIDENCE` | Too little excess or too few samples | Quiet or low-volume periods |

Bank spill-over: a slow bank holds workers on every gateway, so other banks on the same gateways also slow down. The excess still concentrates on the slow bank's column, so the verdict remains `BANK_SIDE`. The evaluation reports how often spill-over causes a wrong verdict.

### 14.5 Action eligibility by verdict

| Action | `GATEWAY_SIDE` | `BANK_SIDE` | `ROUTE_SPECIFIC` | `MULTI_FAULT` or `PLATFORM_WIDE` | `INSUFFICIENT_EVIDENCE` |
| --- | --- | --- | --- | --- | --- |
| `THROTTLE_RETRIES` | Eligible on the gateway | Eligible on the bank's traffic | Eligible on the route | Eligible, platform-scoped | Not recommended |
| `SHED_EXPIRED` | Eligible | Eligible on affected gateways | Eligible | Eligible | Not recommended |
| `PRIORITIZE_FRESH` | Eligible | Eligible | Eligible | Eligible | Not recommended |
| `VERIFY_PENDING` (governed) | Eligible | Eligible | Eligible | Eligible | Eligible |
| `SHIFT_TRAFFIC` | Eligible only with spare capacity and a ramp | **Blocked** (shared bank) | Eligible only if the bank is healthy on the other gateways | Blocked | Blocked |
| `HOLD_MONITOR` and `ESCALATE_HUMAN` | Always | Always | Always | Always | Always |

### 14.6 Evaluation

- Top-1 and top-2 accuracy of the verdict and of the identified segment on set C, by scenario (S2, S3, S4, S5, S6, S7).
- Confusion matrix of verdicts, including the share of `BANK_SIDE` incidents wrongly labelled `GATEWAY_SIDE` (the harmful direction).
- Count of harmful failovers in S3: naive failover (U1) versus QueueShield (U3).
- Runtime per tick for the 15-cell grid; the target is below 1 ms.

### 14.7 Interface and tests

```python
@dataclass
class Localization:
    verdict: str                 # GATEWAY_SIDE | BANK_SIDE | ROUTE_SPECIFIC | MULTI_FAULT | PLATFORM_WIDE | INSUFFICIENT_EVIDENCE
    primary: tuple | None        # segment key of the main suspect
    candidates: list[dict]       # ranked: {segment, share, surprise}
    confidence: float
    total_excess: float

class FaultLocalizer:
    def localize(self, cell_bad: np.ndarray, cell_expected: np.ndarray) -> Localization: ...
```

- Synthetic matrices: a single hot row gives `GATEWAY_SIDE`, a single hot column gives `BANK_SIDE`, a single hot cell gives `ROUTE_SPECIFIC`, a flat matrix gives `PLATFORM_WIDE`.
- Shares in each dimension sum to 1 within a tolerance of 1e-9.
- Noise robustness: adding Poisson noise at the S0 level to a hot row does not change the verdict in at least 95 percent of 1,000 trials.
- Hysteresis: a single contradicting tick does not change the published verdict.

## 15. Policy and recommendation engine

The policy engine is a deterministic rule layer that turns detector, forecaster, localizer and governor outputs into a short, ranked list of bounded actions, each with reasons, safety checks and a rollback condition.

The models detect and forecast. The rules decide which actions are eligible. Keeping that split makes every recommendation auditable and keeps the model from taking any action by itself.

### 15.1 Action catalog

| Action ID | Parameters | Scope | Effect | Side effect to disclose | Rollback condition |
| --- | --- | --- | --- | --- | --- |
| `THROTTLE_RETRIES` | `rate_fraction` (default 0.10 of the segment's fresh rate), `retry_after_ms` (base 2,000 with jitter up to 8,000) | Gateway, bank or route | Limits retry attempts; excess get a fast "retry later" response | Some payments abandon sooner | Segment clean for 6 ticks, then loosen in 2 steps |
| `SHED_EXPIRED` | none | Gateway | Drops queued requests whose client already timed out | None for payments; removes wasted work | Segment clean for 6 ticks |
| `PRIORITIZE_FRESH` | `starvation_guard_ms` (2,000) | Gateway | Serves fresh attempts before retries | Retries wait longer | Segment clean for 6 ticks |
| `VERIFY_PENDING` | Governor parameters (section 12) | Gateway | Resolves unknown payments within budget | Slower resolution than naive verification | When pending count returns to baseline |
| `SHIFT_TRAFFIC` | `from_gateway`, `fraction` (at most 0.25 per step), `ramp_ms` (10,000 per step with jitter) | Gateway or route | Moves new attempts to healthier gateways, keeping a 5 percent trickle for recovery probing | Target gateways take extra load | Target enters `WARNING`, or source recovers (then ramp back) |
| `HOLD_MONITOR` | none | Segment | Keep watching | None | n/a |
| `ESCALATE_HUMAN` | `reason` | Incident | Page a person; list unresolved payments | None | n/a |

The simulator implements the first five as described in section 9.4.

### 15.2 Rules (evaluated in order, first match defines the base list)

| Condition | Recommended list, in priority order |
| --- | --- |
| Model or data failure (`DEGRADED_MONITORING`) | `HOLD_MONITOR`, `ESCALATE_HUMAN` |
| Incident `CRITICAL` with verdict `GATEWAY_SIDE` | `SHED_EXPIRED`, `THROTTLE_RETRIES`, `VERIFY_PENDING`, `PRIORITIZE_FRESH`, then `SHIFT_TRAFFIC` if its checks pass |
| Incident `CRITICAL` with verdict `BANK_SIDE` | `THROTTLE_RETRIES` (bank-scoped), `SHED_EXPIRED`, `VERIFY_PENDING`, `PRIORITIZE_FRESH`; `SHIFT_TRAFFIC` shown as **blocked** with the reason "shared bank" |
| Incident `WARNING` (not yet critical) | `VERIFY_PENDING`, `THROTTLE_RETRIES` at a light setting, `HOLD_MONITOR` |
| Verdict `ROUTE_SPECIFIC` | `THROTTLE_RETRIES` on the route, `VERIFY_PENDING`, `SHIFT_TRAFFIC` for that route only if the bank is healthy via the other gateways |
| Verdict `MULTI_FAULT` or `PLATFORM_WIDE` | `THROTTLE_RETRIES` (platform), `VERIFY_PENDING`, `ESCALATE_HUMAN` |
| `SURGE` label only | `HOLD_MONITOR` with the note "demand surge, queue draining, no action" |

The `CRITICAL` list is shown as a package with checkboxes. The operator may approve all, approve some, or reject.

### 15.3 `SHIFT_TRAFFIC` eligibility checks (all must pass)

1. Verdict is not `BANK_SIDE`, `MULTI_FAULT` or `PLATFORM_WIDE`.
2. Target gateways are configured and eligible for the payment method.
3. Each target has estimated spare capacity of at least 30 percent after receiving its share, using `spare = 1 - (arrival_rate + moved_rate) / estimated_capacity`.
4. No target currently has an open `WARNING` or `CRITICAL` incident, and no target forecast is in `WATCH`.
5. The step is at most 25 percentage points, the source keeps at least 5 percent of its traffic, and steps are spaced by at least 10 s with jitter.
6. In-flight and `PENDING_UNKNOWN` payments are never moved. Only new attempts follow the new weights.

If a check fails, the recommendation is still displayed, marked blocked, with the failing check named. This transparency is part of the demo.

### 15.4 Safety invariants

These are enforced in code and verified by tests. A violation fails the build.

| ID | Invariant |
| --- | --- |
| SI-1 | No new attempt is created for a payment in `PENDING_UNKNOWN`, `STATUS_CHECKING` or `ESCALATED_RECONCILE` |
| SI-2 | `SHIFT_TRAFFIC` is never recommended as eligible unless all checks in 15.3 pass |
| SI-3 | No action takes effect without a recorded operator approval (who, when, which recommendation) |
| SI-4 | While in `DEGRADED_MONITORING`, the engine emits only `HOLD_MONITOR` and `ESCALATE_HUMAN` |
| SI-5 | The Governor never exceeds its per-payment cap or per-gateway budget |
| SI-6 | Every recommendation and decision log entry stores the model version, a hash of the input features, the reasons and the confidence |
| SI-7 | No personal data, amounts or account identifiers appear in events, logs or the dashboard |
| SI-8 | Applied actions have an explicit rollback condition and are automatically proposed for rollback when it is met (the operator still approves) |

### 15.5 Recommendation record

```json
{
  "rec_id": "rec-0007",
  "incident_id": "inc-0003",
  "segment": ["gateway", "G1", "*"],
  "severity": "CRITICAL",
  "verdict": "GATEWAY_SIDE",
  "p_stuck": 0.62,
  "confidence": 0.81,
  "countdown_s_estimate": 34,
  "actions": [
    {"id": "SHED_EXPIRED", "params": {}, "eligible": true},
    {"id": "THROTTLE_RETRIES", "params": {"rate_fraction": 0.10, "retry_after_ms": 2000}, "eligible": true},
    {"id": "SHIFT_TRAFFIC", "params": {"fraction": 0.25}, "eligible": false, "blocked_by": "target G2 forecast WATCH"}
  ],
  "reasons": ["TIMEOUT_UP z=+5.1 (0.08 vs 0.01)", "RAF_UP 1.9 vs 1.02", "QUEUE_GROWING +14/s"],
  "model_versions": {"detector": "if-2026-10-17a", "forecaster": "grid-v3"},
  "features_sha256": "...",
  "status": "pending"
}
```

### 15.6 Simulated operator (for evaluation only)

The policy arena needs an operator model so approvals happen without a human.

- Approval delay `d_op` is a parameter, default 20 s, with a sensitivity sweep over 5, 15, 30, 60 and 120 s.
- The operator approves a package when its confidence is at least 0.6, otherwise waits one more tick. Blocked actions are never approved.
- The live demo uses a real human with the same buttons. The sweep over `d_op` answers an important question: how fast must an operator act for early detection to matter?

### 15.7 Tests

- Rule table tests: each row of 15.2 has a unit test with a fabricated incident record.
- Property test for SI-1 and SI-2 over thousands of randomly generated states.
- `SHIFT_TRAFFIC` is blocked for every `BANK_SIDE` verdict in set C of S3 and S6.
- Golden recommendation record tests: the JSON schema validates and contains the model versions and feature hash.

## 16. Operator dashboard and decision log

The dashboard is a single page served by the LinuxONE-hosted API that shows live evidence, lets a person approve or reject recommendations, and doubles as the demo stage.

### 16.1 Technology decision

- **Primary:** FastAPI serves a static single-page app (plain HTML and JavaScript with one vendored charting library such as uPlot, copied into the repository so the page works offline) and a server-sent-events stream at `/v1/stream`.
- **Why not Streamlit as primary:** it pulls in heavy dependencies (notably `pyarrow`) that may lack prebuilt s390x wheels, and it reruns scripts on every interaction, which makes live control awkward. The abstract named Streamlit conditionally ("package availability will be verified"), so this change is consistent with it. If the environment check shows Streamlit installs cleanly and a teammate prefers it, it MAY be used for the secondary evaluation-results page only.
- **Fallback:** if the on-box web stack fails, run the dashboard on a laptop pointing at the LinuxONE API and say so in the report.

### 16.2 Layout

One screen, no scrolling at 1920 by 1080, dark theme, large type for the demo video.

| Panel | Content | Data source | Refresh |
| --- | --- | --- | --- |
| Header | Scenario, seed, simulator time, global state (`MONITORING`, `WARNING`, `CRITICAL`, `DEGRADED_MONITORING`), events per second | `/v1/stream` | 1 s |
| Segment heat map | 3 by 5 grid of gateway-bank routes coloured by anomaly score; row and column totals for gateway and bank; localizer verdict outlined | `/v1/stream` | 5 s |
| Metric strip | For the selected segment: timeout ratio, p95 latency, RAF, queue depth, pending count over the last 5 minutes | `/v1/stream` | 5 s |
| Detector race | One timeline per detector (D0, D1, D2, D3) with incident onset marked and each first alert as a flag with its delay in seconds | `/v1/stream` | 5 s |
| Cascade map | Point-of-no-return phase plane (section 13.8) with the selected gateway's trail, current `p_stuck` and countdown estimate | `/v1/stream` | 5 s |
| Verification panel | Pending count by state, status-check rate against budget, escalated list size | `/v1/stream` | 5 s |
| Recommendation card | Severity, verdict and confidence, reasons, ranked actions with eligibility or the blocking reason, Approve all, Approve selected, Reject | `/v1/recommendations` | event-driven |
| Decision log | Time, operator, recommendation, decision, note, action result | `/v1/decisions` | event-driven |
| Platform performance | Measured p50, p95, p99 event-to-recommendation latency and events per second, plus the environment line (architecture, CPU count, Python version) | `/v1/metrics` | 5 s |

A second tab, **Policy arena**, replays the pre-computed paired universes (U0 to U3) for the same seed with queue depth, goodput, retries and duplicates on shared axes. It reads the batch results from `artifacts/results/arena/`, so it never depends on the live run.

### 16.3 Scenario control panel

- Buttons: Normal, Surge, Gateway slowdown, Bank slowdown, Retry storm, Verification storm, Surge plus slowdown.
- Controls: seed, simulator speed (1x, 2x, 4x), pause, resume, reset, operator-delay slider for the auto-operator, and a toggle for "human approves" versus "auto-operator approves after N seconds".
- Every button maps to `POST /v1/sim/scenario` with a scenario name, seed and optional parameter overrides.

### 16.4 Operator workflow

1. An incident opens; the header changes colour and the segment is outlined on the heat map.
2. The operator reads the recommendation card: what is wrong, where, how sure, why, and what could go wrong with each action.
3. The operator approves, approves a subset, or rejects, with an optional note.
4. The decision is written to the decision log with operator name, timestamp, recommendation id, model versions and the feature hash.
5. The simulator applies approved actions and emits `ACTION_APPLIED`. The card moves to `MITIGATING`.
6. When rollback conditions are met the system proposes rollback steps; the operator approves them the same way.
7. The incident closes after six clean ticks.

### 16.5 Access control and logging

- All write endpoints (decisions, scenario control) require the header `X-QS-Key` equal to the value of the `QS_API_KEY` environment variable. Read endpoints for the dashboard use the same key through a cookie set at page load in the demo.
- Operator names come from a fixed list in `configs/operators.yaml`. No passwords or personal data are stored.
- Logs contain identifiers, timestamps and codes only. No payloads beyond the schema fields.

### 16.6 Decision log schema (export format)

| Column | Meaning |
| --- | --- |
| `ts` | Simulator time of the decision |
| `rec_id`, `incident_id` | Links |
| `operator` | Who decided |
| `decision` | `approve_all`, `approve_selected`, `reject` |
| `actions_approved` | List of action ids with parameters |
| `note` | Free text from the operator |
| `model_versions` | Detector, forecaster and rule-set versions |
| `features_sha256` | Hash of the input features that produced the recommendation |
| `outcome` | Filled later: recovered, escalated, rolled back |

`make export-log` writes the decision log to `artifacts/results/decision_log.csv` for the report.

### 16.7 UI acceptance checklist

- [ ] The page loads with no network access (all assets local).
- [ ] A scenario button starts the run and the header shows the scenario within 1 s.
- [ ] The heat map highlights the correct segment during S2, S3 and S7.
- [ ] The detector race shows four flags with distinct delays on S4.
- [ ] The cascade map trail crosses the 0.5 contour on S4 and stays below it on S1.
- [ ] A blocked `SHIFT_TRAFFIC` shows its reason on S3.
- [ ] Approving writes a decision log row and a visible `ACTION_APPLIED` entry within 2 s.
- [ ] Killing the event stream for 12 s shows `DEGRADED_MONITORING` and removes action buttons.

## 17. Evaluation plan

We evaluate five things separately (detection, component quality, policy outcomes, runtime, and a public benchmark), on identical seeds and held-out configurations, with the hypotheses and primary metrics fixed before the test set is touched.

### 17.1 Pre-registered primary hypotheses

Write these into `docs/EVAL.md` and commit before running set C. Report every one, whether it holds or not.

| ID | Hypothesis | Metric | Comparison |
| --- | --- | --- | --- |
| P1 | D3 detects localised incidents sooner than D0, D1 and D2 | Detection delay, missed incidents | Scenarios S2, S3, S4, S5, S7 at the matched false-alert budget (section 11.7) |
| P2 | D3 stays within the false-alert budget on surges | False alerts per simulated hour | S0 and S1, all detectors |
| P3 | U3 recovers faster with fewer wasted retries than U0 and U1 | Recovery time, excess retry attempts, peak queue depth | S4, paired by seed |
| P4 | Localisation prevents harmful failover | Count of failovers that raise peak queue or recovery time | S3, U1 versus U3 |
| P5 | Governed verification produces a lower status-check peak than naive verification | Peak status-check rate, peak queue | S6 |
| P6 | The forecaster gives positive lead time with a low false critical rate | Lead time, false critical per hour | S4, S1, S5, short triggers |

### 17.2 Policy universes (paired on one random tape per seed)

| Universe | Clients | Operator behaviour | What it represents |
| --- | --- | --- | --- |
| U0 | `naive` | None | Do nothing; the baseline collapse |
| U1 | `naive` | Textbook failover: when a gateway's `success_ratio` is below 0.90 for 2 ticks, move its traffic evenly to the others; restore when above 0.97 for 3 ticks | A common reactive rule that can spread outages |
| U2 | `disciplined` (full-jitter backoff, 3 attempts, idempotency keys, Governor-style checks) | None | Static retry hygiene without any detection, a deliberately strong honest baseline |
| U3 | `naive` | QueueShield recommends; the simulated operator approves after `d_op` | Our system acting on a mostly naive client population |

The report MUST include the cases where U2 beats U3, if any, and explain them. A system that wins only against strawmen is not credible.

### 17.3 Metrics

| Group | Metric | Definition |
| --- | --- | --- |
| Detection | Detection delay | Valid alert time minus onset (section 11.6); missed incidents reported separately and never imputed |
| Detection | False alerts per hour | Alerts outside incident windows per simulated hour, aggregated |
| Detection | Localisation accuracy | Top-1 and top-2 match of segment and level |
| Policy | Peak queue depth | Maximum summed gateway queue depth |
| Policy | Excess retry attempts | Attempts minus fresh payments over the run |
| Policy | Time to recover | Seconds from trigger end until goodput is at least 95 percent of baseline for 30 s |
| Policy | Unique successful payments | Payments with a commit within 30 s of initiation; retries never counted as new customers |
| Policy | End-to-end p95 completion time | Among successful payments |
| Policy | Duplicate commits | Committed attempts minus one per payment (ground truth); in governed arms this is also a safety invariant check |
| Policy | Status-check peak and share of capacity | Peak checks per second and share of gateway capacity used by checks |
| Policy | Escalated to reconciliation | Payments still unknown when their check budget ended |
| Policy | Harmful failovers | Failovers after which peak queue or recovery time worsens relative to U0 on the same seed |
| Forecaster | AUC, Brier score, lead time, false critical per hour | Section 13.7 |
| Runtime | Event-to-recommendation latency, events per second | Section 18 |

### 17.4 Sample sizes and compute budget

Python event simulation is not free. Measure seconds per run at gate G1 and set the counts from the table.

| Purpose | Minimum viable | Target |
| --- | --- | --- |
| Detection on set C, all eight scenarios, U0 only | 30 configurations each | 60 |
| Policy arena on set C, scenarios S2 to S6, four universes | 20 configurations each | 40 |
| Forecaster training set A | 300 runs | 600 |
| Validation set B (tuning) | 15 per scenario | 30 |

Runs are independent, so use `multiprocessing` with one worker per core. At 10 s per run, the target budget is about 2,100 runs, about 6 core-hours, under one hour on 8 cores. If runs are slower than 20 s, drop to the minimum viable column rather than shortening scenarios.

### 17.5 Statistics

- Pair by seed. Report the median and interquartile range per arm and the median paired difference with a 95 percent bootstrap interval (10,000 resamples over seeds).
- Use the Wilcoxon signed-rank test for paired differences as a sanity check and show effect sizes; do not chase significance.
- Report counts of missed incidents and failed runs beside every delay statistic. Do not hide missed incidents by averaging delays of detected ones only.
- Primary hypotheses P1 to P6 are the headline results. Every other comparison is labelled exploratory.

### 17.6 Leakage controls and freeze protocol

1. Training, validation and test configurations are disjoint (section 9.8). Test contains segments, onsets and parameter ranges not in A or B.
2. Scalers, baselines' priors and thresholds are fitted on A and tuned on B only.
3. At the evaluation freeze (gate G5) tag `eval-freeze`. After the tag, only fixes that do not change model outputs are allowed, and each is listed in `docs/DEVIATIONS.md`.
4. Set C is run once for the headline results. Reruns are allowed only for crashes, with the seeds unchanged.
5. The forecaster never receives scenario names, trigger times or ground-truth labels at inference.

### 17.7 Sensitivity and robustness sweeps (exploratory)

- Operator delay `d_op` in 5, 15, 30, 60 and 120 s: how quickly must someone act for early detection to matter?
- Client timeout 2,500, 3,000 and 4,000 ms; baseline utilisation 0.45, 0.55 and 0.65; attempt cap 3 to 6.
- Surge size, and how close a surge is to capacity before the gate fails.
- Low-volume routes: the effect of the 30-attempt minimum on route-level detection (S7).

### 17.8 Public benchmark (reported separately)

Use three to five real-world server-metric time series from the Numenta Anomaly Benchmark (for example from its real AWS CloudWatch group). Run D2, windowed Isolation Forest and RRCF with the same persistence rule, and score detection inside labelled anomaly windows and false alerts outside. Download the data by script and do not commit it to our repository; check the dataset licence before redistributing anything. The table is titled "Detector sanity check on non-payment server metrics" and never mixed with payment results. Do not touch AIOps 2020 or Loghub unless every other part of the plan is finished.

### 17.9 Report figures

| Figure | Content |
| --- | --- |
| F1 | Metastability calibration heat map (stuck probability versus trigger size and duration) |
| F2 | Detector race on a representative S4 run |
| F3 | Detection delay by detector and scenario at the matched budget, with missed counts |
| F4 | False alerts per hour by detector on S0 and S1 |
| F5 | Cascade map with trajectories for S4 and S1 |
| F6 | Forecaster ROC and reliability curve; lead-time distribution |
| F7 | Localisation confusion matrix |
| F8 | Policy arena on a representative S4 seed: queue depth, goodput, retries for U0 to U3 |
| F9 | S6 status-check rate and queue for naive versus governed verification |
| F10 | Operator-delay sensitivity and runtime latency distribution on LinuxONE |

### 17.10 Threats to validity (state them in the report)

- **Circularity.** We wrote the simulator and the detectors. Mitigations: held-out configurations with different parameter ranges, strong baselines (D2, U2), ablations, an independent public benchmark and negative results reported.
- **Simulator realism.** It captures qualitative retry and queue dynamics, not real gateway behaviour. Results are relative improvements under stated assumptions.
- **Small topology.** Three gateways and five banks. Localisation may behave differently on thousands of routes.
- **Synthetic operator.** Real operators vary in speed and judgement; the delay sweep addresses part of this.
- **Parameter choices.** Calibrated to produce metastability; a different calibration could change magnitudes. The sensitivity sweeps show the direction of the effect.

## 18. IBM LinuxONE integration and performance measurement

The core decision path runs on the supplied LinuxONE environment and we publish measured latency and throughput with the hardware disclosed; anything about Telum, ONNX acceleration or the AI Toolkit is claimed only if we demonstrate it.

### 18.1 What runs where

| Component | Runs on LinuxONE | Notes |
| --- | --- | --- |
| Event ingest API, validation, deduplication | Yes | FastAPI plus uvicorn |
| Feature engine, detectors, forecaster, localizer, governor, policy engine | Yes | The core workload |
| Decision log and incident store (SQLite) | Yes | Single process, WAL mode |
| Dashboard server | Yes | Static page plus SSE from the same API |
| Live simulator (demo mode) | Yes, if CPU allows; otherwise a second VM or the laptop | Posts events to the API over HTTP |
| Model training (Isolation Forest, forecaster) | Yes (`make train`) | Avoids endianness problems |
| Batch evaluation (thousands of runs) and figure generation | Preferably yes; MAY run elsewhere | The report MUST state which results were produced where |

### 18.2 Environment check (first 30 minutes, work package WP-00)

Run `scripts/env_check.sh`, which calls `qs/bench/env_report.py` and writes `artifacts/env_report.json`. Required fields:

- `uname -m` (expect `s390x`), `sys.byteorder` (expect `big`), OS release, kernel.
- `lscpu` output, `nproc`, memory and disk free.
- Python version, `pip --version`, and for each library in 18.3 whether it imports and its version.
- CPU feature flags from `/proc/cpuinfo`. Look for the flag that indicates the Neural Network Processing Assist instruction (as far as we know it appears as `nnpa`). Its absence means no on-chip AI accelerator is exposed, and we make no accelerator claim.
- Whether `docker` or `podman` exists and works, and whether the IBM container registry is reachable.
- Outbound internet test to `pypi.org` and `github.com`, or the configured mirror.

Post the output in the team channel and in `docs/DEVIATIONS.md` if any assumption in section 4 was wrong.

### 18.3 Package matrix with fallback chain

For each package try the next option only if the previous one fails, and time-box each package to 30 minutes.

| Package | Needed for | Chain |
| --- | --- | --- |
| `numpy`, `scipy` | Core maths | pip wheel, then `apt install python3-numpy python3-scipy`, then build |
| `scikit-learn` | Isolation Forest, ensemble | pip wheel, then `apt install python3-sklearn`, then build from source (slow; start early, in the background) |
| `fastapi`, `uvicorn`, `pydantic`, `httpx` | API | pip (pydantic v2 uses a compiled core; if no wheel, fall back to `pydantic<2` or plain dataclasses with manual validation) |
| `orjson` | Fast JSON | Optional; fall back to stdlib `json` |
| `onnx`, `skl2onnx`, `onnxruntime` | Model export and load test | Optional stretch; if `onnxruntime` is missing, skip the load test and say so |
| `rrcf` | Ablation | Pure Python; pip |
| `matplotlib`, `pandas` | Figures and tables | MAY run on the laptop; results files are plain CSV and JSON |
| `pyarrow`, `streamlit` | Not required | Avoided on purpose |

If a package cannot be installed within its time box, record the failure and use the stated fallback. Do not block the project on one package.

### 18.4 Endianness safety

s390x is big-endian. Rules:

1. Train models on the box with `make train`. Do not copy pickled scikit-learn or NumPy objects from a little-endian machine.
2. Data exchange uses CSV, JSON or `.npz` files (NumPy stores byte order in the header). Run `bench/roundtrip_check.py`, which saves and reloads a model and an array on the box and compares scores.
3. If a model file must be moved, load it on the box first and compare scores with the saved reference scores on a held-out sample before using it.

### 18.5 Performance benchmark (`bench/latency_bench.py`)

The benchmark measures system speed, not detection delay. Detection delay is seconds by design because it depends on windows and persistence; system latency is milliseconds.

| Measurement | Definition |
| --- | --- |
| Ingest latency | Time from an event batch entering the API to the in-memory state being updated, per batch of 100 and 500 events |
| Tick compute time | Wall time of one tick: features for 24 segments, scoring, forecasting, localization, governor and policy |
| Decision latency | Time from the last event of a window being received to the recommendation record being written |
| Throughput | Highest sustained events per second with no growth in queue length and decision latency below 100 ms at p99 |

Method:

1. Pre-generate a synthetic event file at fixed size so generation cost is excluded.
2. Offered rates of 1,000, 5,000, 10,000, 20,000 and 50,000 events per second, two modes: in-process (no HTTP) and over localhost HTTP with batches.
3. Warm up for 10 s, measure for 60 s, repeat three times, report the median of repetitions with the p50, p95 and p99 of each latency.
4. Record CPU utilisation, number of worker processes and the environment line from `env_report.json`. Disclose the workload: 24 segments, event size in bytes, share of event types.
5. If CPU permits, repeat on the laptop for context, labelled as such. Do not present the comparison as a platform advantage unless the numbers show it.

Performance guidance for implementers: parse batches with one JSON call, update counters with NumPy `bincount` on batch arrays, avoid creating a Python object per event on the hot path, keep SQLite writes batched and off the request path, and shard by gateway across processes if one process saturates a core.

### 18.6 Model portability path and the claim ladder

| Level | What we did | What we may claim |
| --- | --- | --- |
| 0 | Core workload runs on s390x LinuxONE with measured latency and throughput | "The decision path runs on IBM LinuxONE", with numbers and hardware disclosed |
| 1 | The forecaster or detector was exported to ONNX and loaded on the box with matching scores | "Our model is portable in ONNX and runs on s390x" |
| 2 | The AI Toolkit container, Snap ML or the IBM Z Deep Learning Compiler ran our model | "We ran it through IBM's AI Toolkit", with the version |
| 3 | The accelerator flag is present, the library confirms it, and a benchmark shows a measured difference | "On-chip acceleration measured", only with that evidence |

IBM's Triton Snap ML backend accepts tree models in PMML, ONNX or XGBoost JSON, and the toolkit covers PyTorch, TensorFlow, Snap ML and ONNX. Our forecaster is a small tree ensemble on purpose, so Level 1 or 2 is realistic if the packages are available. Until proven, describe Telum as the production roadmap, not as something we used.

### 18.7 Data locality and security statement (honest version)

- All data are synthetic. Scoring happens next to the event ingestion on the same host, so nothing leaves the platform in the design.
- Operator endpoints require an API key; the database file has owner-only permissions; logs hold identifiers and codes only.
- We do not claim Secure Execution, hardware encryption or any certified control.

### 18.8 Questions for IBM mentors in the first hour

- Is Telum or Spyre acceleration exposed on our instance, and through which library?
- Is the AI Toolkit container registry reachable, and are there ready-made images for Python and Snap ML?
- Can we get more than one virtual CPU, and for how long does the instance live?
- Is there a preferred way to show measured performance in the submission?
- Are results produced off-platform acceptable for batch evaluation if disclosed?

## 19. Tech stack, repository layout and interface contracts

The stack is deliberately small and mostly pure Python so that it installs on s390x, and the repository is laid out so that each work package owns a folder and talks to the others through typed interfaces.

### 19.1 Stack

| Layer | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.10 or newer | Matches the abstract; wide library support |
| Simulator | Own `heapq` event loop, NumPy for random tapes | Minimal dependencies, deterministic |
| Numerics | NumPy, SciPy (optional) | Vectorised windows and statistics |
| Models | scikit-learn (`IsolationForest`, `HistGradientBoostingClassifier` or `RandomForestClassifier`), optional `rrcf` | Matches the abstract |
| API | FastAPI with uvicorn; server-sent events for the dashboard | Matches the abstract; SSE needs no extra packages |
| Data validation | `pydantic` v2, or `dataclasses` with manual checks if wheels are missing | Fallback in section 18.3 |
| Storage | SQLite (WAL mode) | Matches the abstract; single process |
| Dashboard | Static HTML and JavaScript, one vendored chart library | Avoids heavy Python dependencies |
| Tests | `pytest`, `hypothesis` for property tests | Pure Python |
| Formatting | `ruff` (lint and format) if installable; otherwise `black` off-box | Consistency |
| Packaging | `pyproject.toml`, plain `requirements.txt` pinned to versions that actually install on the box | Reproducibility |

### 19.2 Repository layout

```text
queueshield/
  README.md                 quick start, demo script, results summary
  AGENTS.md                 short rules for coding agents (section 22.1)
  Makefile                  all targets in section 21
  pyproject.toml
  requirements.txt          pinned, verified on the box
  configs/
    sim_default.yaml        frozen after calibration
    detectors_frozen.yaml   frozen after validation tuning
    governor.yaml, policy.yaml, operators.yaml
    rails/upi_like.yaml     analogy preset (section 12.2)
  docs/
    BLUEPRINT.md            export of this document
    EVAL.md                 pre-registered hypotheses and protocol
    CLAIMS.md               allowed and forbidden claims (section 27)
    DEVIATIONS.md           every deviation, one line each
    ARCHITECTURE.md         diagram and component notes
  qs/
    config.py  schema.py  clock.py  ids.py
    sim/       engine.py entities.py clients.py scenarios.py tape.py routing.py status_service.py controls.py run.py
    core/      ingest.py dedupe.py payments.py windows.py features.py baselines.py
               detectors/ (threshold.py adaptive.py rules.py iforest.py rrcf.py persistence.py)
               lifecycle.py localizer.py governor.py policy.py actions.py pipeline.py
    forecast/  dataset.py train.py model.py grid.py
    store/     db.py schema.sql log.py
    api/       app.py routes.py sse.py auth.py
    eval/      harness.py matching.py metrics.py arena.py ablations.py nab.py report.py plots.py
    ui/        static/index.html app.js style.css vendor/uplot.min.js
    bench/     env_report.py roundtrip_check.py latency_bench.py throughput_bench.py
  scripts/     env_check.sh tune_metastability.py make_configs.py make_cascade_dataset.py
               train_models.py run_eval.sh demo.sh record_checklist.md
  tests/       unit/ property/ golden/ integration/
  artifacts/   models/ results/ figures/ logs/   (generated; only small summaries are committed)
```

### 19.3 Module responsibilities (who owns what)

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `qs/schema.py` | Event, payment, segment and record models; the only place enums are defined | none |
| `qs/sim` | Generates events and applies actions; owns ground truth | `schema`, `config` |
| `qs/core/pipeline.py` | Wires ingest, payments, windows, features, detectors, localizer, forecaster, governor and policy into one `Pipeline` class with `ingest()` and `tick()` | all of `core`, `forecast` |
| `qs/store` | Persistence and the decision log | `schema` |
| `qs/api` | HTTP and SSE around `Pipeline` | `core`, `store` |
| `qs/eval` | Runs the batch pipeline over simulated runs and scores results | `sim`, `core` |
| `qs/ui` | Static dashboard | API only |

Rule: `qs/core` must not import from `qs/sim`, and `qs/sim` must not import from `qs/core`. Both depend on `qs/schema`. This guarantees that the pipeline only sees what a real platform would see.

### 19.4 Core interfaces (stable contracts)

```python
# qs/core/pipeline.py
class Pipeline:
    def __init__(self, cfg: PipelineConfig, store: Store | None = None): ...
    def ingest(self, events: list[Event]) -> IngestResult: ...          # accepted, duplicates, rejected
    def tick(self, now_ms: int) -> TickResult: ...                       # features, scores, incidents, recs
    def decide(self, rec_id: str, decision: Decision) -> list[Action]: ...  # operator decision -> actions
    def pending_actions(self) -> list[Action]: ...                       # for the simulator to apply

# qs/sim/run.py
def run_scenario(scenario: str, seed: int, arm: str, cfg: SimConfig,
                 sink: EventSink, action_source: ActionSource) -> RunResult: ...
# EventSink: in-process Pipeline.ingest, HTTP client, or file writer
# ActionSource: Pipeline.pending_actions, HTTP poller, or a null source
```

### 19.5 HTTP API (version 1)

| Method and path | Purpose | Body or query | Response |
| --- | --- | --- | --- |
| `POST /v1/events` | Ingest a batch (up to 500 events) | JSON list of events | `202 {accepted, duplicates, rejected, errors[]}` |
| `GET /v1/state` | Current per-segment features, scores, verdicts, forecasts | none | JSON snapshot |
| `GET /v1/stream` | Server-sent events with snapshots, incidents, recommendations, applied actions | none | `text/event-stream` |
| `GET /v1/incidents` | List incidents | `state`, `limit` | JSON list |
| `GET /v1/recommendations` | List recommendations | `status` | JSON list |
| `POST /v1/recommendations/{rec_id}/decision` | Record an operator decision | `{decision, operator, note, actions_selected[]}` | `200 {actions[]}` |
| `GET /v1/actions/pending` | Simulator polls for approved actions | `since` | JSON list |
| `POST /v1/actions/{id}/applied` | Simulator reports application | `{ts, result}` | `200` |
| `POST /v1/sim/scenario` | Start or reset a scenario | `{scenario, seed, speed, overrides}` | `202` |
| `GET /v1/metrics` | Latency histograms, throughput, environment line | none | JSON |
| `GET /v1/health` | Liveness and global state | none | `{status, global_state}` |

All write endpoints require `X-QS-Key`. Errors use a consistent JSON body `{error, detail}` with the right HTTP status.

### 19.6 Conventions

- Type hints everywhere; `mypy` is optional, but interfaces above must keep their signatures. Changing one requires a message in the team channel and an update here.
- Logging with the standard `logging` module, JSON lines, no payloads beyond schema fields. Log level from `QS_LOG_LEVEL`.
- Configuration lives in YAML under `configs/`, loaded once into frozen dataclasses. No magic numbers in code that appear in a table of this document; read them from config.
- Randomness only through `qs/sim/tape.py` and explicitly seeded `numpy.random.Generator` objects.
- Time only through `qs/clock.py` (simulator clock or wall clock). Nothing else calls `time.time()` except the benchmark code.

### 19.7 Git workflow

1. `main` is always runnable. Each work package works on a branch `wp-XX-short-name` and merges through a pull request with at least one other person (or the lead) checking the acceptance tests.
2. Commit messages start with the package id, for example `WP-05: add surge gate`.
3. Tags mark gates: `g0-env`, `g1-sim-frozen` (alias `sim-frozen`), `g2-detect`, `g3-arena`, `g4-live`, `eval-freeze`, `submission`.
4. Generated artefacts larger than 5 MB are not committed. Result summaries (CSV, JSON, figures) are committed so the report works without a rerun.

## 20. Phased implementation plan

The build runs in seven stages with a hard gate at the end of each one, and no stage may start its optional work before its gate is green.

Hours are relative to the moment the team starts (H0). If time has already passed, shift the plan and apply the compression table at the end of this section. Owners refer to the roles in section 22.2.

### 20.1 Stage overview

&#91;embedded content: roadmap · 7 stages, 7 gates, 1 freeze\]

Main owners by stage: everyone on stage 0; R1 on stage 1 with R2 and R4 starting their skeletons; R2 on stage 2 with R1 on the harness; R3 on stage 3 with R1 and R2; R4 on stage 4 with R2; R1 and R3 on stage 5; everyone on stage 6.

### 20.2 Stage 0: Mobilise and verify the platform (H0 to H2.5)

**Objective.** Prove that the platform works for us and that every person can start work.

1. **T0.1** Obtain access, SSH in, run `scripts/env_check.sh`, save `artifacts/env_report.json` (WP-00).
2. **T0.2** Create the repository from section 19.2, push to the shared remote, give everyone write access, enable branch protection on `main` if available.
3. **T0.3** Work through the package matrix (section 18.3). Start any slow source build, such as scikit-learn, in the background immediately.
4. **T0.4** Run `bench/roundtrip_check.py` for endianness and a minimal FastAPI hello-world with SQLite on the box.
5. **T0.5** Create `docs/DEVIATIONS.md`, `docs/CLAIMS.md` (copy section 27) and `docs/EVAL.md` (copy section 17.1).
6. **T0.6** Ask the IBM mentor questions in section 18.8.
7. **T0.7** Assign work packages (section 22), agree stand-ups every three hours (10 minutes, three questions: done, next, blocked), set a team rule that anything blocked for more than 45 minutes triggers the stated fallback.

**Gate G0 (tag `g0-env`).** `env_report.json` committed; package decisions recorded; remote repository works; each person has a branch and a package.

### 20.3 Stage 1: Simulator, schema and calibration (H2 to H7)

**Objective.** A deterministic, reactive simulator that really exhibits the retry loop, plus the shared schema everyone codes against.

1. **T1.1** `qs/schema.py` and `qs/config.py` with all enums, event models and config dataclasses (WP-01). This is on the critical path: finish within 60 minutes and announce it.
2. **T1.2** Simulator engine: arrivals, gateways, queues, banks, clients, timeouts, retries, zombie work, commits and duplicate accounting (WP-02).
3. **T1.3** Random tape and common random numbers; scenarios S0 to S7; ground-truth writer; configuration sets A, B and C (WP-03).
4. **T1.4** `scripts/tune_metastability.py` and the calibration in section 9.9. Save the heat map as figure F1.
5. **T1.5** Early drop for downstream teammates at about H4: recorded event files for S0, S2 and S4 with ground truth, so feature and detector work starts before the full simulator is finished.
6. **T1.6** In parallel (R2): ingest, payment state machine and window skeleton against the recorded files. (R4): dashboard skeleton with mock data.
7. **T1.7** Measure seconds per run and set the sample sizes in section 17.4.

**Gate G1 (tag `sim-frozen`).** Determinism and conservation tests pass; common-random-number test passes; S0 utilisation is 0.50 to 0.60; the metastability acceptance in section 9.9 passes or the congestion-forecaster fallback (section 13.9) is formally chosen; ground-truth files verified by eye for S2 and S3. After this tag, simulator dynamics do not change.

### 20.4 Stage 2: Core pipeline and detection (H6 to H11)

**Objective.** End-to-end detection numbers on small samples, with all four detectors and the ablation arms runnable.

1. **T2.1** Ingest, dedupe, ordering, payment state machine (WP-04).
2. **T2.2** Windows and the feature engine with per-segment baselines and freeze rule (WP-04).
3. **T2.3** Detectors D0, D1, D2 (baselines) and D3 (rules, surge gate, Isolation Forest, persistence, severity, reason codes) (WP-05).
4. **T2.4** Incident lifecycle, degraded-monitoring triggers and SQLite logging (WP-06).
5. **T2.5** Evaluation harness: alert-to-ground-truth matching, metrics, false-alert budget bisection, parallel runner (WP-13).
6. **T2.6** Train the first Isolation Forest on the box (`make train`) and verify the round trip.

**Gate G2 (tag `g2-detect`).** `make eval-detect-small` runs end to end on at least 5 configurations per scenario and prints the detection table for D0 to D3; all unit tests for sections 10 and 11 pass; no crash on a 12 s stream cut.

### 20.5 Stage 3: Innovations, policy and arena (H10 to H16)

**Objective.** The three innovations, the action layer and the paired policy comparison.

1. **T3.1** Governor, the S6 scenario controls and the naive-verify baseline (WP-07).
2. **T3.2** Cascade dataset generation, grid model first, then the ensemble, evaluation on set B (WP-08). Start dataset generation as soon as G1 is green because it is compute-heavy.
3. **T3.3** Fault Localizer with unit tests and S3 and S6 evaluation (WP-09).
4. **T3.4** Policy engine, action catalog, recommendation records, simulated operator, server-side controls wired into the simulator (WP-10).
5. **T3.5** Policy arena U0 to U3 with paired seeds and the metrics in section 17.3 (WP-13).

**Gate G3 (tag `g3-arena`).** The arena runs for S3, S4 and S6 with at least 5 seeds each and reports all four universes; safety invariant tests SI-1 to SI-8 pass; the forecaster has a validation AUC and calibration plot; the localizer has validation accuracy. If an innovation misses its target, apply the cut rules in section 23 immediately rather than polishing.

### 20.6 Stage 4: Live loop, dashboard and platform measurement (H15 to H20)

**Objective.** A demo that runs on the supplied platform with a human in the loop, and honest speed numbers.

1. **T4.1** HTTP API, server-sent events and live simulator mode with action polling (WP-11).
2. **T4.2** Dashboard panels and scenario control (WP-12). Use the checklist in section 16.7 as the definition of done.
3. **T4.3** Deploy to the box, run under `systemd` or `tmux`, record start commands in the README (WP-14).
4. **T4.4** Run the latency and throughput benchmarks (section 18.5), the ONNX round trip if packages exist, and save results (WP-14).
5. **T4.5** Demo rehearsal number 1 with the script in section 24, timed.
6. **T4.6** Feature freeze at H20: no new capabilities after this point, only fixes.

**Gate G4 (tag `g4-live`).** The full demo script runs twice in a row on the box without manual repair; benchmark results are saved; the model loads on the box; the UI checklist passes.

### 20.7 Stage 5: Frozen evaluation and ablations (H19 to H22)

**Objective.** Produce every number and figure in the report from frozen thresholds.

1. **T5.1** Tune detector thresholds on set B to the matched false-alert budget; write `configs/detectors_frozen.yaml` (by H18 to H19).
2. **T5.2** Tag `eval-freeze`. Run set C once for detection, ablations, forecaster, localizer, governor and arena (WP-13).
3. **T5.3** Sensitivity sweeps on operator delay and one or two other parameters if time permits.
4. **T5.4** NAB sanity check if every earlier task is green (section 17.8).
5. **T5.5** Generate figures F1 to F10 and `results/summary.md`; review each number against the pre-registered hypotheses P1 to P6.

**Gate G5.** `make eval` regenerates `results/summary.md` from the frozen configs; every figure exists or is explicitly dropped with a note; `docs/DEVIATIONS.md` reviewed by the lead.

### 20.8 Stage 6: Report, video and submission (H21 to H24)

1. **T6.1** Record a backup demo video at H21 as soon as G4 is green, then record the final at H23.
2. **T6.2** Write the README (quick start, architecture, results, limitations), export this blueprint to `docs/BLUEPRINT.md`, write the report from `results/summary.md`.
3. **T6.3** Clean the repository: remove secrets, large files and dead code; make sure `make demo` and `make eval` work from a clean clone.
4. **T6.4** Complete the submission checklist in section 24 and submit with at least 30 minutes of buffer.

**Gate G6.** Submission checklist complete and the submission confirmed.

### 20.9 Mapping to the phases promised in the abstract

| Abstract phase | Hours | Where it appears in this plan |
| --- | --- | --- |
| Verify access and packages; schema; simulator behaviour; one public benchmark subset | 0 to 4 | Stages 0 and 1 (benchmark moved to Stage 5 as optional) |
| Ingestion, rolling features, baselines, anomaly scoring, event logging | 4 to 10 | Stages 1 and 2 |
| Recommendations, pending-status safeguards, dashboard, deployment | 10 to 16 | Stages 3 and 4 |
| Paired tests for normal, surge, slowdown and retry storm; fix leakage | 16 to 21 | Stages 4 and 5 |
| Reproducible repository, results, architecture explanation, video | 21 to 24 | Stage 6 |

### 20.10 Compression plan if fewer hours remain

| Hours left | Keep | Cut or downgrade |
| --- | --- | --- |
| 24 | Everything above | Nothing planned |
| 18 | All of Stages 0 to 4, minimum viable sample sizes, grid forecaster only | NAB benchmark, ONNX or Snap ML, e-detector, RRCF ablation, sensitivity sweeps beyond operator delay |
| 12 | Simulator S0 to S4 and S6, D0 to D3, Governor, Localizer, policy engine, arena on S3, S4 and S6, live demo, latency benchmark | Forecaster becomes the grid-only or the plain queue-delay estimate; scenarios S5 and S7 dropped; detector ablations reduced to R, M and RM |
| 8 | S0, S2, S4, S6, D0 to D3 on small samples, Governor, a simplified localizer, live demo | Forecaster, arena beyond S4, all optional items; be explicit in the report about what was not done |

## 21. Build pipeline, make targets and quality gates

One `Makefile` drives the whole project, every target is deterministic and records what it produced, and a gate check script refuses to call a stage done until its artefacts exist.

### 21.1 Data and artefact pipeline

The project is a chain where each step reads files written by the previous step, so any teammate can restart from the middle.

1. `configs/*.yaml` (frozen parameters) feed `make_configs`, which writes configuration sets A, B and C as JSON.
2. Configurations plus seeds feed the simulator, which writes event streams and `ground_truth.json` per run into `artifacts/runs/`.
3. Event streams feed the pipeline, which writes feature rows, alerts, incidents, recommendations and metrics per run.
4. Set A runs feed model training and the forecaster dataset, which write `artifacts/models/` and update the model registry.
5. Set B runs feed threshold tuning, which writes `configs/detectors_frozen.yaml`.
6. Set C runs feed the evaluation harness, which writes `artifacts/results/*.csv`, `summary.md` and `figures/`.
7. The report and README read only from `artifacts/results/`.

### 21.2 Make targets

| Target | What it does | Needs | Typical time | Where |
| --- | --- | --- | --- | --- |
| `make setup` | Create a virtual environment and install pinned requirements, with the fallback chain from section 18.3 | Internet or mirror | 5 to 30 min | Box |
| `make env-check` | Run `scripts/env_check.sh` and write `artifacts/env_report.json` | none | 1 min | Box |
| `make test` | Run all unit, property and golden tests | setup | 2 to 5 min | Anywhere |
| `make test-fast` | Unit tests only | setup | under 1 min | Anywhere |
| `make sim-calibrate` | Metastability sweep and heat map (section 9.9) | setup | 10 to 30 min | Anywhere |
| `make configs` | Generate configuration sets A, B and C | calibrated `sim_default.yaml` | seconds | Anywhere |
| `make dataset` | Generate the cascade forecaster dataset from set A | configs | 20 to 60 min | Parallel cores |
| `make train` | Fit Isolation Forest, forecaster grid and ensemble; update the model registry | dataset | 5 to 15 min | **Box** |
| `make eval-detect-small` | Detection table on 5 configurations per scenario | train | 5 to 10 min | Anywhere |
| `make tune` | Bisect detector sensitivities on set B to the false-alert budget | train | 10 to 30 min | Anywhere |
| `make freeze` | Verify clean tree, write config hashes, create tag `eval-freeze` | tune | seconds | Anywhere |
| `make eval-detect` | Full detection evaluation on set C | freeze | 15 to 45 min | Parallel cores |
| `make eval-arena` | Four-universe policy arena on set C | freeze | 30 to 90 min | Parallel cores |
| `make eval-components` | Forecaster, localizer and governor metrics on set C | freeze | 10 to 30 min | Parallel cores |
| `make eval-nab` | NAB sanity check (optional) | download script | 5 to 10 min | Anywhere |
| `make eval` | All of the above and the report tables and figures | freeze | 1 to 3 hours | Parallel cores |
| `make bench` | Latency and throughput benchmark | train | 15 min | **Box** |
| `make demo` | Start API, dashboard and live simulator for the scripted demo | train | 1 min | **Box** |
| `make export-log` | Write the decision log to CSV | a demo run | seconds | Box |
| `make report` | Assemble `results/summary.md` and figures into the report skeleton | eval | 1 min | Anywhere |
| `make check-gates` | Verify that tags and artefacts exist for every gate reached so far | none | seconds | Anywhere |
| `make submission-check` | Run the checklist in section 24 automatically where possible | none | 1 min | Anywhere |

Two helper targets support the demo: `make demo-replay` plays recorded event files without the live simulator (the backup if the live run fails), and `scripts/demo_smoke.sh` runs the scripted scenarios against a running demo and checks that the expected incidents appear.

### 21.3 Quality gates (automated where possible)

| Gate | Automated check |
| --- | --- |
| G0 | `artifacts/env_report.json` exists; `docs/DEVIATIONS.md` exists |
| G1 | Tag `sim-frozen`; `configs/sim_default.yaml` hash recorded; determinism, conservation and common-random-number tests pass; calibration heat map exists |
| G2 | `make eval-detect-small` exits zero and writes `results/detect_small.csv` with rows for D0 to D3 |
| G3 | `results/arena_small.csv` has four universes for S3, S4 and S6; invariants tests SI-1 to SI-8 pass |
| G4 | `results/bench.json` exists; demo smoke test (`scripts/demo_smoke.sh`) passes twice; model load test passes |
| G5 | Tag `eval-freeze` precedes `results/summary.md`; `results/MANIFEST.json` lists git commit, config hashes, seeds and timings |
| G6 | `make submission-check` passes |

### 21.4 Reproducibility rules

- Seeds are listed in `configs/seeds.yaml` (set A seeds 1 to 999, set B 1000 to 1999, set C 2000 to 2999). No other seeds are used for reported results.
- Every results file begins with a header comment containing the git commit, the config hashes and the environment line.
- `results/MANIFEST.json` records which results were produced on the box and which elsewhere.
- `requirements.txt` pins the versions that were verified on the box. A clean clone plus `make setup test eval-detect-small` MUST work on a fresh virtual environment.
- Evaluation runs write one row per run, so any figure can be redrawn without recomputing.

### 21.5 Continuous integration substitute

There is no hosted CI during a hackathon. Instead, every pull request MUST show the output of `make test-fast`, and the lead runs `make test` and `make check-gates` at every stand-up. Failing tests on `main` stop all feature work until fixed.

## 22. Team split and agent work packets

The project is divided into sixteen self-contained work packages that a person or a coding agent can pick up by reading this document, the packet below and the sections it cites.

### 22.1 Rules file for coding agents (`AGENTS.md`, paste into the repository root)

```text
QueueShield v2 agent rules
1. Read docs/BLUEPRINT.md sections 1 and 19, then your work package (section 22) and the sections it cites.
2. Edit only files listed under your package's Outputs. If you need a change elsewhere, write it in docs/DEVIATIONS.md and ask the lead.
3. qs/schema.py is the only place for enums and record types. Do not redefine them.
4. qs/core must never import qs/sim, and qs/sim must never import qs/core.
5. All randomness comes from qs/sim/tape.py or an explicitly seeded numpy Generator. Never use unseeded randomness.
6. All thresholds, windows and parameters that appear in the blueprint come from configs/*.yaml.
7. Write the acceptance tests first, then the code. Run `make test-fast` before every commit.
8. Prefer the simplest implementation that satisfies the acceptance tests. Log every assumption or deviation in docs/DEVIATIONS.md as one line.
9. No new dependency without recording it in docs/DEVIATIONS.md; assume the target machine is s390x and big-endian.
10. Never use ground truth, scenario names or trigger times inside qs/core or qs/forecast inference.
11. Do not change frozen configs after their tag (sim-frozen, eval-freeze).
12. Code comments and documentation state facts; they never claim Telum acceleration, ML for z/OS or Secure Execution.
```

### 22.2 Roles and how to split by team size

| Role | Owns packages | Focus |
| --- | --- | --- |
| Lead (Srijaa) | WP-01, WP-15, reviews, claims | Integration, schema, scope control, report |
| R1 Simulator and evaluation | WP-02, WP-03, WP-13 | Reactive simulator, scenarios, harness, arena |
| R2 Core pipeline | WP-04, WP-05, WP-06 | Ingest, features, detectors, lifecycle, storage |
| R3 Innovations and policy | WP-07, WP-08, WP-09, WP-10 | Governor, forecaster, localizer, policy engine |
| R4 Platform and demo | WP-00, WP-11, WP-12, WP-14 | Environment, API, dashboard, deployment, benchmarks |

| Team size | Suggested split |
| --- | --- |
| 3 people | Person A: R1 plus WP-15. Person B: R2 plus WP-09 and WP-10. Person C: R4 plus WP-07 and WP-08. The lead is one of them and does WP-01 first |
| 4 people | One person per role R1 to R4; the lead floats and reviews, and takes WP-01 and WP-15 |
| 5 or more | Split R3 into R3a (WP-07, WP-09) and R3b (WP-08, WP-10); add a dedicated demo and report owner for WP-12 and WP-15 |

Principles: the person closest to the simulator owns the evaluation harness, because ground truth lives there. The person who builds the dashboard also owns deployment, because both need the box.

### 22.3 Work packages

Each packet lists the goal, what to read, files to write, how to know it is done, and what to avoid.

**WP-00 Environment and repository bootstrap** (R4, H0 to H2.5)

- Goal: verified platform and a repository everyone can use.
- Read: sections 4, 18, 19, 21.
- Outputs: `scripts/env_check.sh`, `qs/bench/env_report.py`, `qs/bench/roundtrip_check.py`, repository skeleton, `Makefile` skeleton, pinned `requirements.txt`, `artifacts/env_report.json`.
- Done when: gate G0 is green.
- Avoid: spending more than 30 minutes on any one package (use the fallback chain).

**WP-01 Schema, configuration and stubs** (Lead, H0 to H1.5)

- Goal: shared types so everyone can code in parallel.
- Read: sections 8, 10.6, 12.6, 13.10, 14.7, 15.5, 19.4.
- Outputs: `qs/schema.py`, `qs/config.py`, `qs/clock.py`, `qs/ids.py`, stub `Pipeline` and `run_scenario` that raise `NotImplementedError`, YAML config skeletons.
- Done when: the event JSON in section 8.1 round-trips; invalid enums are rejected; every other package can import the stubs.
- Avoid: adding fields that are not in section 8 without recording them.

**WP-02 Simulator engine** (R1, H2 to H6)

- Goal: a deterministic reactive discrete-event simulator.
- Read: section 9 (all), section 8.
- Outputs: `qs/sim/engine.py`, `entities.py`, `clients.py`, `routing.py`, `status_service.py`, `controls.py`.
- Done when: the tests in section 9.11 pass, except the calibration which belongs to WP-03.
- Avoid: external simulation libraries; emitting ground truth in events.

**WP-03 Scenarios, tape, ground truth, configurations, calibration** (R1, H3 to H7)

- Goal: scenarios S0 to S7, common random numbers, configuration sets A, B and C, calibrated parameters.
- Read: sections 9.5 to 9.9.
- Outputs: `qs/sim/tape.py`, `scenarios.py`, `run.py`, `scripts/make_configs.py`, `scripts/tune_metastability.py`, `configs/sim_default.yaml`, heat map figure.
- Done when: gate G1 is green.
- Avoid: changing dynamics after the `sim-frozen` tag.

**WP-04 Ingest, payment state machine, windows, features** (R2, H2 to H8)

- Goal: from ordered events to feature rows with baselines.
- Read: sections 8.3 to 8.6, 10.
- Outputs: `qs/core/ingest.py`, `dedupe.py`, `payments.py`, `windows.py`, `features.py`, `baselines.py`.
- Done when: the tests in sections 8.5 and 10.7 pass and a tick for 24 segments takes under 10 ms.
- Avoid: per-event Python objects on the hot path; reading simulator internals.

**WP-05 Detectors D0 to D3 and persistence** (R2, H6 to H10)

- Goal: four detectors and the ablation arms.
- Read: section 11.
- Outputs: `qs/core/detectors/threshold.py`, `adaptive.py`, `rules.py`, `iforest.py`, `rrcf.py`, `persistence.py`, `scripts/train_models.py` (Isolation Forest part).
- Done when: the tests in section 11.10 pass and each arm runs through the harness.
- Avoid: tuning thresholds on set C.

**WP-06 Lifecycle, degraded monitoring and storage** (R2, H8 to H11)

- Goal: incidents, deduplication, closing, degraded monitoring and persistence.
- Read: sections 8.6, 11.4 to 11.6.
- Outputs: `qs/core/lifecycle.py`, `qs/store/db.py`, `schema.sql`, `log.py`.
- Done when: lifecycle and degraded-state tests pass; batched writes keep up with 5,000 events per second.
- Avoid: writing to SQLite inside the request path.

**WP-07 Verification Governor** (R3, H10 to H13)

- Goal: budgeted verification and retry gating, plus S6 controls and the naive-verify baseline.
- Read: section 12 and section 9.4.
- Outputs: `qs/core/governor.py`, `configs/governor.yaml`, `configs/rails/upi_like.yaml`.
- Done when: the tests in section 12.7 pass and S6 shows the naive-versus-governed contrast.
- Avoid: implementing real rail rules; label presets as analogies.

**WP-08 Cascade Forecaster** (R3, H9 to H15)

- Goal: grid model first, ensemble second, evaluated on set B.
- Read: section 13.
- Outputs: `qs/forecast/dataset.py`, `train.py`, `model.py`, `grid.py`, `scripts/make_cascade_dataset.py`.
- Done when: split integrity, monotonicity and inference-time tests pass; validation AUC and calibration are reported.
- Avoid: leaking run ids across splits; using ground truth at inference.

**WP-09 Fault Localizer** (R3, H10 to H13)

- Goal: verdicts and ranked candidates with eligibility consequences.
- Read: section 14.
- Outputs: `qs/core/localizer.py`.
- Done when: the tests in section 14.7 pass and S3, S4, S6 and S7 give the right verdict on validation.
- Avoid: heavy research code; keep it a few hundred lines of NumPy.

**WP-10 Policy engine, actions and simulated operator** (R3, H12 to H16)

- Goal: recommendations with reasons, safety checks and rollback; action application in the simulator.
- Read: sections 15 and 9.4.
- Outputs: `qs/core/policy.py`, `actions.py`, `configs/policy.yaml`, `configs/operators.yaml`, operator model in `qs/eval/arena.py` (shared with WP-13).
- Done when: the tests in section 15.7 pass.
- Avoid: recommending actions that the simulator cannot apply.

**WP-11 API and live loop** (R4, H14 to H18)

- Goal: HTTP ingest, state, SSE stream, decisions and action polling; live simulator mode.
- Read: sections 16 and 19.5.
- Outputs: `qs/api/*`, live runner in `qs/sim/run.py` (agree with R1).
- Done when: a scripted client can ingest, receive a recommendation, approve it and see `ACTION_APPLIED`.
- Avoid: unauthenticated write endpoints.

**WP-12 Dashboard** (R4, H5 to H19, mock first)

- Goal: the single-page console and the policy arena tab.
- Read: sections 13.8 and 16.
- Outputs: `qs/ui/static/*`.
- Done when: the checklist in section 16.7 passes.
- Avoid: CDN links; scrolling layouts.

**WP-13 Evaluation harness, arena and reporting tables** (R1, H8 to H22)

- Goal: scoring and the four-universe comparison.
- Read: sections 11.6, 11.7, 17.
- Outputs: `qs/eval/*`, `scripts/run_eval.sh`, `results/MANIFEST.json`.
- Done when: gate G5 is green.
- Avoid: peeking at set C before the freeze.

**WP-14 Deployment and benchmarks** (R4, H16 to H20)

- Goal: demo running on the box; measured speed; model portability tests.
- Read: section 18.
- Outputs: `qs/bench/latency_bench.py`, `throughput_bench.py`, deploy scripts, `results/bench.json`.
- Done when: gate G4 is green.
- Avoid: claiming accelerator use without evidence.

**WP-15 Report, README, video and submission** (Lead plus everyone, H21 to H24)

- Goal: a submission a stranger can reproduce and a judge can follow.
- Read: sections 24, 26, 27.
- Outputs: `README.md`, report, `docs/ARCHITECTURE.md`, video, submission form.
- Done when: gate G6 is green.

### 22.4 Packet dependency map

| Packet | Needs first | Unblocks |
| --- | --- | --- |
| WP-01 | none | Everything |
| WP-02, WP-03 | WP-01 | WP-04 test data, WP-08 dataset, WP-13 |
| WP-04 | WP-01 and recorded events from WP-03 (early drop) | WP-05, WP-06, WP-07, WP-09 |
| WP-05, WP-06 | WP-04 | WP-13, WP-10 |
| WP-07, WP-09 | WP-04 | WP-10 |
| WP-08 | WP-03 and WP-04 | WP-10 severity, dashboard cascade map |
| WP-10 | WP-05, 07, 08, 09 | WP-11 recommendations, WP-13 arena |
| WP-11, WP-12 | WP-01 (mock first), then WP-10 | WP-14 |
| WP-13 | WP-03, then WP-05 | WP-15 |
| WP-14 | WP-11 | WP-15 |

### 22.5 Prompt template for starting a coding agent on a packet

```text
You are implementing work package WP-XX of QueueShield v2.
Repository: <path>. Read AGENTS.md, docs/BLUEPRINT.md sections 1 and 19, and sections <list> for this packet.
Goal: <goal line>.
Write only these files: <outputs>.
Interfaces you must respect: <signatures from sections 10.6, 12.6, 13.10, 14.7, 19.4>.
Acceptance: implement and pass these tests first: <test list from the spec sections>.
Constraints: Python <version> on s390x big-endian; no new dependencies; deterministic randomness only.
When finished: run make test-fast, summarise what you did and any deviation in one paragraph, and list open questions.
```

## 23. Risks, fallbacks and cut lines

The biggest risks are platform package gaps, a simulator that does not show metastability, and scope creep, and each has a pre-agreed fallback so no one has to decide under pressure.

### 23.1 Risk register

| # | Risk | Likelihood | Impact | Early signal | Mitigation | Fallback |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Key packages (`scikit-learn`, `pydantic`, `onnxruntime`) do not install on s390x | Medium | High | `pip install` fails in WP-00 | Package matrix with apt and source-build chain; start source builds at once | Dataclasses instead of pydantic; run training off-box and disclose; skip ONNX and say so |
| 2 | Endianness breaks a copied model | Medium | Medium | Load error or different scores | Train on the box (`make train`); round-trip check | Retrain on the box; never copy pickles |
| 3 | Simulator never shows metastability | Medium | High | Calibration sweep finds no bistable region | Section 9.9 adjustment order; time-box 90 minutes | Congestion forecaster (section 13.9); drop the word metastable from results |
| 4 | Simulator too slow for the sample sizes | Medium | Medium | Seconds per run above 20 s at G1 | Profile; vectorise random draws; parallelise | Minimum viable sample sizes (section 17.4); shorter runs only as a last resort |
| 5 | Detector does not beat D2 on some scenarios | Medium | Medium | G2 table | Tune on set B; check feature windows; fix leaks | Report honestly; emphasise localisation, forecasting and policy value; keep negative results |
| 6 | Forecaster shows no advantage over queue threshold | Medium | Medium | Validation lead-time comparison | Check the retry-backlog feature; improve the label | Keep the simpler model; present the cascade map as an explanation tool and say lead time was not improved |
| 7 | Localizer confuses bank spill-over with gateway faults | Medium | Medium | Confusion matrix on validation | Tune thresholds; test traffic shares; add the residual re-check | Report the confusion; keep `SHIFT_TRAFFIC` blocked on `MULTI_FAULT` |
| 8 | Dashboard stack fails on the box | Low to medium | Medium | Static page or SSE fails | Plain HTML and SSE only | Dashboard on a laptop pointing at the box API; disclose |
| 9 | Teammate unavailable or blocked | Medium | Medium | Missed stand-up | Packages are self-contained; any agent can take over | Lead reassigns; apply the cut ladder |
| 10 | Overclaiming under judge questioning | Medium | High | Slides say Telum or production | Claims policy (section 27); Q&A rehearsal | Say not claimed and point to the claim ladder |
| 11 | Leakage between train, validation and test | Low to medium | High | Suspiciously good test results | Disjoint configuration sets; freeze protocol; tests | Rerun with corrected splits and report the issue |
| 12 | Demo fails live | Medium | High | Rehearsal failures | Two rehearsals; scripted scenario buttons; replay mode | Play the backup video recorded at H21 and show decision log and results |
| 13 | Time overrun in the final three hours | High | High | Stage 5 starts late | Feature freeze at H20; report skeleton prepared in advance | Cut ladder below |
| 14 | Network or mirror outages on the box | Low to medium | Medium | `pip` timeouts | Download wheels early; keep a local wheelhouse | Work from the wheelhouse |

### 23.2 Cut ladder (cut from the top first)

1. NAB benchmark.
2. E-detector and conformal alert budget.
3. ONNX export beyond the load test; Snap ML and zDLC.
4. RRCF ablation arm and the EWS feature arm.
5. Sensitivity sweeps other than operator delay.
6. Scenario S7 (silent drift) and S5 (surge plus slowdown), in that order.
7. The tree-ensemble forecaster (keep the grid).
8. Policy arena scenarios other than S3, S4 and S6.
9. The Governor's breaker and the adaptive budget (keep a fixed budget).
10. Anything else needs a lead decision.

Never cut: the simulator's retry loop, D0 to D3 on a matched budget, the Governor's basic budget and retry gating, the safety invariants, the live demo, the latency benchmark, the claims policy and the README.

### 23.3 Working rules to avoid derailment

- **45-minute rule.** Any blocker lasting longer than 45 minutes triggers the stated fallback or a lead decision.
- **Two-hour rule for experiments.** An experiment that has not produced a usable result after two hours is cut or replaced by its fallback.
- **No new features after H20.** Only fixes, documentation and evaluation.
- **One writer per file.** Conflicting edits are resolved by the package owner.
- **Sleep and food.** A 24-hour build rewards rotating rest. Schedule two 90-minute rests per person between H6 and H18 so that at least two people are always working.
- **Truth over polish.** If a number is disappointing, report it with the explanation. A credible negative result beats a hidden one.

## 24. Demo script and submission checklist

The demo is a four-minute story told on the live dashboard: false alarms avoided, the right segment named, a retry storm forecast and contained, and a verification storm that the Governor prevents.

### 24.1 Four-minute script (adapt to the video limit)

| Time | Screen | What to say | What to click |
| --- | --- | --- | --- |
| 0:00 to 0:20 | Dashboard, normal traffic | One sentence on the problem: payment faults start small, retries amplify them, and a timeout does not mean failure. One sentence on UPI: status-check floods contributed to the 2025 outages | None |
| 0:20 to 0:45 | Header and performance panel | Core path runs on IBM LinuxONE (read the environment line: architecture, CPUs). Show measured events per second and p99 decision latency | None |
| 0:45 to 1:10 | Surge scenario | Demand doubles, latency stays stable, queue drains. QueueShield labels it a surge and stays quiet. Show the fixed-threshold monitors also quiet and a false-alert count of zero | Surge button |
| 1:10 to 1:55 | Bank slowdown (S3) | Heat map lights one bank column across all gateways. Localizer says bank-side. The failover action is blocked with the reason shared bank, which the naive policy would have taken. Approve throttle and governed verification | Bank slowdown, then Approve selected |
| 1:55 to 2:50 | Retry storm (S4), then Policy arena tab | Detector race: the fixed threshold is late, the adaptive detector fires later, QueueShield names the gateway first. Cascade map crosses the point of no return and shows a countdown. Approve the package and watch recovery. Switch to the arena: same seed, four universes; the naive one stays stuck | Retry storm, Approve all, Policy arena tab |
| 2:50 to 3:25 | Verification storm (S6) | Naive verification floods the status path; the Governor's budget keeps it flat. Mention the trade-off: some payments wait longer to be resolved | Verification storm |
| 3:25 to 3:50 | Results slide | The six pre-registered hypotheses and their outcomes, including any that did not hold; simulation-only and limitations in one sentence | None |
| 3:50 to 4:00 | Closing | What runs on LinuxONE today, what we did not claim, and the roadmap (ONNX and accelerator path) | None |

A 90-second cut for a shorter limit: problem sentence, surge stays quiet, retry storm with cascade map and arena, one results line.

### 24.2 Demo preparation

- [ ] `make demo` starts API, dashboard and live simulator from a clean shell in one command.
- [ ] Seeds and scenario order are fixed in `scripts/demo.sh`; rehearsed at least twice with a stopwatch.
- [ ] Browser at 1920 by 1080, zoom set, no bookmarks bar, no notifications.
- [ ] Decision log cleared before the recording; API key in the environment.
- [ ] Policy arena results for the demo seed are pre-computed and present.
- [ ] Backup: `make demo-replay` plays recorded event files without the live simulator; the backup video exists.
- [ ] A second person can run the demo from the README alone.

### 24.3 Recording tips

- Record the backup video as soon as gate G4 is green (about H21), then re-record the final at H23 if time allows.
- Record the screen at 1080p with voice-over after a silent run so that clicks are not rushed. Keep the cursor visible and slow.
- Show the terminal with `uname -m` and the environment line once, to prove the platform.
- Keep captions short and consistent with the claims policy in section 27.

### 24.4 Submission checklist

The exact submission format is still to be confirmed (section 4). Replace this list with the official one as soon as it is known.

- [ ] Repository link works for the judges, with a clean `README.md` and quick start that reproduces the demo in under 30 minutes.
- [ ] `make test` and `make eval-detect-small` pass from a fresh clone on the box.
- [ ] `results/summary.md`, `results/MANIFEST.json` and figures F1 to F10 (or explicit notes where dropped) are committed.
- [ ] Report with: problem, solution, architecture diagram, methods, results against the pre-registered hypotheses, limitations, and the abstract-compliance table (section 26.4).
- [ ] Architecture diagram exported as an image and linked from the README.
- [ ] Demo video within the length limit, with audio, showing the platform evidence.
- [ ] Evidence of on-platform execution: `env_report.json`, benchmark results, a terminal screenshot.
- [ ] Attributions and licences: NAB, scikit-learn, FastAPI, the vendored chart library, and every paper or blog used, with links; no third-party data committed unless its licence allows.
- [ ] No secrets in the repository (`git grep` for keys), no real personal data anywhere.
- [ ] `docs/CLAIMS.md` and `docs/DEVIATIONS.md` reviewed; every claim in slides, README and report passes the claims policy.
- [ ] Team names, contact and the requested form fields completed.
- [ ] Submitted with at least 30 minutes of buffer, and the confirmation saved.

## 25. Judge Q&A preparation

Every answer states what we did, what we measured and what we do not claim, and numbers in square brackets are filled from `results/summary.md` after the evaluation freeze.

| # | Likely question | Short answer |
| --- | --- | --- |
| 1 | What problem do you solve? | Payment faults start in one gateway, bank or route; retries amplify them; operators lack early, localised, safe guidance. We detect early, forecast whether it will lock in, and recommend budgeted, status-safe actions that a human approves |
| 2 | Why not watch the platform success rate? | It is a lagging average. In our scenarios the platform-wide monitor fired after \[X s\] or never, while segment-level latency and timeout signals fired after \[Y s\] |
| 3 | How do you tell a surge from degradation? | A surge raises arrivals with stable latency and a draining queue. Degradation raises latency, timeouts or retry ratio together with queue growth. Our surge gate and the false-alert numbers on S1 are in figure F4 |
| 4 | Is your simulator realistic? | It models the mechanisms that matter for the question (queues, timeouts, retries, wasted work, shared banks, status checks), not any real gateway. We use it to compare policies on identical random draws and say so. Production adoption would need real telemetry |
| 5 | Isn't testing on your own simulator circular? | Partly, so we mitigate: held-out configurations with unseen segments and parameter ranges, strong baselines including a static retry-hygiene universe, ablations, an independent public benchmark and a report of negative results |
| 6 | Do you need machine learning at all? | We tested it: rules only, model only and combined (section 11.8). The result is \[from ablation\]. We report whichever is better and why |
| 7 | What does the forecaster forecast? | The probability that a degrading gateway locks into a self-sustaining collapse if nothing changes, learned from simulated runs. It is not a prediction of fault onset and not a real-world probability. Validation AUC \[X\], lead time \[Y s\] versus a queue-threshold alert |
| 8 | How is this different from smart routing at Razorpay or Cashfree? | They decide where to send the next payment from success-rate signals. We detect and localise degradation, forecast retry-storm lock-in, treat status checks as load and block harmful failover. The two are complementary |
| 9 | What is new compared with metastable-failure research? | That research analyses design-time vulnerability and states runtime prediction as open work. We use the same queue and retry-backlog picture at runtime, learn the boundary from Monte Carlo simulation, and connect it to operator actions. Validated only in simulation |
| 10 | Why must a human approve? | Financial and routing actions are consequential and a timeout is not a confirmed failure. Our value is earlier, better-evidenced decisions, and the operator-delay sweep shows how fast approval must be |
| 11 | Are the verification numbers from UPI? | No. NPCI's published limits motivated the design (first check after 90 s, at most three checks in two hours). Our simulator uses scaled analogues and the preset is labelled an analogy |
| 12 | How do you avoid duplicate payments? | A timed-out payment is marked unknown, status-checked within budget, and re-attempted only after a confirmed failure; idempotency keys protect replays. In governed arms duplicates are an invariant we test; in naive arms we measure them |
| 13 | Why block failover on a bank-side fault? | Gateways share banks. Moving traffic away from a gateway sends the same slow-bank traffic through the others and spreads the problem. Our localizer attributes the fault and the policy blocks the unsafe action, with the reason shown |
| 14 | Why LinuxONE, and did you use Telum? | The core path (ingest, features, scoring, forecasting, governor, logging) runs on LinuxONE with measured latency \[p50, p95, p99\] and throughput \[events per second\], hardware disclosed. We did not claim Telum acceleration unless our environment check showed it: \[state result\] |
| 15 | What are the performance numbers? | Event-to-recommendation latency p99 \[X ms\] and \[Y\] events per second on \[hardware\]. Detection delay is seconds by design because of windows and persistence; system latency is milliseconds |
| 16 | Does it scale to thousands of routes? | Not tested. The feature engine is O(1) per event and vectorised per tick, and sparse routes fall back to their parents, but the localizer and thresholds need work at that scale |
| 17 | How did you set the false-alert budget? | All detectors were tuned on validation data to one false alert per simulated hour of normal or surge traffic, then frozen before the test set. Delay comparisons are at that matched budget |
| 18 | What if the model fails or data are stale? | The system enters degraded monitoring, falls back to rule signals, and recommends only hold and escalate. It never reports healthy when it cannot see |
| 19 | What would production require? | Real payment telemetry, integration with gateway and bank status APIs and rail rules, load and safety testing, operator workflow integration, and approval from risk and compliance |
| 20 | What did not work? | \[List honestly from the report: for example scenarios where the adaptive baseline matched us, forecaster cases without lead time, spill-over confusing the localizer\] |
| 21 | Why no large language model? | The decisions need low latency, auditability and determinism. A language model could later explain incidents, but it is outside the safety-critical path and outside our scope |
| 22 | Privacy and security? | Synthetic data only; no personal identifiers in the schema; operator endpoints require a key; logs hold identifiers and codes. We do not claim certified controls or Secure Execution |
| 23 | What if operators are slow? | The operator-delay sweep (5 to 120 s) shows how benefit decays: \[result\]. Early localised alerts matter most when approval is slow |
| 24 | How does this differ from your abstract? | Same problem, scope and safeguards. We kept every promised component and added the Verification Governor, Cascade Forecaster and Fault Localizer, a reactive simulator and a policy arena. See section 26 for the compliance table |

Rehearsal rule: two teammates role-play judges and ask these questions cold; any answer that needs a number must point at a figure or table in the report.

## 26. Original idea (v1) versus QueueShield v2

v2 keeps the submitted problem, scope and safeguards intact and changes what happens inside them: it proves interventions work in a reactive simulator, forecasts cascades, localises faults to gate unsafe actions, and budgets its own safeguards.

### 26.1 In one paragraph

The original idea was a sound detector and decision-support design: segment-level anomaly detection with Isolation Forest, persistence, baselines, status-safe recommendations and a dashboard, evaluated honestly. Its weaknesses were that a replay of fixed events cannot show an intervention helping, that detection was the end point rather than a trigger for action, that the verification step could itself overload a segment, that failover advice ignored shared banks, and that the platform story was "it runs on LinuxONE" without a measurement plan. v2 closes each gap and adds a way to show it.

### 26.2 Dimension by dimension

| Dimension | v1 (submitted abstracts) | v2 | Why v2 is better | Evidence we will produce |
| --- | --- | --- | --- | --- |
| Simulator | Python generator that replays timestamped attempts and outcomes | Reactive discrete-event simulator with retry loop, wasted work, shared banks and status service; common random numbers | Interventions change outcomes, so benefits can be measured instead of asserted | Section 17 policy arena on identical seeds |
| Evidence of retry storms | Described | Calibrated so that trigger size and duration decide whether the system tips | Shows the phenomenon the proposal is named after | Figure F1 heat map |
| Detection | Isolation Forest plus persistence, fixed and adaptive baselines | Same components plus rule signals, surge gate, matched false-alert budget and ablations | Fair comparison; shows whether the model is actually needed | Figures F3 and F4, ablation table |
| Early warning | Alert on deviation | Cascade Forecaster: probability of lock-in and a countdown | Tells operators how urgently to act, not only that something is wrong | AUC, calibration, lead time |
| Localisation | Segment keys by gateway and bank; compare each segment to its baseline | Explanatory-power attribution into gateway-side, bank-side, route-specific | Names the right level and blocks harmful failover | Confusion matrix; harmful failovers in S3 |
| Verification safety | Status check before any retry | Verification Governor: delayed, jittered, budgeted checks, adaptive to spare capacity, escalation list | The safeguard cannot become the storm; grounded in NPCI's 2025 rules | S6 status-check peak and queue, naive versus governed |
| Mitigation advice | Bounded backoff, status check, alternative route if configured | Action catalog with parameters, eligibility checks, ramps, rollback conditions and reason codes | Proportionate, auditable, testable actions | Safety invariant tests SI-1 to SI-8 |
| Policy evaluation | Describe benefits as simulation outcomes | Four universes (do nothing, naive failover, static retry hygiene, QueueShield) on one random tape, plus operator-delay sweep | Honest strong baselines; shows where QueueShield wins and where it does not | F8 and the delay sensitivity plot |
| Alert statistics | Persistence to reduce noise | Matched false-alert budget; optional calibrated e-detector | Removes the objection that thresholds were hand-picked | False alerts per hour at equal budget |
| Explainability | Observed change versus recent normal values | Reason codes ranked by deviation, forecaster map, localizer shares | Operators see why, where and how urgent | Dashboard and decision log |
| Platform story | Core workload on LinuxONE; claims only if available | Environment check, package matrix, endianness plan, measured p50 p95 p99 and events per second, claim ladder | A defensible, evidence-based IBM section | `results/bench.json` and environment report |
| Dashboard stack | Streamlit (conditional on packages) | FastAPI plus static page with server-sent events; Streamlit optional for results | Avoids heavy s390x dependencies and supports live control | UI checklist 16.7 |
| Public benchmark | One NAB subset in hours 0 to 4 | Optional sanity check in Stage 5, reported separately | Protects schedule; still honest about non-payment data | Table titled as sanity check |
| Demo | Dashboard with incident view | Scripted four-minute story with a three-detector race, cascade map and paired universes | Judges remember a story, not an architecture | Demo script in section 24 |
| Claims | Conservative | Conservative and formalised: claims policy, claim ladder and Q&A | Prevents overclaiming under questioning | Section 27 |

### 26.3 Subjective scorecard (my estimate, not a measured result)

| Criterion | v1 | v2 target | Main reason for the change |
| --- | --- | --- | --- |
| Problem framing | 8.5 | 9 | Metastability and the UPI evidence anchor the story |
| Rigor and honesty | 9 | 9 | Already strong; v2 adds pre-registered hypotheses and a freeze |
| Technical novelty | 5.5 | 8 | Forecaster, localizer and governed verification beyond a generic detector |
| IBM Z and LinuxONE angle | 5 | 6.5 | Measured platform numbers; capped by what the environment exposes |
| Demo and wow factor | 5 | 8 | Reactive paired universes and a scripted story |
| 24-hour feasibility | 6.5 | 6.5 | More scope, offset by the cut ladder and early gates |

These are my judgement of the design, not predictions of the judges' scores. Feasibility is the criterion most likely to hurt us; sections 20 and 23 exist to protect it.

### 26.4 Compliance with the submitted abstracts

Every commitment is kept or explicitly changed with a reason.

| Commitment in the abstracts | Status in v2 | Where |
| --- | --- | --- |
| Three gateways with independently varied arrivals, capacity, retry delays, recovery times | Kept, with five banks added | Section 9 |
| Known incident start times and segments kept as ground truth | Kept | Section 9.7 |
| Event schema with identifiers, timing, outcome and service state | Kept and extended | Section 8 |
| Rolling features by gateway and bank, minimum samples | Kept; route level and parent fallback added | Section 10 |
| Isolation Forest plus persistence | Kept as the multivariate component of D3 | Section 11 |
| Fixed-threshold and adaptive statistical baselines | Kept (D0, D1, D2) | Section 11.1 |
| Policy checks and status verification gate | Kept and strengthened (Governor, invariants) | Sections 12 and 15 |
| Operator approval or rejection; decision and event log | Kept | Section 16 |
| Degraded-monitoring state | Kept with explicit triggers | Section 11.5 |
| Bounded backoff recommendation | Kept as `THROTTLE_RETRIES` with a retry budget | Section 15.1 |
| Alternative route only when configured, eligible and with spare capacity | Kept as `SHIFT_TRAFFIC` with eligibility checks and a shared-bank block | Section 15.3 |
| FastAPI, scikit-learn, SQLite | Kept | Section 19 |
| Streamlit dashboard | **Changed** to FastAPI plus static page, because the abstract made it conditional on package availability and Streamlit's dependencies may not exist for s390x | Section 16.1 |
| Chronological training, validation and test splits | **Adapted**: runs are independent simulations, so splits are by disjoint seed ranges and held-out configurations; baselines and windows are causal within each run | Section 17.6 |
| NAB subset | **Moved** to an optional sanity check, reported separately | Section 17.8 |
| AIOps 2020 and Loghub as candidates | Not planned; only if everything else is finished | Section 17.8 |
| Paired tests: normal, demand surge, localised slowdown, retry storm | Kept and extended to eight scenarios | Section 9.7 |
| Detection delay, alert quality, operational effect, runtime performance | Kept; matched budget and paired statistics added | Section 17 |
| No Telum, ML for z/OS or Secure Execution claims unless demonstrated | Kept and formalised as a claim ladder | Sections 18.6 and 27 |
| Seasonal baselines, transaction-type segments, ONNX packaging (second abstract) | Seasonality and transaction type **dropped** (runs last minutes, not days); ONNX kept as a stretch with a load test | Sections 10 and 18.6 |
| CUSUM, retry amplification factor, p95 and p99 inference latency (second abstract) | Kept | Sections 10, 11 and 18 |

### 26.5 What deliberately did not change

- The problem statement and the rule that a timeout is never treated as a confirmed failure.
- Human approval for every action and no autonomous money movement.
- Separation of public-benchmark results from simulator results.
- No accuracy, latency or savings claim without a measurement behind it.

### 26.6 New risks that v2 introduces

More scope, a calibration dependency (metastability must actually appear), more models to validate and a bigger demo to rehearse. Section 23 lists each risk with a fallback, and the cut ladder removes optional parts in a fixed order so the core story survives.

## 27. Claims policy and honesty rules

Every sentence in the README, report, slides and video MUST pass this table, because a single overclaim under questioning costs more than any feature gains.

### 27.1 What we may and may not say

| Topic | We may say | We must not say | Evidence needed |
| --- | --- | --- | --- |
| Results | In our reactive simulator, on held-out configurations, QueueShield detected X sooner than Y at an equal false-alert budget | QueueShield reduces payment failures or saves money in production | Result tables with intervals |
| Prediction | We forecast whether a degradation will lock into a self-sustaining collapse if nothing changes | We predict outages before they begin | Forecaster evaluation; wording check |
| Detection timing | Detection delay is measured from injected onset | Detection is instantaneous or proactive | Detection delay table |
| Data | The simulator is synthetic; the public benchmark is non-payment server metrics, reported separately | Our results come from real payment data or from the public benchmark's payments | Section 17.8 labelling |
| Platform | The core decision path runs on IBM LinuxONE with measured latency and throughput on the disclosed hardware | LinuxONE makes it faster than other hardware, unless measured | `bench.json` and environment report |
| Accelerators | Telum or ONNX acceleration is a roadmap item, or measured only at claim-ladder level 3 | We use Telum, Spyre, Machine Learning for z/OS or Secure Execution | Section 18.6 evidence |
| UPI | NPCI limited status checks after outages it attributed to status-check call volume; our Governor follows the same principle in analogue form | We implement UPI rules or reproduce the outage | NPCI sources; preset labelled analogy |
| Safety | A timeout is treated as unknown; retries wait for a confirmed failure; invariants are tested | Duplicate payments are impossible | Tests; naive-arm measurements |
| Human control | Every action needs operator approval | The system is fully autonomous | Decision log |
| Novelty | To our knowledge from public sources we reviewed, we did not find a project combining these elements | Nobody has done this | Section 3 and sources |
| Scale | Tested on three gateways and five banks | Scales to production volumes | Not tested |
| Fairness | Baselines were tuned to the same false-alert budget on validation data | Baselines were tuned fairly in general | Section 11.7 |

### 27.2 Wording rules

- Say "in simulation" or "in our simulator" next to every outcome number.
- Write intervals or counts of missed incidents next to every delay statistic.
- Describe the forecast as "if nothing changes" and as learned from our simulator.
- Refer to the public benchmark as a sanity check on non-payment data.
- When unsure, say what we measured and stop.

### 27.3 Review procedure

1. The lead reads every slide, README paragraph, report section and the video script against this table before submission.
2. Any sentence containing Telum, z/OS, Secure Execution, production, prediction, guarantee or savings gets a second reviewer.
3. Changes after the review go through the same check.

## 28. Appendices

These appendices give copy-ready configuration skeletons, pseudocode for the trickiest components, a glossary and the sources behind the design.

### Appendix A: configuration skeletons

Values repeat the defaults in the sections cited; the YAML files are the single source of truth in code, and any change here MUST also be made in the section it mirrors.

```yaml
# configs/sim_default.yaml  (section 9.6; freeze after calibration)
topology: {gateways: 3, banks: 5, bank_shares: [0.30, 0.25, 0.20, 0.15, 0.10]}
gateway: {workers: 40, queue_bound: 400}
service_ms: {gateway_mean: 100, gateway_cv: 0.5, bank_mean: 300, bank_cv: 0.6}
demand: {total_rate_per_s: 165}
client: {timeout_ms: 3000, p_retry: 0.9, hard_decline_rate: 0.02}
profiles:
  naive: {retry_delay_ms: [500, 2000], attempt_cap: 5, idempotency: false}
  disciplined: {backoff: full_jitter, base_ms: 1000, cap_ms: 16000, attempt_cap: 3, idempotency: true}
server_defaults: {shed_expired: false, fresh_first: false}
run: {length_s: 600, warmup_s: 120, tick_s: 5}

# configs/pipeline.yaml  (sections 10 and 11)
windows: {short_s: 10, long_s: 60, tick_s: 5, bucket_s: 1, watermark_ms: 2000}
min_samples: {route_attempts_short: 30, latency_completions: 20}
baseline: {alpha: 0.02, warmup_s: 120, freeze_on_incident: true}
signals: {lat_z: 2.5, to_abs: 0.02, to_spread: 3, rej_abs: 0.01, raf_ratio: 1.25, qdelay_ratio: 1.5}
surge_gate: {arrival_z: 2.5, max_signals: 1}
persistence: {k: 3, m: 4, tau_hi: 0.999}
lifecycle: {clean_ticks_to_close: 6, reopen_window_s: 60, merge_window_s: 30}
degraded: {no_event_s: 10, model_budget_ms: 50}

# configs/governor.yaml  (section 12.2)
governor: {first_check_delay_ms: 10000, backoff_factor: 1.5, jitter: 0.2, max_checks: 3,
  horizon_ms: 120000, budget_fraction_spare: 0.10, budget_min_per_s: 1, budget_max_per_s: 10,
  burst_s: 2, breaker_open_ms: 15000, stop_codes: []}

# configs/policy.yaml  (sections 14 and 15)
forecaster: {watch: 0.3, critical: 0.5, ema_weight: 0.5}
localizer: {min_total_excess: 20, cum_share: 0.67, min_share: 0.10, other_dim_max: 0.50, hysteresis_ticks: 2}
throttle: {rate_fraction: 0.10, retry_after_ms: [2000, 8000]}
shift: {max_step: 0.25, spare_min: 0.30, trickle: 0.05, step_spacing_ms: 10000}
operator_model: {approval_delay_s: 20, min_confidence: 0.6}
```

### Appendix B: pseudocode

**B.1 Simulator main loop (section 9.10)**

```python
while heap and heap[0].t <= end_ms:
    ev = heappop(heap); now = ev.t
    if ev.kind == ARRIVAL:          start_payment(ev, tape)                  # draws bank, schedules ATTEMPT_ARRIVE
    elif ev.kind == ATTEMPT_ARRIVE: route -> gateway; reject if queue full else enqueue; try_start_service(gw)
    elif ev.kind == SERVICE_DONE:   free worker; commit or decline per tape; emit outcome unless client already timed out
                                    (then record a late commit); try_start_service(gw)
    elif ev.kind == CLIENT_TIMEOUT: if attempt unfinished: emit CLIENT_TIMEOUT; client decides retry per profile/controls
    elif ev.kind == RETRY_DUE:      create next attempt (retry_count + 1), schedule ATTEMPT_ARRIVE
    elif ev.kind == STATUS_ARRIVE:  enqueue small job; STATUS_DONE answers from payment ground truth (section 12.4)
    elif ev.kind == TICK:           sink.ingest(events_so_far); if live: apply pending actions from action_source
    elif ev.kind in (TRIGGER_START, TRIGGER_END): update multipliers for the scenario
    try_start_service(gw):          while idle worker and queue: pop (fresh-first if enabled; drop if shed_expired and expired)
```

**B.2 Per-tick pipeline (section 6.2)**

```python
def tick(now):
    rows = features.tick(now)                       # 24 segments, z-scores vs own baselines
    scores = detector.score(rows)                   # signals, surge gate, model score, persistence
    loc = localizer.localize(cell_bad, cell_expected)
    p = forecaster.predict(rows)                    # per gateway
    incidents = lifecycle.update(scores, loc, p, now)
    due = governor.due_checks(now, gateway_state)   # payments to status-check, within budget
    recs = policy.recommend(incidents, loc, p, due, gateway_state)
    store.append(rows, scores, incidents, recs)
    return TickResult(rows, scores, incidents, recs)
```

**B.3 Persistence and surge gate (section 11.2)**

```python
signals = [S_lat, S_to, S_q, S_raf, S_rej]            # booleans from z-scores
if z_arrival >= 2.5 and sum(signals) <= 1 and not queue_growing_two_ticks: return SURGE
history.append(score >= tau and sum(signals) >= 2)    # keep last m=4
if sum(history) >= 3: return WARNING
if score >= tau_hi and sum(signals) >= 3 and last_two_ticks_hi: return WARNING   # fast path
```

**B.4 Fault localizer (section 14.3)**

```python
ex = np.maximum(0, bad - expected); total = ex.sum()
if total < 20: return INSUFFICIENT_EVIDENCE
EPg = ex.sum(axis=1) / total; EPb = ex.sum(axis=0) / total
cand_g = smallest_set(EPg, cum=0.67, min_share=0.10); cand_b = smallest_set(EPb, cum=0.67, min_share=0.10)
if len(cand_g) == 1 and EPb.max() <= 0.50: return GATEWAY_SIDE(cand_g[0])
if len(cand_b) == 1 and EPg.max() <= 0.50: return BANK_SIDE(cand_b[0])
if len(cand_g) == 1 and len(cand_b) == 1 and ex[g, b] / total >= 0.5: return ROUTE_SPECIFIC(g, b)
return MULTI_FAULT if len(cand_g) > 1 or len(cand_b) > 1 else PLATFORM_WIDE
```

**B.5 Governor release (section 12.3)**

```python
for gw in gateways:
    bucket[gw].refill(rate=clamp(0.10 * spare_capacity_per_s(gw), 1, 10), burst=2s)
    if breaker_open(gw): continue
    for p in sorted(pending_due(gw), key=oldest_first):
        if p.checks >= 3 or now - p.first_timeout > 120_000: escalate(p); continue
        if bucket[gw].take(1): release(p)          # next check time = now + spacing * 1.5**p.checks, jittered
```

**B.6 Cascade labelling (section 13.3)**

```python
stuck = goodput(window=[t_end + 60s, t_end + 120s]) < 0.5 * baseline_goodput    # run-level label
for tick in ticks(t_start, t_end + 60s):
    sample = (features_observable(tick), stuck, run_id, config_id)               # split by config_id
```

### Appendix C: glossary

| Term | Meaning |
| --- | --- |
| Segment | A gateway, a bank, a gateway-bank route, or the platform as a whole |
| RAF | Retry Amplification Factor: retry attempts divided by fresh attempts |
| Metastable failure | A degraded state sustained by a feedback effect such as retries after the original trigger has ended |
| Orbit | Payments waiting to retry; in the queueing literature, the retrial pool |
| Zombie work | Requests still served after their client has timed out |
| Goodput | Rate of payments completed successfully within the deadline |
| Idempotency key | Identifier that lets a gateway recognise a repeated request for the same payment |
| Common random numbers | Using the same random draws in every compared run, so differences come from the policy only |
| Explanatory power | Share of the total anomaly that one attribute value accounts for |
| Claim ladder | Levels of evidence required before an IBM-platform claim may be made |
| Gate | A checkpoint that must be green before a stage counts as finished |

### Appendix D: sources

Researched on 17 October 2026. Two sources were read in full; the rest were reviewed through search excerpts, so check each against the original before quoting it.

**Read in full**

- Isaacs, Alvaro, Majumdar and others, Analyzing Metastable Failures (HotOS 2025): [sigops.org PDF](https://sigops.org/s/conferences/hotos/2025/papers/hotos25-106.pdf)
- Cashfree Tech, Building a Real-Time Routing System for High Volume Payment Success: [tech.cashfree.com](https://tech.cashfree.com/building-a-real-time-routing-system-for-high-volume-payment-success-at-cashfree-payments-6add2bbf01d9)

**Reviewed through excerpts: metastability and retries**

- Huang and others, Metastable Failures in the Wild (OSDI 2022): [USENIX PDF](https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf)
- Alvaro and others, Formal Analysis of Metastable Failures in Software Systems: [arXiv 2510.03551](https://arxiv.org/abs/2510.03551)
- Habibi and others, MSF-Model: [arXiv 2309.16181](https://arxiv.org/html/2309.16181)
- Farahbakhsh and others, Characterizing Metastable Faults and Failures: [arXiv 2606.00942](https://arxiv.org/pdf/2606.00942)
- Retry budgets and backoff: [Google SRE book, addressing cascading failures](https://sre.google/sre-book/addressing-cascading-failures/), [AWS Builders' Library](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/), [summary article](https://dev.to/beefedai/smart-retry-strategies-and-how-to-avoid-retry-storms-2adm)

**Payments and UPI**

- NPCI status-check limits: [MediaNama](https://www.medianama.com/2025/04/223-npci-new-guidelines-upi-outage/), [Business Standard](https://www.business-standard.com/finance/news/npci-springs-to-action-with-two-new-circulars-to-contain-upi-outages-125042901208_1.html)
- Razorpay smart routing paper: [Papers with Code entry](https://paperswithcode.com/paper/an-ai-powered-smart-routing-solution-for)
- Payment observability (vendor blog, treat as directional): [Yuno](https://www.y.uno/en/blog/payment-observability-the-metrics-that-predict-failures-before-they-happen)

**Detection, localisation and statistics**

- Multi-dimensional localisation: [PSqueeze paper](https://arxiv.org/abs/2305.03331), [PSqueeze code](https://github.com/NetManAIOps/PSqueeze)
- Streaming anomaly detection: [rrcf](https://github.com/tallarium/rrcf)
- Sequential change detection: [E-detectors](https://arxiv.org/abs/2203.03532), [multi-stream testing](https://arxiv.org/abs/2501.04130), [conformal anomaly detection](https://arxiv.org/pdf/2605.13642)
- Early-warning signals and their limits: [tipping point forecasting study](https://arxiv.org/pdf/2308.08794)
- Benchmark: [Numenta Anomaly Benchmark](https://github.com/numenta/NAB)

**Related open-source projects**

- [retry-storm-lab](https://github.com/telemetry-sh/retry-storm-lab), [Sentinel (Razorpay hackathon)](https://github.com/Manvi0408/Sentinel), [TraceAI](https://github.com/kanklc34/traceai)

**IBM platform**

- [Telum announcement](https://www.ibm.com/new/announcements/ibm-telum-processor-the-next-gen-microprocessor-for-ibm-z-and-ibm-linuxone), [AI on IBM LinuxONE](https://www.ibm.com/linuxone/ai-processor), [AI Toolkit for IBM Z and LinuxONE Redpaper](https://redbooks.ibm.com/abstracts/redp5749.html), [Triton Snap ML backend](https://community.ibm.com/community/user/blogs/vivek-mankar/2025/02/20/deploying-end-to-end-ml-pipeline-with-low-latency)
- [LinuxONE Community Cloud overview](https://community.ibm.com/community/user/blogs/bruce-gilkes1/2023/03/28/get-access-to-linuxone), [past Datathon winner write-up](https://community.ibm.com/community/user/blogs/christopher-eliot-hall/2024/04/10/ibm-z-datathon)

### Appendix E: open items to resolve in the first hours

- [ ] Official judging rubric and submission format (sections 4 and 24.4).
- [ ] Telum or accelerator availability and the AI Toolkit registry (section 18.8).
- [ ] Final team size and role assignment (section 22.2).
- [ ] Measured seconds per simulator run, then final sample sizes (section 17.4).
- [ ] Calibration outcome: metastability reproduced or congestion fallback (sections 9.9 and 13.9).
- [ ] Whether the Community Cloud instance allows more than one CPU and for how long it lives.
