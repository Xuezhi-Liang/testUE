#!/usr/bin/env python3
"""The whole exposure test on one map, over ONE UnrealCV connection (a second connection kills
this build - CLAUDE.md):

  1. sweep: N poses x {auto, EV candidates} as PNG + histogram table -> chosen bias
  2. record the frozen route twice with the real capture actor: level auto-exposure (what every
     episode so far got) and manual at the chosen bias
  3. leave both episodes on disk for the analysis + page step (exposure_report.py)

    MAP=/Game/... FROZEN=<frozen.json> OUT=<dir> python3 exposure_test.py
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine                 # noqa: E402
import capture_engine as cape # noqa: E402
import exposure_probe as xp   # noqa: E402

MAP = os.environ["MAP"]
FROZEN = Path(os.environ["FROZEN"])
OUT = Path(os.environ["OUT"])
FORCE_EV = os.environ.get("FORCE_EV")


def main():
    fz = json.loads(FROZEN.read_text())
    assert fz["map_id"] == MAP, (fz["map_id"], MAP)
    OUT.mkdir(parents=True, exist_ok=True)
    ucv = engine.connect(1280, 720)
    if ucv is None:
        raise SystemExit("UE never became reachable")
    req = ucv.client.request
    lighting = cape.ensure_dynamic_sky(req)
    t0 = time.time()
    table, chosen = xp.sweep(req, fz, OUT / "sweep", lighting=lighting)
    ev = float(FORCE_EV) if FORCE_EV is not None else chosen
    if ev is None:
        raise SystemExit("no bias chosen by the sweep and FORCE_EV not given - refusing to record")
    print(f"[test] sweep took {time.time()-t0:.0f} s; recording with manual EV {ev:+.1f} and with auto", flush=True)

    results = {"map_id": MAP, "frozen": str(FROZEN), "chosen_ev": ev, "sweep_table": table, "episodes": {}}
    for tag, env in (("auto", {"EXPOSURE_AUTO": "1"}), (f"manual_ev{ev:+.1f}", {"EXPOSURE_BIAS_EV": str(ev)})):
        for k in ("EXPOSURE_AUTO", "EXPOSURE_BIAS_EV"):
            os.environ.pop(k, None)
        os.environ.update(env)
        root = OUT / tag
        root.mkdir(parents=True, exist_ok=True)
        t1 = time.time()
        ep = cape.capture(str(FROZEN), out_root=root, ucv=ucv)
        results["episodes"][tag] = {"dir": str(ep), "seconds": round(time.time() - t1, 1), "env": env}
        print(f"[test] {tag}: {ep} in {time.time()-t1:.0f} s", flush=True)
    (OUT / "test.json").write_text(json.dumps(results, indent=1))
    print("wrote", OUT / "test.json")


if __name__ == "__main__":
    main()
