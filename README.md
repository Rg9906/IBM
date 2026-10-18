# QueueShield v2

Detection, forecasting, fault localisation and operator-approved recovery for payment-gateway degradation, built for the IBM Z Datathon 2026. Everything here is evaluated **in a synthetic, reactive simulator**; no real payment data is used.

QueueShield watches payment events (attempt outcomes, retries, status checks, service snapshots), computes per-segment features (platform, gateway, bank, route), and raises incidents earlier and with better localisation than fixed thresholds. It then recommends actions (throttle retries, shed expired work, prioritise fresh attempts, verify pending payments within a budget, shift traffic). Every action needs operator approval and is logged.

Start with the pieces that matter most:

| Document | Read it for |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, module map, API, configs |
| [docs/EVAL.md](docs/EVAL.md) | Pre-registered hypotheses and evaluation protocol |
| [results/summary.md](results/summary.md) | Generated result tables (set C) |
| [docs/RESULTS.md](docs/RESULTS.md) | Plain-language reading of those results, including where QueueShield does not win |
| [docs/REPORT.md](docs/REPORT.md), [docs/DEMO.md](docs/DEMO.md) | Technical report; demo script and checklist |
| [docs/EXPLORATORY.md](docs/EXPLORATORY.md), [docs/EVAL_D.md](docs/EVAL_D.md) | NAB check, ablations, sweeps, pre-registered confirmation set D |
| [docs/CLAIMS.md](docs/CLAIMS.md) | What may and may not be said about the project |
| [docs/DEVIATIONS.md](docs/DEVIATIONS.md) | Every assumption and departure from the blueprint |
| [docs/QueueShield v2 Complete Implementation Blueprint (IBM Z Datathon 2026).md](docs/QueueShield%20v2%20Complete%20Implementation%20Blueprint%20(IBM%20Z%20Datathon%202026).md) | The full specification |

## Status

| Work package | State |
| --- | --- |
| WP-01 to WP-05: schema, simulator, scenarios, features, detectors D0 to D3 | Done |
| WP-06: incident lifecycle, degraded monitoring, SQLite store | Done |
| WP-07: Verification Governor | Done |
| WP-08: Cascade Forecaster | Done (lead time is measured against the stuck-judging window, not collapse; see RESULTS) |
| WP-09: Fault Localizer | Done (weak on S2, S3 and S7; see RESULTS) |
| WP-10: policy engine, actions, simulated operator | Done |
| WP-11: pipeline, HTTP API v1, live runner | Done (known gaps in DEVIATIONS) |
| WP-13: evaluation harness and set C results | Done, scored once |
| WP-12: dashboard (`qs/ui/static`, served at `/`) | Built and checked visually in Chrome on 2026-10-18 (live heat map, segment chart, panels and arena tab render, no console errors; one S3 run only) |
| WP-00 and WP-14: LinuxONE environment check and benchmarks | **Not done**; no platform measurements exist |
| WP-15: report and demo script | [docs/REPORT.md](docs/REPORT.md) and [docs/DEMO.md](docs/DEMO.md) written; **video not recorded** |
| `pyproject.toml`, `requirements.txt`, `Makefile` | Present. Tests verified (140 pass); the Makefile was not run (`make` is not installed on the dev machine) |
| Public benchmark (NAB), RRCF and EWS ablations, S4 sensitivity sweeps, confirmation set D | Done, exploratory: [docs/EXPLORATORY.md](docs/EXPLORATORY.md). Surge-size and low-volume-route sweeps not run |

## Setup

Requirements: Python 3.10 or newer (developed on 3.11). `requirements.txt` has lower bounds except scikit-learn, pinned to 1.8.0 because the committed forecaster model was pickled with it. The code imports:

`numpy`, `pydantic` (v2), `pyyaml`, `scikit-learn`, `rrcf`, `fastapi`, `uvicorn` (to serve the API), `pytest` (tests), and optionally `matplotlib` (the stuck heat map only). `scipy` is optional: without it the Wilcoxon p-values in `qs/eval/stats.py` are left empty.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use source .venv/bin/activate elsewhere
pip install -r requirements.txt
```

Run everything from the repository root; scripts read `configs/` and write under `artifacts/` and `results/` by relative path.

## Run the tests

`make test` is equivalent. The `Makefile` also wraps the evaluation steps below (`make -n eval-detect` shows what runs); it has not been executed end to end.

```bash
python -m pytest -q            # tests/unit, one file per work package
```

Each `tests/unit/test_wpNN.py` holds the acceptance tests for that work package. 140 tests passed on Python 3.13 with the versions in `requirements.txt`.

## Run the live demo (API only)

```bash
set QS_API_KEY=changeme        # PowerShell: $env:QS_API_KEY="changeme"
uvicorn --factory qs.api.app:create_app --port 8000
```

Set `QS_DETECTOR_MODE=R` to run the rules-only detector, which raised fewer false alerts on the confirmation set (docs/EXPLORATORY.md); the default `RM` is what the frozen results used. Open http://localhost:8000 for the console (paste the key into the box), or run `python scripts/demo.py`. Over HTTP:

```bash
curl -X POST localhost:8000/v1/sim/scenario -H "X-QS-Key: changeme" -H "Content-Type: application/json" \
     -d '{"scenario":"S4","seed":1,"speed":20,"arm":"U3","auto_operator":true}'
curl -N localhost:8000/v1/stream
curl localhost:8000/v1/state
```

If `QS_API_KEY` is unset, a random key is generated and logged once at start-up. Endpoint list: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#http-api-v1).

## Reproduce the evaluation

Disjoint configuration sets A (train), B (tune), C (test) come from `scripts/make_configs.py`. The order below is the one used for the committed results. Commands and defaults come from each script's `argparse`; run a script with `-h` for the full list.

```bash
python scripts/make_configs.py                                 # sets A, B, C -> artifacts/configs
python scripts/tune_metastability.py                           # calibration sweep (blueprint 9.9)
python scripts/make_cascade_dataset.py --set A --n 600         # likewise --set B --n 200, --set C --n 200
python scripts/train_models.py                                 # Isolation Forest -> artifacts/models/iforest.pkl
python -m qs.forecast.train                                    # Cascade Forecaster (train A, select B, report C)
python scripts/cache_rows.py --set B --n 15                    # per-tick feature rows (use --set C --n 30)
python scripts/tune_detectors.py --set B --budget 1.5          # equal false-alert budget -> configs/detectors_frozen*.{yaml,json}
python scripts/eval_localizer.py                               # localiser validation on set B
python scripts/run_eval.py detect --set C                      # P1, P2
python scripts/run_eval.py arena  --set C --scenarios S3,S4    # P3, P4
python scripts/run_eval.py s6                                  # P5
python scripts/run_eval.py forecast                            # P6
python scripts/run_eval.py report                              # writes results/summary.md

# Exploratory (outputs under results/exploratory/; never touch the frozen configs or results/detection.json)
python scripts/public_benchmark.py                             # NAB sanity check; downloads data to artifacts/nab (git-ignored)
python scripts/run_ablation.py train && python scripts/run_ablation.py tune && python scripts/run_ablation.py score
python scripts/run_sweeps.py                                   # S4 sensitivity sweeps on set B
python scripts/cache_rows.py --set D --n 30                    # confirmation set D (docs/EVAL_D.md); rows are git-ignored
python scripts/run_eval.py detect --set D --detect-out exploratory/detection_D.json
python scripts/run_ablation.py score --set D
python scripts/run_eval.py arena --set D --n 30 --scenarios S3,S4 --out exploratory/arena_D.json
```

Set C must be scored once (EVAL rule 6). `results/MANIFEST.json` records the git commit, config hashes and stage timestamps for the committed run. The exact sizes and arguments used for the committed results are not recorded beyond that manifest and `results/summary.md`; the dataset sizes are in DEVIATIONS (WP-08, WP-13).

## Repository layout

```text
configs/     frozen and default YAML/JSON parameters (simulator, detectors, governor, policy, operators, rails)
docs/        blueprint, evaluation protocol, deviations, architecture, claims, results reading
qs/          the package; see docs/ARCHITECTURE.md
results/     generated evaluation outputs and summary.md
scripts/     calibration, dataset, training, tuning and evaluation entry points
tests/unit/  acceptance tests per work package
artifacts/   generated models, datasets, cached rows and intermediate results (committed for reproducibility of results only)
```

`prof.out` in the repository root is a profiling dump and not part of the system.

## Honest scope

- Simulator-only evidence. Outcome numbers say "in simulation".
- Three gateways and five banks. Scale beyond that is untested.
- The forecaster answers "will this degradation lock into a self-sustaining collapse if nothing changes?". It does not give early warning of the collapse itself (DEVIATIONS WP-08).
- No claim is made about Telum, Spyre, Machine Learning for z/OS or Secure Execution, and no LinuxONE measurement exists yet.
- The full rules are in [docs/CLAIMS.md](docs/CLAIMS.md).
