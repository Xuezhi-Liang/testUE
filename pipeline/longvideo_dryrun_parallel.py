#!/usr/bin/env python3
"""longvideo_dryrun.py's per-map body, run over a list of maps in parallel processes.

    python3 longvideo_dryrun_parallel.py <maps.json> <out.json> [--jobs N]

<maps.json> is a list of {"slug", "core_m2", ...}; each row gets the same measurement the serial
dry-run makes (network on the core, 8 coverage walks, pass cost at the fast yaw / medium speed
tier) and the same caveats: no head-clearance pruning, no tuner, so a little optimistic. One map
is 1-5 min of pure CPU with no shared state, which is why processes and not threads. Written for
the "how many hours are the 58 unrecorded maps" question, where 58 x 3 min serial was the wall.
"""
import json
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np

PIPE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
sys.path.insert(0, str(PIPE))
import coverage as C          # noqa: E402
import plan_navmesh as pnm    # noqa: E402

NAV = PIPE / "frozen" / "nav"
TASK = json.loads((PIPE / "tasks" / "longvideo_template.json").read_text())


def measure(m):
    task = TASK
    fps = float(task["fps"]); want = int(task["target_passes"])
    cap = int(round(fps * float(task["max_duration_s"])))
    eye = float(task["camera"]["eye_height_m"]) * 100.0
    pl = float(task["camera"]["pitch_limit_deg"]); plu = float(task["camera"].get("pitch_limit_up_deg", pl))
    styles = tuple(task["styles"]); fills = tuple(task["fill_styles"])
    head = float(task.get("size_headroom", 1.04))
    slug = m["slug"]; t0 = time.time()
    nb, nj = NAV / f"{slug}.bin", NAV / f"{slug}.json"
    if not nb.exists():
        return {**m, "error": "no cached navmesh export"}
    try:
        nav, meta = pnm.load(str(nb), str(nj))
        G, rep, region = C.build_network(nav, core_only=True)
        z_at = C.make_z_at(nav, region)
        rng = np.random.default_rng(int(m.get("seed", 2000)))
        lo, hi = {"slow": (15.0, 30.0), "medium": (30.0, 60.0), "fast": (60.0, 90.0)}[task["yaw_tier"]]
        yaw = float(rng.uniform(lo, hi))
        if task.get("yaw_deg_per_s") is not None:
            yaw = float(task["yaw_deg_per_s"])
        prof = task.get("turn_profile", "cosine")
        knobs = {k: 1.0 for k in C.KNOBS}; knobs["_tier"] = (0.6, 1.0)
        walks = C.prepare_walks(G, int(rng.integers(1 << 30)), count=max(8, want))
        per = C.measure_passes(G, rep, walks, styles, knobs, fps, eye, pl, yaw, z_at, fills, pitch_limit_up=plu, turn_profile=prof)
        bare = sum(per[:want]) if len(per) >= want else None
        need = int(bare * head) if bare is not None else None
        bound = "max_duration_s" if (need is None or need > cap) else f"target_passes={want}"
        frames = cap if bound == "max_duration_s" else bare
        return {"slug": slug, "core_m2": m.get("core_m2"), "yaw_deg_per_s": yaw, "turn_profile": prof, "roads": rep["roads"],
                "core_centreline_m": round(rep["centreline_m"], 1),
                "one_pass_frames": per[0], "one_pass_min": round(per[0] / fps / 60, 1),
                "pass_frames": per, "passes_measured": len(per),
                "frames_for_target_passes": bare,
                "hours_for_target_passes": (round(bare / fps / 3600, 2) if bare else None),
                "bound_by": bound, "episode_frames": frames,
                "episode_hours": round(frames / fps / 3600, 2), "took_s": round(time.time() - t0, 1)}
    except Exception as e:
        return {**m, "error": f"{type(e).__name__}: {e}", "took_s": round(time.time() - t0, 1)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    jobs = next((int(a.split("=", 1)[1]) for a in sys.argv[1:] if a.startswith("--jobs=")), 12)
    maps = json.loads(Path(args[0]).read_text()); out = Path(args[1])
    # biggest cores first so the slow ones do not trail the pool
    maps.sort(key=lambda m: -(m.get("core_m2") or 0))
    rows = []
    with Pool(jobs) as pool:
        for r in pool.imap_unordered(measure, maps):
            rows.append(r); print(json.dumps({k: v for k, v in r.items() if k != "pass_frames"}), flush=True)
            out.write_text(json.dumps(rows, indent=1) + "\n")
    print("wrote", out, len(rows), "rows,", sum("error" in r for r in rows), "errors")


if __name__ == "__main__":
    main()
