# Confirmation set D (pre-registered before any set D result is looked at)

Set C was scored once for P1 to P6 and those results stand unchanged ([EVAL.md](EVAL.md), [RESULTS.md](RESULTS.md)). The exploratory ablation on set C (results/exploratory/ablation.json) suggested that the rules-only arm D3:R is no worse than the full detector D3 and raises fewer false alerts. Choosing a configuration because of set C results would be circular, so this question is tested on a **fresh set D**.

## Set D

`make_configs("D", n)` in scripts/make_configs.py: same distribution as C (three gateways, five banks, wider magnitudes), seeds 3000 to 3999, onset grid 152 to 302 s in steps of 10 (disjoint from A, B, C). 30 configurations per scenario. Nothing is tuned on D: every detector uses the parameters already frozen on set B (configs/detectors_frozen_arms.json, results/exploratory/ablation_frozen.json). The frozen configs and results/detection.json are not modified; D outputs go to results/exploratory/.

## Questions (reported whether or not they hold)

| ID | Question | Metric | Decision rule written in advance |
| --- | --- | --- | --- |
| Q1 | Does D3:R raise fewer false alerts than D3 without missing more incidents? | False alert groups per simulated hour on S0+S1 and over all quiet time; missed incidents on S2, S3, S4, S5, S7 | Q1 holds if D3:R has fewer false alerts per hour (S0+S1) and no more missed incidents in total |
| Q2 | Do the P1 and P2 findings replicate? | D3 versus D1 paired delay difference with 95 percent bootstrap interval; D3 false alerts per hour on S0+S1 against 1.5 | Replicates if the sign and rough size match set C |
| Q3 | Does the S4 arena finding replicate? | Paired differences U3 versus U0, U1, U2 on recovery, excess retries and peak queue | Replicates if the sign matches set C for each metric |
| Q4 | How do RM-EWS and RM-RRCF compare? | Same metrics as Q1 at their set B taus | Exploratory |

## Rules

1. Set D is scored once per stage. Reruns only for crashes, with the same seeds.
2. Missed incidents are reported next to every delay and never imputed.
3. Statistics as in EVAL.md (paired by seed, bootstrap intervals over seeds, Wilcoxon check).
4. If Q1 holds, the report recommends D3:R as the default detector and says it was found on C and confirmed on D. If it does not hold, the report says so and D3 stays the default.
5. Whatever happens, set C results are not rewritten.
