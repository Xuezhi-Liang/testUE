#!/usr/bin/env python3
"""Freeze one map's long-video route and report whether it could be recorded. No capture.

    MAP=/Game/... SLUG=... SEED=... [SEED2=...] python3 preflight_one.py

Everything that decides go/no-go happens in freeze: the spawn has to project, the core has to
yield a road network, head-clearance pruning has to leave one, the route has to be collision free
and the depth probe has to be clear. Capture is the expensive part and it decides nothing.

The verdict is read from the frozen file's own `pre_gates` rather than re-derived here. Deriving
it a second time is how a pre-flight ends up disagreeing with the run it is meant to predict -
and the first version of this file did exactly that, then crashed on a key it had guessed.

A NO_GO that is a route problem rather than a map problem gets a second seed on the same editor.
`run_coverage.py`'s own refusal says "a different seed, or a map whose corridors are wider", and
one frame of 501 at 56.7 cm against a 60 cm threshold is the former. Thresholds are not moved to
make a map pass; the route is redrawn.
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
import engine  # noqa: E402
import freeze_coverage as fc  # noqa: E402

MAP = os.environ["MAP"]
SLUG = os.environ.get("SLUG") or "Game_" + MAP.lstrip("/").removeprefix("Game/").replace("/", "_")
SEED = int(os.environ.get("SEED", "2000"))
SEED2 = int(os.environ.get("SEED2", str(SEED + 500)))
CAP_S = float(os.environ.get("PF_CAP_S", "7200"))
# A NO_GO on one of these is a property of the route drawn from this seed. A NO_GO on anything
# else is a property of the map, and a second seed will not change it.
ROUTE_GATES = {"collision_free", "depth_probe_clear"}


def attempt(task, ucv, seed):
    task = dict(task)
    task["seed"] = seed
    task["task_id"] = f"pf_{SLUG}_s{seed}"
    _, fz = fc.freeze(task, ucv=ucv)
    net, pr = fz["road_network"], fz["head_clearance_pruning"]
    col, dp, cov = fz["collision"], fz["depth_probe"], fz["coverage"]
    gates = fz["pre_gates"]
    failed = sorted(k for k, v in gates.items() if not v)
    return {
        "seed": seed,
        "verdict": "GO" if not failed else "NO_GO",
        "failed_gates": failed,
        "roads_before_pruning": net.get("roads"),
        "centreline_before_pruning_m": round(net.get("centreline_m", 0), 1),
        "core_m2": net.get("core_m2"),
        "roads_kept": pr.get("roads_kept_connected"),
        "centreline_kept_m": round(pr.get("centreline_kept_m", 0), 1),
        "pruning_kept_fraction": pr.get("kept_fraction"),
        "frames": fz["frames"],
        "minutes": round(fz["frames"] / float(fz["fps"]) / 60, 1),
        "length_bound_by": (fz.get("length") or {}).get("bound_by"),
        "collisions": col["collision_count"],
        "penetrations": col["penetration_count"],
        "min_clearance_cm": col.get("minimum_clearance_cm"),
        "max_step_cm": col["maximum_frame_translation_cm"],
        "max_yaw_deg_s": col["maximum_frame_yaw_rate_deg_s"],
        "depth_nearest_cm": dp.get("minimum_depth_cm"),
        "depth_too_close": dp.get("too_close_count"),
        "depth_probed": dp.get("probed_frames"),
        "depth_threshold_cm": dp.get("threshold_cm"),
        "coverage": f"{cov['roads_walked']}/{cov['roads_total']}",
        "mix_in_band": fz["action_mix_in_band"],
    }


def main():
    task = json.loads((PIPE / "tasks" / "longvideo_template.json").read_text())
    task.update(map_id=MAP, target_passes=1, max_duration_s=CAP_S,
                allow_short_episode=True, min_frames=24 * 300)
    W, H = task["camera"]["resolution"]
    out = {"slug": SLUG, "map_id": MAP, "attempts": []}
    t0 = time.time()
    ucv = engine.connect(W, H)
    if ucv is None:
        out.update(verdict="NO_EDITOR", error="UE never became reachable",
                   took_s=round(time.time() - t0, 1))
        print("PREFLIGHT " + json.dumps(out), flush=True)
        return 2
    for seed in (SEED, SEED2):
        try:
            a = attempt(task, ucv, seed)
        except Exception as e:
            traceback.print_exc()
            a = {"seed": seed, "verdict": "FREEZE_FAILED",
                 "error": f"{type(e).__name__}: {str(e)[:300]}"}
        out["attempts"].append(a)
        print(f"[pf:{SLUG}] seed {seed}: {a['verdict']}"
              + (f" {a.get('failed_gates') or a.get('error')}"
                 if a["verdict"] != "GO" else ""), flush=True)
        if a["verdict"] == "GO":
            break
        if a["verdict"] == "FREEZE_FAILED":
            break                      # not a route problem; a second seed will not help
        if not (set(a["failed_gates"]) <= ROUTE_GATES):
            break                      # a map problem, not a route problem
    best = next((a for a in out["attempts"] if a["verdict"] == "GO"), out["attempts"][-1])
    out.update(verdict=best["verdict"], chosen_seed=best.get("seed"),
               summary=best, took_s=round(time.time() - t0, 1))
    print("PREFLIGHT " + json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
