"""Trace what U3 does on one configuration: trace_u3.py S4 [set]"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.api.live import LiveRun  # noqa: E402
from qs.config import OperatorConfig  # noqa: E402
from qs.eval.arena import SimulatedOperator  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402

KEEP = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
        "client_timeout_ms", "attempt_cap", "flash_mult"}
sc = sys.argv[1]
c = [c for c in make_configs(sys.argv[2] if len(sys.argv) > 2 else "B", 6, 0) if c["scenario"] == sc][0]
p = ScenarioParams.from_dict({k: v for k, v in c.items() if k in KEEP})


def show(r):
    for i in r.incidents:
        print(r.tick_ts // 1000, "INC", i.incident_id, i.segment, i.state.value, i.severity.value)
    for x in r.recs:
        print(r.tick_ts // 1000, "REC", x.rec_id, x.verdict.value, x.severity.value, x.confidence,
              [(a.id.value, a.eligible, a.blocked_by) for a in x.actions])


lr = LiveRun(sc, c["seed"], "U3", params=p, on_tick=show, horizon_ms=p.onset_ms + 90_000,
             operator=SimulatedOperator(OperatorConfig(approval_delay_ms=10_000)))
lr.run()
print("onset", p.onset_ms // 1000, "end", (p.onset_ms + p.duration_ms) // 1000)
print("applied", [(t // 1000, a, rb) for t, a, rb in lr.applied])
print("decisions", [(d.rec_id, d.decided_ts // 1000, d.applied) for d in lr.pipe.actions.log])
