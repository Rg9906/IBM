# Exploratory results

Everything here is outside the pre-registered P1 to P6 ([EVAL.md](EVAL.md)); those results ([RESULTS.md](RESULTS.md)) are unchanged. Raw outputs are in `results/exploratory/`. All outcomes are in simulation unless stated; section 1 is on non-payment data and is never to be mixed with payment results.

## 1. Detector sanity check on non-payment server metrics (NAB)

`scripts/public_benchmark.py`. Data: Numenta Anomaly Benchmark, `realAWSCloudwatch` (AGPL-3.0), downloaded by the script into the git-ignored `artifacts/nab/`, not redistributed. Five test series (ec2_cpu_utilization_24ae8d, ec2_cpu_utilization_5f5533, ec2_network_in_257a54, elb_request_count_8c0756, rds_cpu_utilization_cc0c53) and five disjoint tuning series. Three detectors with the same 3-of-4 persistence rule as the payment detector: a CUSUM baseline, a windowed Isolation Forest and RRCF (window of 6 points, fitted on the first 15 percent of each series). Each threshold is bisected on the tuning series to 1.0 pooled false alerts per day, then scored once on the test series.

| Detector | threshold | tuning FA/day | test windows detected | test FA/day |
| --- | --- | --- | --- | --- |
| CUSUM | 5.46 | 0.99 | 8 of 9 | 0.54 |
| Isolation Forest | 1.008 | 0.99 | 5 of 9 | 0.03 |
| RRCF | 0.913 | 0.99 | 7 of 9 | 0.89 |

Reading: nine labelled windows is a very small sample, so differences between detectors are not meaningful; the point is that all three work on real server metrics at a comparable false-alert rate. Delay from window start (about 8.5 hours for all three) mostly reflects NAB's wide windows centred on the anomaly and is not a latency measure. A first run with empirical-CDF scores saturated at 1.0 and could not meet the budget; raw scores relative to the largest training score were used instead.

## 2. Ablation arms on the payment simulator

Frozen on set B to at most 1.5 false alert groups per simulated hour (`results/exploratory/ablation_frozen.json`), scored on set C (`ablation.json`) and the confirmation set D (`ablation_D.json`).

| Arm | tau (B) | FA/h on B | FA/h S0+S1, set C | FA/h S0+S1, set D |
| --- | --- | --- | --- | --- |
| D3 (RM, full) | 1.0 | 1.42 | 4.24 | 5.95 |
| D3:R (rules only) | none | 0.95 | 1.61 | 2.52 |
| RM-EWS (adds queue variance and autocorrelation) | 1.0 | 1.23 | 5.14 | 9.88 |
| RM-RRCF (RRCF instead of Isolation Forest) | 1.0 | 1.80 (budget not met) | 4.03 | 7.66 |

Neither the critical-slowing-down features nor RRCF helped: on D both raise more false alerts than D3 (and RM-EWS about 1.7 times as many), with no gain in detection (RM-EWS misses the same 2 S3 incidents as D3; RRCF misses none on S3 but 26 of 30 on S2 against D3's 28, still all of S7). The blueprint asked for the EWS result to be reported honestly if negative; it is negative here.

## 3. Confirmation set D (docs/EVAL_D.md, scored once)

Set D has new seeds (3000 to 3999) and a new onset grid, the same distribution as C, 30 configurations per scenario. Nothing was tuned on it; frozen parameters from set B were used.

**Q1: does D3:R raise fewer false alerts than D3 without missing more incidents? Yes.**

| | D3 (full) | D3:R (rules only) |
| --- | --- | --- |
| FA/h, S0+S1 | 5.95 | 2.52 |
| FA/h, all quiet time | 2.71 | 1.15 |
| Missed incidents, S2+S3+S4+S5+S7 (150 total) | 60 | 57 |
| Median delay S3, S4 (s) | 13 | 18 |
| Median delay S5 (s) | 18 | 18 |

The learned model buys about 5 seconds of detection delay on S3 and S4 at more than twice the false alerts. The same pattern held on set C. By the pre-registered rule, the recommendation is to use D3:R as the default detector; it was suggested by set C and confirmed on D. The frozen evaluation and the policy-arena results still used the full detector, so the default has **not** been switched in code: select it with `QS_DETECTOR_MODE=R` (API) or `Pipeline(detector_mode="R")`.

**Q2: do P1 and P2 replicate? Yes.** D3 against D1: paired median delay difference 0.0 s [0.0, 0.0], p = 0.077; D3 raises 5.95 false alerts per hour on S0+S1 against the 1.5 budget; D3 misses 28 of 30 S2 incidents and all S7, while D2 misses 4 and 0.

**Q3: does the S4 arena finding replicate? Yes, with the same signs on every metric** (30 pairs; recovery pairs only where both recovered):

| S4, U3 minus other | recovery s | excess retries | peak queue |
| --- | --- | --- | --- |
| versus U0 (18 pairs for recovery) | [-100.5, 0.0] | [-14574, -6052] | [-1051, -129] |
| versus U1 (failover) | [0.0, 0.0] | [+3182, +3981] | [+1071, +1234] |
| versus U2 (disciplined clients) | [-51, -7.5] | [+4214, +4905] | [+246, +545] |

Bracketed values are 95 percent bootstrap intervals of the paired median difference. QueueShield beats doing nothing, ties failover on recovery and loses to it on retries and queue, and recovers faster than disciplined clients but with far more retries. On S3, medians: peak queue U0 9000, U1 7362, U2 2760, U3 9000; U2 is best on every metric. Failover (U1) applied a traffic shift in 26 of 30 S3 runs and was harmful (worse peak queue or recovery than U0) in 1 of them, so P4's predicted harm is rare here.

## 4. Sensitivity sweeps on S4 (set B, 12 paired configurations, `sweeps.md`)

- **Operator delay is the dominant factor.** Median recovery by approval delay: 5 s 0, 15 s 0, 30 s 8, 60 s 26, 120 s 64, against 74 s with no action (U0). Peak queue is the same as U0 at 60 s and above; at 120 s excess retries are only about 12 percent below U0 (8,816 against 10,037). QueueShield helps only if someone approves quickly.
- Client timeout (2.5, 3, 4 s) and attempt cap (3 to 6): U3 beats U0 on peak queue and retries at every setting, and recovers (median 0 to 1 s versus about 75 s with 1 to 2 of 12 never recovering under U0).
- Utilisation: at 0.45 no run gets stuck and U3 is essentially equal to U0 (peak queue 775 against 774); at 0.65 U0 fails to recover in 8 of 12 runs while U3 recovers in all 12.
- Not run: surge-size sweeps and the low-volume-route sweep.

## 5. Caveats

- Sets C and D were each read for the ablation arms; the headline arms on C were scored once and are untouched.
- Section 1 has nine labelled windows; section 4 has twelve configurations per cell.
- The recommended default (D3:R) was never evaluated in the policy arena; U3 results are for the full detector.
- False-alert rates are counts of alert groups over a few simulated hours per set; no confidence intervals are given for them.
