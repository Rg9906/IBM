"""Evaluation harness (blueprint 17). Stages write JSON under results/; `report` builds results/summary.md.

  run_eval.py detect   --set C        P1, P2: D0-D3 and ablation arms at the frozen equal false-alert budget
  run_eval.py arena    --set C --n 20 P3, P4: U0-U3 on S2-S6, paired by seed
  run_eval.py s6       --set C --n 10 P5: naive versus governed verification
  run_eval.py forecast --set C        P6: false-critical rate on healthy and surge runs
  run_eval.py report                  results/summary.md and results/MANIFEST.json
Set C is scored once for the headline results (docs/EVAL.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.eval import detection as D  # noqa: E402
from qs.eval import rowcache  # noqa: E402
from qs.eval.stats import iqr, paired_bootstrap  # noqa: E402

RES = ROOT / "results"
MODEL = "artifacts/models/iforest.pkl"
KEEP = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
        "client_timeout_ms", "attempt_cap", "flash_mult"}
DETECT_SCEN = ["S2", "S3", "S4", "S5", "S7"]
ARMS = ["D0", "D1", "D2", "D3", "D3:M", "D3:R"]


def _save(name: str, obj) -> None:
    RES.mkdir(exist_ok=True)
    (RES / name).write_text(json.dumps(obj, indent=1, default=str))


def _load(name: str):
    return json.loads((RES / name).read_text())


def frozen_specs() -> dict:
    fz = json.loads((ROOT / "configs" / "detectors_frozen_arms.json").read_text())
    base = {"D0": {"det": "D0"}, "D1": {"det": "D1"}, "D2": {"det": "D2"}, "D3": {"det": "D3", "mode": "RM"},
            "D3:M": {"det": "D3", "mode": "M", "name": "D3:M"}, "D3:R": {"det": "D3", "mode": "R", "name": "D3:R"}}
    out = {}
    for k, spec in base.items():
        if k in fz:
            out[k] = dict(spec, param=fz[k]["param"]) if fz[k]["param"] is not None else dict(spec)
    return out


# ------------------------------------------------------------------ detection
def stage_detect(a) -> None:
    specs = frozen_specs()
    paths = D.paths(a.set)
    t0 = time.perf_counter()
    scored: dict = {}
    with ProcessPoolExecutor() as ex:
        for arm, spec in specs.items():
            res = list(ex.map(D.run_alerts, [(p, spec, MODEL) for p in paths], chunksize=2))
            scored[arm] = [D.score_run(r) for r in res]
            print(f"{arm}: scored {len(res)} runs", flush=True)
    summary: dict = {"set": a.set, "n_runs": len(paths), "arms": {}, "pairs": {}, "wall_s": time.perf_counter() - t0}
    for arm, sc in scored.items():
        per: dict = {}
        for s in sc:
            e = per.setdefault(s["scenario"], {"n_incidents": 0, "missed": 0, "delays": [], "false_groups": 0, "quiet_s": 0})
            e["n_incidents"] += s["n_incidents"]
            e["missed"] += s["missed"]
            e["delays"] += s["delays"]
            e["false_groups"] += s["false_groups"]
            e["quiet_s"] += s["quiet_s"]
        for e in per.values():
            e["median_iqr"] = iqr(e["delays"])
            e["fa_per_hour"] = 3600 * e["false_groups"] / max(1.0, e["quiet_s"])
        allfa = sum(s["false_groups"] for s in sc)
        allq = sum(s["quiet_s"] for s in sc)
        surge = [s for s in sc if s["scenario"] in ("S0", "S1")]
        summary["arms"][arm] = {"by_scenario": per, "fa_per_hour_all": 3600 * allfa / max(1.0, allq),
                                "fa_per_hour_S0_S1": 3600 * sum(s["false_groups"] for s in surge) / max(1.0, sum(s["quiet_s"] for s in surge)),
                                "spec": specs[arm]}
    # paired delays D3 versus each other arm (incidents both detected), plus miss counts
    def delays(arm):
        d = {}
        for s in scored[arm]:
            if s["scenario"] in DETECT_SCEN:
                d[s["config_id"]] = (s["delays"][0] if s["delays"] else None)
        return d
    d3 = delays("D3")
    for arm in scored:
        if arm == "D3":
            continue
        other = delays(arm)
        both = [c for c in d3 if d3[c] is not None and other.get(c) is not None]
        pb = paired_bootstrap([other[c] for c in both], [d3[c] for c in both])
        pb.update({"d3_only_detected": sum(1 for c in d3 if d3[c] is not None and other.get(c) is None),
                   "other_only_detected": sum(1 for c in d3 if d3[c] is None and other.get(c) is not None)})
        summary["pairs"][arm] = pb
    _save("detection.json", summary)
    print(json.dumps({k: v["fa_per_hour_all"] for k, v in summary["arms"].items()}, indent=1))


# ------------------------------------------------------------------ policy arena
def arena_job(args: tuple) -> dict:
    from qs.eval.universes import run_universe
    from qs.sim.scenarios import ScenarioParams
    cfg, uni, delay = args
    p = ScenarioParams.from_dict({k: v for k, v in cfg.items() if k in KEEP})
    t0 = time.perf_counter()
    m = run_universe(uni, p, cfg["seed"], operator_delay_ms=delay)
    m["wall_s"] = time.perf_counter() - t0
    m["operator_delay_ms"] = delay
    return m


def stage_arena(a) -> None:
    scen = a.scenarios.split(",")
    cfgs = [c for c in make_configs(a.set, a.n, 0) if c["scenario"] in scen]
    unis = a.universes.split(",")
    delays = [int(x) for x in a.delays.split(",")]
    jobs = [(c, u, d) for c in cfgs for u in unis for d in (delays if u == "U3" else [20_000])]
    print(f"{len(jobs)} arena runs", flush=True)
    t0 = time.perf_counter()
    out = []
    with ProcessPoolExecutor() as ex:
        for i, m in enumerate(ex.map(arena_job, jobs, chunksize=1)):
            out.append(m)
            if i % 20 == 0:
                print(f"{i + 1}/{len(jobs)}", flush=True)
    _save(a.out, {"set": a.set, "wall_s": time.perf_counter() - t0, "runs": out})


def summarize_arena(runs: list, metric: str, lower_better: bool = True) -> dict:
    """Median [IQR] per scenario and universe for the main (20 s) operator delay."""
    out: dict = {}
    for r in runs:
        if r["universe"] == "U3" and r["operator_delay_ms"] != 20_000:
            continue
        v = r[metric]
        if v is None:
            v = float("nan")
        out.setdefault(r["scenario"], {}).setdefault(r["universe"], []).append(v)
    return {sc: {u: iqr([x for x in vs if x == x]) + (len([x for x in vs if x != x]),) for u, vs in d.items()}
            for sc, d in out.items()}


def arena_pairs(runs: list, scenario: str, metric: str, a: str, b: str) -> dict:
    by = defaultdict(dict)
    for r in runs:
        if r["scenario"] == scenario and not (r["universe"] == "U3" and r["operator_delay_ms"] != 20_000):
            by[r["config_id"]][r["universe"]] = r[metric]
    pairs = [(v[a], v[b]) for v in by.values() if a in v and b in v and v[a] is not None and v[b] is not None]
    return paired_bootstrap([x for x, _ in pairs], [y for _, y in pairs])


# ------------------------------------------------------------------ S6 verification
def s6_job(args: tuple) -> dict:
    import s6_contrast
    from qs.sim.scenarios import ScenarioParams
    cfg, arm = args
    p = ScenarioParams.from_dict({k: v for k, v in cfg.items() if k in KEEP})
    r = s6_contrast.run_arm(arm, p, cfg["seed"])
    r["config_id"] = cfg["config_id"]
    return r


def stage_s6(a) -> None:
    cfgs = [c for c in make_configs(a.set, a.n, 0) if c["scenario"] == "S6"][: a.n]
    jobs = [(c, arm) for c in cfgs for arm in ("no_verification", "naive_verify", "governed")]
    with ProcessPoolExecutor() as ex:
        out = list(ex.map(s6_job, jobs, chunksize=1))
    _save("s6.json", {"set": a.set, "runs": out})


# ------------------------------------------------------------------ forecaster false criticals
def stage_forecast(a) -> None:
    from qs.forecast.model import CascadeForecaster
    out: dict = {}
    for scen in ("S0", "S1", "S5"):
        secs, edges5, edges9, per = 0.0, 0, 0, []
        for p in D.paths(a.set):
            run = rowcache.load(p)
            if run["config"]["scenario"] != scen:
                continue
            fc = CascadeForecaster()
            fc.load("artifacts/models/forecaster")
            pk = run["pk"]
            prev5, prev9 = set(), set()
            for i in range(len(pk["ts"])):
                ps = fc.predict(rowcache.rows_at(pk, i))
                now5 = {k for k, v in ps.items() if v >= 0.5}
                now9 = {k for k, v in ps.items() if v >= fc.critical}
                edges5 += len(now5 - prev5)
                edges9 += len(now9 - prev9)
                prev5, prev9 = now5, now9
            secs += float(pk["ts"][-1]) / 1000.0
            per.append(p)
        out[scen] = {"runs": len(per), "sim_hours": secs / 3600.0, "critical_events_at_0.5": edges5,
                     "critical_events_at_tuned": edges9,
                     "per_hour_at_0.5": edges5 / max(secs / 3600.0, 1e-9),
                     "per_hour_at_tuned": edges9 / max(secs / 3600.0, 1e-9)}
    _save("forecast_false_critical.json", out)
    print(json.dumps(out, indent=1))


# ------------------------------------------------------------------ report
def fmt(t) -> str:
    if t is None or t[0] is None:
        return "-"
    return f"{t[0]:.0f} [{t[1]:.0f}-{t[2]:.0f}]"


def stage_report(a) -> None:
    L = ["# Evaluation summary", "", "Generated by scripts/run_eval.py. Protocol: docs/EVAL.md. All numbers are from the "
         "simulator; see docs/DEVIATIONS.md for known limits.", ""]
    if (RES / "detection.json").exists():
        d = _load("detection.json")
        L += ["## P1, P2: detection at the equal false-alert budget (set %s, %d runs)" % (d["set"], d["n_runs"]), "",
              "Frozen parameter per arm (configs/detectors_frozen_arms.json). Delay is seconds from onset, median [IQR]; "
              "`missed` counts incidents with no valid alert in 300 s and is never imputed.", "",
              "| Arm | param | " + " | ".join(f"{s} delay (missed/n)" for s in DETECT_SCEN) + " | FA/h all | FA/h S0+S1 |",
              "| --- | --- | " + " | ".join("---" for _ in DETECT_SCEN) + " | --- | --- |"]
        for arm, v in d["arms"].items():
            cells = []
            for s in DETECT_SCEN:
                e = v["by_scenario"].get(s)
                cells.append("-" if not e else f"{fmt(e['median_iqr'])} ({e['missed']}/{e['n_incidents']})")
            prm = v["spec"].get("param")
            L.append(f"| {arm} | {'-' if prm is None else f'{prm:.4g}'} | " + " | ".join(cells) +
                     f" | {v['fa_per_hour_all']:.2f} | {v['fa_per_hour_S0_S1']:.2f} |")
        L += ["", "Paired delay difference, other arm minus D3, over incidents both detect (positive favours D3):", "",
              "| vs | pairs | median diff s [95% CI] | Wilcoxon p | only D3 detects | only other detects |",
              "| --- | --- | --- | --- | --- | --- |"]
        for arm, p in d["pairs"].items():
            md = "-" if p["median_diff"] is None else f"{p['median_diff']:.1f} [{p['lo']:.1f}, {p['hi']:.1f}]"
            wp = "-" if p["wilcoxon_p"] is None else f"{p['wilcoxon_p']:.3g}"
            L.append(f"| {arm} | {p['n']} | {md} | {wp} | {p['d3_only_detected']} | {p['other_only_detected']} |")
        L.append("")
    for fname, title in (("arena.json", "P3, P4: policy arena (set C, paired by seed)"),):
        if not (RES / fname).exists():
            continue
        runs = _load(fname)["runs"]
        L += [f"## {title}", ""]
        for metric, lb in (("peak_queue", True), ("excess_retries", True), ("recovery_s", True),
                           ("unique_success", False), ("duplicate_commits", True)):
            summ = summarize_arena(runs, metric)
            L += [f"**{metric}** median [IQR] (runs not recovered/NaN in brackets)", "",
                  "| Scenario | U0 | U1 | U2 | U3 |", "| --- | --- | --- | --- | --- |"]
            for sc in sorted(summ):
                L.append(f"| {sc} | " + " | ".join(
                    (f"{fmt(summ[sc][u][:3])} ({summ[sc][u][3]})" if u in summ[sc] else "-") for u in ("U0", "U1", "U2", "U3")) + " |")
            L.append("")
        L += ["Paired differences on S4 (first minus second; negative favours the first for lower-is-better metrics):", "",
              "| Comparison | metric | pairs | median diff [95% CI] | Wilcoxon p |", "| --- | --- | --- | --- | --- |"]
        for x, y in (("U3", "U0"), ("U3", "U1"), ("U3", "U2")):
            for metric in ("recovery_s", "excess_retries", "peak_queue"):
                p = arena_pairs(runs, "S4", metric, x, y)
                md = "-" if p["median_diff"] is None else f"{p['median_diff']:.1f} [{p['lo']:.1f}, {p['hi']:.1f}]"
                L.append(f"| {x} vs {y} | {metric} | {p['n']} | {md} | {'-' if p['wilcoxon_p'] is None else f'{p['wilcoxon_p']:.3g}'} |")
        sh = [(r["universe"], r["shift_applied"], r["config_id"], r["peak_queue"], r["recovery_s"]) for r in runs
              if r["scenario"] == "S3" and not (r["universe"] == "U3" and r["operator_delay_ms"] != 20_000)]
        by: dict = defaultdict(dict)
        for u, s, c, q, rc in sh:
            by[c][u] = (s, q, rc)
        harm = {"U1": 0, "U3": 0}
        applied = {"U1": 0, "U3": 0}
        for c, v in by.items():
            for u in ("U1", "U3"):
                if u in v and "U0" in v and v[u][0]:
                    applied[u] += 1
                    worse = v[u][1] > v["U0"][1] or (v[u][2] is not None and v["U0"][2] is not None and v[u][2] > v["U0"][2])
                    harm[u] += bool(worse)
        L += ["", "P4, S3 harmful failovers (failover applied and peak queue or recovery worse than U0 on the same seed): "
              f"U1 {harm['U1']}/{applied['U1']} runs with a failover, U3 {harm['U3']}/{applied['U3']}.", ""]
        sweep = defaultdict(list)
        for r in runs:
            if r["universe"] == "U3" and r["scenario"] == "S4":
                sweep[r["operator_delay_ms"]].append(r["recovery_s"] if r["recovery_s"] is not None else float("nan"))
        if len(sweep) > 1:
            L += ["Operator-delay sweep on S4, U3 recovery_s (exploratory):", "", "| d_op s | median [IQR] | not recovered |", "| --- | --- | --- |"]
            for dly in sorted(sweep):
                vs = sweep[dly]
                L.append(f"| {dly / 1000:.0f} | {fmt(iqr([x for x in vs if x == x]))} | {sum(1 for x in vs if x != x)} |")
            L.append("")
    if (RES / "s6.json").exists():
        rows = _load("s6.json")["runs"]
        L += ["## P5: S6 verification storm", "", "| Arm | runs | peak status checks/s | peak queue | duplicates | escalated | resolve p50 s |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
        for arm in ("no_verification", "naive_verify", "governed"):
            rs = [r for r in rows if r["arm"] == arm]
            if rs:
                med = lambda k: np.nanmedian([np.nan if r[k] is None else r[k] for r in rs])
                L.append(f"| {arm} | {len(rs)} | {med('peak_status_check_rate'):.1f} | {med('peak_queue_depth'):.0f} | "
                         f"{med('duplicate_commits'):.0f} | {med('escalated') if arm == 'governed' else float('nan'):.0f} | {med('resolve_p50_s'):.0f} |")
        L.append("")
    if (RES / "forecast_false_critical.json").exists():
        f = _load("forecast_false_critical.json")
        L += ["## P6: forecaster false criticals (rising edges per gateway per simulated hour)", "",
              "| Scenario | runs | sim hours | per hour at 0.5 | per hour at tuned threshold |", "| --- | --- | --- | --- | --- |"]
        for s, v in f.items():
            L.append(f"| {s} | {v['runs']} | {v['sim_hours']:.2f} | {v['per_hour_at_0.5']:.1f} | {v['per_hour_at_tuned']:.1f} |")
        L += ["", "Lead time, AUC and Brier on the forecaster test set are in artifacts/results/forecaster_eval.json "
              "(see docs/DEVIATIONS.md, WP-08).", ""]
    (RES / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    cfg_hash = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "configs").glob("*"))
                if p.is_file()}
    _save("MANIFEST.json", {"git_commit": commit, "config_sha256": cfg_hash,
                            "stages": {p.name: p.stat().st_mtime for p in RES.glob("*.json")},
                            "wall_s": {n: _load(n).get("wall_s") for n in ("detection.json", "arena.json") if (RES / n).exists()}})
    print("wrote", RES / "summary.md")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["detect", "arena", "s6", "forecast", "report"])
    ap.add_argument("--set", default="C")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--scenarios", default="S2,S3,S4,S5,S6")
    ap.add_argument("--universes", default="U0,U1,U2,U3")
    ap.add_argument("--delays", default="20000")
    ap.add_argument("--out", default="arena.json")
    a = ap.parse_args()
    {"detect": stage_detect, "arena": stage_arena, "s6": stage_s6, "forecast": stage_forecast,
     "report": stage_report}[a.stage](a)


if __name__ == "__main__":
    main()
