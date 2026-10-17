# QueueShield v2 (project instructions)

Payment-gateway degradation detection, forecasting, localisation and operator-approved recovery, evaluated in a synthetic simulator (IBM Z Datathon 2026). Python package `qs/`. See README.md for status, docs/ARCHITECTURE.md for the module map, docs/RESULTS.md for what the numbers show.

## Rules (also in docs/Agents.md; those are binding)

- `qs/schema.py` is the only place for enums and record types.
- `qs/core` must never import `qs/sim`, and `qs/sim` must never import `qs/core`. Joining code lives in `qs/api/live.py` and `qs/eval/`.
- All randomness comes from `qs/sim/tape.py` or an explicitly seeded numpy Generator.
- Thresholds and parameters come from `configs/*.yaml`. Do not change frozen configs (`detectors_frozen*`, `sim_default.yaml`) after the freeze; `results/MANIFEST.json` hashes them.
- No ground truth, scenario names or trigger times inside `qs/core` or `qs/forecast` inference.
- Record every assumption, deviation or new dependency as one line in docs/DEVIATIONS.md.
- Never claim Telum, ML for z/OS, Secure Execution, production results or outage prediction. Follow docs/CLAIMS.md. Put "in simulation" next to outcome numbers.
- Set C is scored once (docs/EVAL.md). Do not tune on it.

## Commands

A `Makefile` wraps these (untested end to end). Run from the repository root.

- Tests: `python -m pytest -q` (one file per work package in `tests/unit/`)
- API: `uvicorn --factory qs.api.app:create_app --port 8000` with `QS_API_KEY` set
- Evaluation pipeline: see README "Reproduce the evaluation"

## Environment notes

- The machine's default Python had no pytest installed when the docs were written; use a venv with the packages listed in README.
- Windows host, PowerShell or Git Bash. SSL interception on the dev machine required `pip --trusted-host` earlier (DEVIATIONS WP-01).
- `prof.out` and `artifacts/rows/*.pkl` are generated; do not edit.

## Current status and open items

Done: WP-01 to WP-11 and WP-13 (committed results on set C). Not done: WP-12 dashboard (no `qs/ui`), WP-00/WP-14 LinuxONE check and benchmarks, WP-15 video and submission, pyproject and exact dependency pins (`requirements.txt` has lower bounds only), public benchmark, further ablations.

Known issues worth knowing before changing things:
- D3 does not beat D1 on delay and misses S2 and S7 almost entirely; it exceeds the false-alert budget on S0+S1 (4.24/h vs 1.5) on set C.
- Forecaster has no positive lead time against collapse and 7 to 18 false criticals per hour on S1/S5; trained on post-trigger ticks only, patched by a quiet-traffic guard (DEVIATIONS WP-11).
- The localizer gives INSUFFICIENT_EVIDENCE on most S2/S7 and weak bank-side results on S3.
- The closed-loop run on B-S4-000 raised a spurious second incident whose cause is not investigated.

Keep this section aligned when work packages complete.
