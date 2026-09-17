#!/usr/bin/env python3
"""Offline: what will a long-video episode actually be, per map, before any GPU time.

Answers the one question the task template deliberately does not decide - whether the episode
ends because the core was walked `target_passes` times or because `max_duration_s` ran out - from
the cached navmesh exports alone. No editor, no capture.

Two things this CANNOT see, and both make it optimistic:
  - head-clearance pruning, which needs engine capsule sweeps. On IndustrialArea it removed 28 of
    1742 roads and made 96 more unreachable, so a real run's network is a little smaller.
  - the action tuner, which moves the knobs off 1.0 and changes what a pass costs.
So treat these as the order of magnitude, not the plan. freeze prints the real figures.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coverage as C          # noqa: E402
import plan_navmesh as pnm    # noqa: E402

NAV = HERE / "frozen" / "nav"


def main():
    task = json.loads((HERE / "tasks" / "longvideo_template.json").read_text())
    maps = json.loads((HERE / "longvideo_maps.json").read_text())
    fps = float(task["fps"])
    want = int(task["target_passes"])
    cap = int(round(fps * float(task["max_duration_s"])))
    eye = float(task["camera"]["eye_height_m"]) * 100.0
    pl = float(task["camera"]["pitch_limit_deg"])
    plu = float(task["camera"].get("pitch_limit_up_deg", pl))
    styles = tuple(task["styles"])
    fills = tuple(task["fill_styles"])
    head = float(task.get("size_headroom", 1.04))
    rows = []
    for m in maps:
        slug = m["slug"]
        nb, nj = NAV / f"{slug}.bin", NAV / f"{slug}.json"
        if not nb.exists():
            rows.append({**m, "error": "no cached navmesh export"})
            print(json.dumps(rows[-1]), flush=True)
            continue
        t0 = time.time()
        try:
            nav, meta = pnm.load(str(nb), str(nj))
            G, rep, region = C.build_network(nav, core_only=True)
            z_at = C.make_z_at(nav, region)
            rng = np.random.default_rng(int(m["seed"]))
            yaw = float(rng.uniform(60.0, 90.0))          # the "fast" yaw tier
            knobs = {k: 1.0 for k in C.KNOBS}
            knobs["_tier"] = (0.6, 1.0)                   # the "medium" speed tier
            walks = C.prepare_walks(G, int(rng.integers(1 << 30)), count=max(8, want))
            per = C.measure_passes(G, rep, walks, styles, knobs, fps, eye, pl, yaw, z_at,
                                   fills, pitch_limit_up=plu)
            # Bare = what the passes actually cost. Sized = bare x headroom, which is the
            # budget the tuner is given. When the pass count binds, the episode is TRIMMED at the
            # Nth completed pass, so the bare figure is the episode - reporting the sized budget
            # there would overstate it by the headroom.
            bare = sum(per[:want]) if len(per) >= want else None
            need = int(bare * head) if bare is not None else None
            bound = ("max_duration_s" if (need is None or need > cap)
                     else f"target_passes={want}")
            frames = cap if bound == "max_duration_s" else bare
            rows.append({
                "slug": slug, "core_rank": m["core_rank"], "core_m2": m["core_m2"],
                "roads": rep["roads"], "core_centreline_m": round(rep["centreline_m"], 1),
                "one_pass_frames": per[0], "one_pass_min": round(per[0] / fps / 60, 1),
                "passes_measured": len(per),
                "frames_for_target_passes": bare,
                "hours_for_target_passes": (round(bare / fps / 3600, 2) if bare else None),
                "sized_budget_frames": need,
                "sized_budget_note": f"bare passes x {head} headroom; the tuner's room, trimmed "
                                     f"back to the Nth completed pass when the passes bind",
                "bound_by": bound,
                "episode_frames": frames,
                "episode_hours": round(frames / fps / 3600, 2),
                "took_s": round(time.time() - t0, 1),
            })
        except Exception as e:
            rows.append({**m, "error": f"{type(e).__name__}: {e}", "took_s": round(time.time()-t0, 1)})
        print(json.dumps(rows[-1]), flush=True)
    (HERE / "_longvideo_dryrun.json").write_text(json.dumps(rows, indent=1) + "\n")
    print("wrote", HERE / "_longvideo_dryrun.json")


if __name__ == "__main__":
    main()
