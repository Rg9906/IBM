# Targets mirror the README "Reproduce the evaluation" order. Run from the repository root.
PY ?= python

.PHONY: nab ablation sweeps demo export-log test serve configs calibrate datasets train rows tune eval-detect eval-arena eval-s6 eval-forecast report

test:
	$(PY) -m pytest -q

serve:
	$(PY) -m uvicorn --factory qs.api.app:create_app --port 8000

configs:
	$(PY) scripts/make_configs.py

calibrate:
	$(PY) scripts/tune_metastability.py

datasets:
	$(PY) scripts/make_cascade_dataset.py --set A --n 600
	$(PY) scripts/make_cascade_dataset.py --set B --n 200
	$(PY) scripts/make_cascade_dataset.py --set C --n 200

train:
	$(PY) scripts/train_models.py
	$(PY) -m qs.forecast.train

rows:
	$(PY) scripts/cache_rows.py --set B --n 15
	$(PY) scripts/cache_rows.py --set C --n 30

tune:
	$(PY) scripts/tune_detectors.py --set B --budget 1.5

# Set C is scored once (docs/EVAL.md rule 6); rerun only after a crash.
eval-detect:
	$(PY) scripts/run_eval.py detect --set C
eval-arena:
	$(PY) scripts/run_eval.py arena --set C --scenarios S3,S4
eval-s6:
	$(PY) scripts/run_eval.py s6 --set C
eval-forecast:
	$(PY) scripts/run_eval.py forecast --set C

report:
	$(PY) scripts/run_eval.py report

demo:
	$(PY) scripts/demo.py

export-log:
	curl -s localhost:8000/v1/decisions.csv -o artifacts/results/decision_log.csv

nab:
	$(PY) scripts/public_benchmark.py

ablation:
	$(PY) scripts/run_ablation.py train
	$(PY) scripts/run_ablation.py tune
	$(PY) scripts/run_ablation.py score

sweeps:
	$(PY) scripts/run_sweeps.py
