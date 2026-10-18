# Claims policy

Every sentence in the README, report, slides and video must pass this table (blueprint section 27). When unsure, say what was measured and stop.

| Topic | We may say | We must not say |
| --- | --- | --- |
| Results | In our reactive simulator, on held-out configurations, X compared with Y at an equal false-alert budget (with the numbers in [RESULTS.md](RESULTS.md)) | QueueShield reduces payment failures or saves money in production |
| Detection | The rules-only variant raised fewer false alerts than the full detector on two simulator test sets, at about 5 s more delay. Delay is measured from injected onset; missed incidents are reported next to every delay | Detection is instantaneous or proactive; that D3 beats the baselines in general (it does not; see RESULTS P1) |
| Prediction | The forecaster estimates whether a degradation will lock into a self-sustaining collapse if nothing changes, learned from our simulator | We predict outages before they begin; any lead time against collapse (measured lead time is to the stuck-judging window) |
| Data | The simulator is synthetic; no real payment data is used. The NAB check is on non-payment server metrics, titled "Detector sanity check on non-payment server metrics", with nine labelled windows | Results come from real payment data |
| Platform | The code is plain Python. No LinuxONE measurement has been made yet | LinuxONE makes it faster; we use Telum, Spyre, Machine Learning for z/OS or Secure Execution |
| UPI | NPCI limited status checks after outages it attributed to status-check call volume; our Governor follows the same principle in analogue form (`configs/rails/upi_like.yaml` is an analogy preset) | We implement UPI rules or reproduce the outage |
| Safety | A timeout is treated as unknown; retries wait for a confirmed failure; the invariants are covered by tests | Duplicate payments are impossible |
| Human control | Every action needs operator approval, including rollbacks | The system is autonomous |
| Novelty | To our knowledge from the public sources we reviewed, we did not find a project combining these elements | Nobody has done this |
| Scale | Tested on three gateways and five banks | Scales to production volumes |
| Fairness | Baselines were tuned on set B to the same false-alert budget (1.5 per simulated hour, relaxed from 1.0) | Baselines were tuned fairly in general |
| Governor | Peak status-check rate fell from 517.8 to 13.0 per second in S6, at the cost of slower resolution (median 108 s versus 3 s) | The Governor is free of side effects |

## Wording rules

- Put "in simulation" next to every outcome number.
- Write intervals or counts of missed incidents next to every delay statistic.
- Describe the forecast as "if nothing changes".
- Report the cases where a baseline wins (U1 on S4 retries and queue, U2 on S3 and S4 retries, D2 on S2 and S7 coverage).
- Any sentence containing Telum, z/OS, Secure Execution, production, prediction, guarantee or savings gets a second reviewer.
