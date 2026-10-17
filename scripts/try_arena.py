"""Quick look at the arena on one configuration: try_arena.py S4 U0,U1,U2,U3 [set]"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.eval.universes import run_universe  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402

KEEP = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
        "client_timeout_ms", "attempt_cap", "flash_mult"}
sc = sys.argv[1]
c = [c for c in make_configs(sys.argv[3] if len(sys.argv) > 3 else "B", 6, 0) if c["scenario"] == sc][0]
p = ScenarioParams.from_dict({k: v for k, v in c.items() if k in KEEP})
for u in sys.argv[2].split(","):
    t0 = time.time()
    m = run_universe(u, p, c["seed"])
    print(u, round(time.time() - t0), {k: (round(v, 2) if isinstance(v, float) else v)
                                       for k, v in m.items() if k not in ("scenario", "config_id", "seed", "universe")})
