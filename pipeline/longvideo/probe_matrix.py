#!/usr/bin/env python3
"""Two levers, three settings, one editor session: what does it take to reach GO?

    MAP=/Game/... SLUG=... COMBOS="60:90,60:120,60:150" python3 probe_matrix.py

`GROUND_CLEARANCE_CM` is how far the body capsule's bottom sits above the walkable surface. It
governs the sweep between road samples 40 cm apart, and on rolling terrain 6 cm cannot span the
bulges: WinterTown at 6 cm keeps 20% of its core network and collides 989 times; at 40 cm it keeps
96% and collides 73. It does not move the camera - `coverage.GROUND_CLEARANCE_CM` is a separate
constant, 0.0, and the camera sits at surface + eye height either way.

`coverage.MIN_CLEAR_CM` is how far the road centrelines stay from the walkable boundary. It is the
only lever on the depth probe, which measures how close the CAMERA comes to geometry and is
therefore untouched by the capsule's clearance. Note that `body.leg_margin_cm` in the task does
NOT reach this path - only freeze.py reads it - so raising it changes nothing here.

Neither threshold is moved: `PROBE_MIN_CM` and the collision gate stay where they are, and what
this measures is the cost in road network of satisfying them.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
import coverage as C  # noqa: E402
import engine  # noqa: E402
import freeze_coverage as fc  # noqa: E402

MAP = os.environ["MAP"]
SLUG = os.environ.get("SLUG") or "Game_" + MAP.lstrip("/").removeprefix("Game/").replace("/", "_")
COMBOS = [tuple(float(x) for x in c.split(":"))
          for c in os.environ.get("COMBOS", "60:90,60:120,60:150").split(",")]

task = json.loads((PIPE / "tasks" / "longvideo_template.json").read_text())
task.update(map_id=MAP, seed=int(os.environ.get("SEED", "2001")),
            target_passes=1, max_duration_s=float(os.environ.get("PF_CAP_S", "7200")),
            allow_short_episode=True, min_frames=24 * 300)
W, H = task["camera"]["resolution"]
ucv = engine.connect(W, H)
if ucv is None:
    raise SystemExit("UE never became reachable")

rows = []
for clearance, min_clear in COMBOS:
    # Both through the paths the pipeline actually reads. Reassigning C.MIN_CLEAR_CM does
    # nothing: it is a default argument, bound when build_centrelines was defined, so the first
    # version of this experiment ran 120 cm and got the 90 cm network back.
    # NOT `fc.GROUND_CLEARANCE_CM = clearance`. freeze_coverage reads the clearance into a
    # function-local of the same name from `env or task["body"] or 6.0`, so the module global is
    # never consulted and that assignment was dead. Three combos at 40 and 60 cm returned the
    # identical 39-blocked / 547-unreachable pruning as 6 cm and were reported as three GOs - the
    # same bound-at-definition failure the comment above describes, in a different coat.
    os.environ["GROUND_CLEARANCE_CM"] = str(clearance)
    os.environ["MIN_CLEAR_CM"] = str(min_clear)
    print(f"\n===== clearance {clearance:.0f} cm, corridor clearance {min_clear:.0f} cm =====",
          flush=True)
    t = json.loads(json.dumps(task))          # deep copy: body is nested and gets edited
    t.setdefault("body", {})["ground_clearance_cm"] = clearance
    t["task_id"] = f"mx_{SLUG}_c{clearance:.0f}_m{min_clear:.0f}"
    try:
        _, fz = fc.freeze(t, ucv=ucv)
        pr, col, dp, cov = (fz["head_clearance_pruning"], fz["collision"],
                            fz["depth_probe"], fz["coverage"])
        bad = sorted(k for k, v in fz["pre_gates"].items() if not v)
        rows.append(dict(clearance=clearance, min_clear=min_clear,
                         verdict="GO" if not bad else "NO_GO", failed=bad,
                         kept_m=pr["centreline_kept_m"], kept=pr["kept_fraction"],
                         frames=fz["frames"], minutes=round(fz["frames"] / fz["fps"] / 60, 1),
                         collisions=col["collision_count"],
                         depth_nearest=dp.get("minimum_depth_cm"),
                         depth_too_close=dp.get("too_close_count"),
                         depth_probed=dp.get("probed_frames"),
                         coverage=f"{cov['roads_walked']}/{cov['roads_total']}"))
    except Exception as e:
        rows.append(dict(clearance=clearance, min_clear=min_clear, verdict="FAILED",
                         error=f"{type(e).__name__}: {str(e)[:200]}"))
    print("MATRIX " + json.dumps(rows[-1]), flush=True)

print(f"\n{'clr':>4s} {'corr':>5s} {'verdict':>7s} {'kept_m':>7s} {'kept':>5s} {'min':>6s} "
      f"{'coll':>5s} {'depth':>18s}  failed")
for r in rows:
    if r["verdict"] == "FAILED":
        print(f"{r['clearance']:4.0f} {r['min_clear']:5.0f}  FAILED  {r.get('error','')[:60]}")
        continue
    print(f"{r['clearance']:4.0f} {r['min_clear']:5.0f} {r['verdict']:>7s} {r['kept_m']:7.0f} "
          f"{r['kept']*100:4.0f}% {r['minutes']:6.1f} {r['collisions']:5d} "
          f"{str(r['depth_too_close'])+'/'+str(r['depth_probed'])+' @'+str(r['depth_nearest'])+'cm':>18s}"
          f"  {','.join(r['failed'])}")
