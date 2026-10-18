# QueueShield v2: technical report

IBM Z Datathon 2026. Everything below is measured **in a synthetic, reactive payment simulator**; there is no real payment data and no IBM LinuxONE measurement. Numbers are copied from [results/summary.md](../results/summary.md); the reading of them is in [RESULTS.md](RESULTS.md).

## 1. Problem

Payment gateways degrade in ways that average success rates hide: a slowdown raises latency, clients time out and retry, retries add load, and the queue can stay saturated after the original cause ends (metastable failure). A timeout is not a failure, so naive status checks and retries can create duplicates or a status-check storm. Typical alerting watches platform success rate or fixed per-route thresholds.

## 2. System

QueueShield consumes payment events and runs: windowed features per segment (platform, gateway, bank, route) with adaptive baselines; detectors (D3 = rules + surge gate + Isolation Forest with persistence); a Cascade Forecaster (`p_stuck`: will this degradation lock into a self-sustaining collapse if nothing changes); a Fault Localizer (route, gateway, bank, platform, multi-fault, insufficient evidence); a Verification Governor (bounded, jittered status checks); and a policy engine that recommends throttling, shedding expired work, prioritising fresh attempts, governed verification and traffic shift. Every action requires an operator decision and has a rollback. Details: [ARCHITECTURE.md](ARCHITECTURE.md).

## 3. Method

A deterministic discrete-event simulator with eight scenarios (S0 normal, S1 benign surge, S2 gateway slowdown, S3 bank slowdown, S4 retry-storm trigger, S5 surge plus slowdown, S6 verification storm, S7 silent drift) and common random numbers. Configurations are split into disjoint training (A), tuning (B) and test (C) sets. Each detector has one sensitivity parameter tuned on B to an equal false-alert budget (1.5 merged false alerts per simulated hour, relaxed from 1.0; DEVIATIONS WP-13). Hypotheses P1 to P6 were committed before set C was scored ([EVAL.md](EVAL.md)); set C was scored once. Policy universes: U0 nothing, U1 textbook failover, U2 disciplined clients, U3 QueueShield with a simulated operator (20 s approval delay), paired by seed. Statistics: medians with IQR, paired median differences with 95 percent bootstrap intervals, Wilcoxon signed-rank.

## 4. Results (set C, in simulation)

| Hypothesis | Outcome | Key numbers |
| --- | --- | --- |
| P1 D3 detects localised incidents sooner | Not supported | Median delay 15 s (S3, S4, S5), equal to D1 (paired difference 0.0 s, p = 0.111). D3 ahead of D0 by 2.5 s and of D2 by 5 s where both detect. D3 misses 29/30 on S2 and 30/30 on S7; D2 detects S7 (38 s) and misses 3/30 on S2 |
| P2 D3 within false-alert budget on surges | Not met | 4.24 false alert groups per hour on S0+S1 against 1.5; D0 1.21, D1 1.92, D2 2.52 |
| P3 U3 recovers faster with fewer retries | Partly | S4 against U0: recovery 99 s faster, 9,396 fewer excess retries, peak queue 1,178 lower. Against U1 (failover): equal recovery, more excess retries (+3,455) and higher peak queue (+1,079). Against U2: 31 s faster recovery, far more retries (4,496 vs 7 median) |
| P4 localisation prevents harmful failover | Not demonstrated | U1 made no S3 run worse (0 of 14 runs with a failover); U3 applied none |
| P5 governed verification lowers the status-check peak | Supported | 517.8 to 13.0 checks/s, peak queue 230 to 4, no duplicates; resolution median 108 s vs 3 s, 573 payments escalated |
| P6 forecaster: lead time with low false criticals | Not as worded | AUC 0.914, Brier 0.117; no positive lead against collapse (alarm 25 s after it); lead to the stuck-judging window 99.5 s median; false criticals 0 (S0), 7.3 (S1), about 18 (S5) per hour |

## 5. Discussion

What worked: the Governor removes the verification storm at a stated cost; QueueShield clearly beats doing nothing on the retry storm; the pipeline runs closed loop with operator approval, rollback and a decision log; the evaluation was pre-registered and the negative results are reported.

What did not: the learned model added nothing over the rules (D3 equals D3:R), the false-alert budget is not held out of sample, localisation of bank-side faults is weak when a slow bank saturates every gateway, and simple baselines (failover for a capacity loss, disciplined retry hygiene) are strong. Where U1 or U2 beats U3 this is reported rather than hidden.

## 5b. Exploratory results

Full detail in [EXPLORATORY.md](EXPLORATORY.md). (1) Rules only (D3:R) beat the full detector on the pre-registered confirmation set D: 2.52 against 5.95 false alert groups per hour on S0+S1, 57 against 60 missed incidents, at the cost of about 5 s of detection delay; the learned model is therefore not justified by these results. (2) RRCF and critical-slowing-down features did not help. (3) On the public NAB server-metric series the three generic detectors (CUSUM, Isolation Forest, RRCF) detect 5 to 8 of 9 labelled windows at about one false alert per day; this is a sanity check on non-payment data, not evidence about payments. (4) The S4 benefit of QueueShield depends on the approval delay: recovery 0 s at 5 and 15 s, 64 s at 120 s against 74 s for doing nothing. (5) The S4 arena findings replicate on set D with the same signs.

## 6. Threats to validity

- The simulator is ours; mechanisms (stale-queue lock-in, retry amplification) are plausible, not validated against real traffic. The metastability calibration changed the queue limit from 400 to 3000 (DEVIATIONS WP-03).
- Three gateways and five banks; nothing is known about scale.
- S1 and S5 surge flashes were capped at 95 percent utilisation after set B revealed that the original spike was a genuine overload (WP-13). The surge gate misses its 95 percent labelling target.
- The false-alert budget was tuned on B and is exceeded on C; the budget was relaxed once.
- Operator behaviour is a simulated approval delay and confidence threshold, not human behaviour.
- The forecast is trained on post-trigger ticks only; a quiet-traffic guard is a patch (WP-11).
- Sample sizes are small for some comparisons (S4 recovery uses 9 pairs); intervals are shown.
- The NAB check uses nine labelled windows. Set D was read only after its questions were registered, but the question itself came from set C results.

## 7. Reproduction

`requirements.txt` and `Makefile` (see README); `results/MANIFEST.json` stores the commit and config hashes of the committed run. Unit and API tests: 140 tests pass on Python 3.13 (`python -m pytest -q`).

## 8. Not done

LinuxONE environment check, latency and throughput benchmarks, any Telum or accelerator work; surge-size and low-volume-route sweeps; a recorded video; a visual check of the dashboard in a browser (the browser tool was unavailable; the console was exercised through its API and the JS was syntax-checked only).
