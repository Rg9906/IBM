# Evaluation protocol (pre-registered)

Committed before any set C result is looked at. Blueprint sections 11.6, 11.7, 17.

## Primary hypotheses (reported whether or not they hold)

| ID | Hypothesis | Metric | Comparison |
| --- | --- | --- | --- |
| P1 | D3 detects localised incidents sooner than D0, D1 and D2 | Detection delay, missed incidents | S2, S3, S4, S5, S7 at the matched false-alert budget |
| P2 | D3 stays within the false-alert budget on surges | False alerts per simulated hour | S0 and S1, all detectors |
| P3 | U3 recovers faster with fewer wasted retries than U0 and U1 | Recovery time, excess retries, peak queue | S4, paired by seed |
| P4 | Localisation prevents harmful failover | Failovers that raise peak queue or recovery time | S3, U1 versus U3 |
| P5 | Governed verification gives a lower status-check peak than naive verification | Peak status-check rate, peak queue | S6 |
| P6 | The forecaster gives positive lead time with a low false-critical rate | Lead time, false critical per hour | S4, S1, S5, short triggers |

## Rules

1. Training set A, tuning set B, test set C are disjoint (scripts/make_configs.py). Thresholds and model fits use A and B only.
2. Equal false-alert budget: each detector has one sensitivity parameter, bisected on set B to at most 1 merged false alert per simulated hour (S0, S1 and pre-onset ticks of incident runs). Values are written to configs/detectors_frozen.yaml before set C is scored.
3. A false alert is an alert group (overlapping segments within 30 s merged) that starts outside every incident window plus 30 s grace. Platform D0 alerts are grouped separately.
4. Missed incidents are reported next to every delay statistic and never imputed.
5. Pair by seed; report medians with IQR, paired median differences with 95 percent bootstrap intervals over seeds (10,000 resamples) and a Wilcoxon signed-rank check.
6. Set C is scored once. Reruns only for crashes, with the same seeds.
7. Everything outside P1 to P6 is exploratory.
8. Sizes actually used: detection on set C, 30 configurations per scenario; set B, 15 per scenario; policy arena and sweeps as budget allows (see results/summary.md).

## Known limits stated in advance

- The S1 surge gate does not meet the 95 percent labelling target (DEVIATIONS, WP-05); P2 results include that effect.
- Forecaster lead time is measured against the stuck-judging window, not against collapse (DEVIATIONS, WP-08).
- Localizer is validated on S2, S3, S4, S6, S7 only; S2 and S7 verdicts are often INSUFFICIENT_EVIDENCE (DEVIATIONS, WP-09).
