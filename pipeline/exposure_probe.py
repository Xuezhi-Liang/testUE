#!/usr/bin/env python3
"""Choose a map's exposure bias by measuring it, before any episode is recorded.

    MAP=/Game/... FROZEN=<frozen.json> OUT=<dir> [EVS=-2,-1,0,1,2,3] [N=24] PORT=9208 python3 exposure_probe.py

Renders N poses spread along a frozen route, each once under the level's own auto-exposure (a
cold view state - what every episode so far was recorded with) and once per candidate bias
under manual exposure, and ranks the biases on the histogram the FIRST pipeline calibrated its
maps with (docs/MAPS.md): no blown highlights, few crushed blacks, a mid-grey median.

    choice: within blown (Y>=250) <= 5% and near-black (Y<=5) <= 20%, the bias losing the fewest
    pixels at both ends, mid-grey median as tie-break - per frame in the engine, medians over N.

The chosen bias goes into the task as render.exposure_bias_ev. The auto-exposure column is not
a candidate - it is the reference that shows what "unpinned" produced at these same poses.
Nothing here decides for a map that fails every candidate: it prints the table and says so.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402

MAP = os.environ.get("MAP")
FROZEN = Path(os.environ.get("FROZEN", ""))
OUT = Path(os.environ.get("OUT") or (HERE / "_exposure_probe" / FROZEN.stem))
EVS = None   # fine grid; None = derive it from the coarse stage
N = int(os.environ.get("N", "24"))
W, H = int(os.environ.get("W", "640")), int(os.environ.get("H", "360"))

# Limits, measured against Downtown West rather than copied from the first pipeline. Its
# 0.5% blown / 3% near-black were fitted with a film curve on interiors; a street with sky and
# arcade shadow in the same frame cannot meet them at ANY exposure - the level's own auto-exposure
# sits at 2.6% blown / 11% near-black on these poses. So: hard limits wide enough that the picture
# exists (blown <= 5%, near-black <= 20%), and the choice is the bias that loses the fewest pixels
# at both ends, with the mid-grey median as tie-breaker. What auto-exposure produced is reported
# beside it as the reference, never as a candidate.
TARGET_P50 = 120.0
MAX_BLOWN = 0.05
MAX_BLACK = 0.20


def pick_poses(fz, n):
    poses = fz["poses"]
    # spread along the route, and prefer frames that are not mid-turn so the pose is legible
    idx = [int(round(i * (len(poses) - 1) / max(n - 1, 1))) for i in range(n)]
    return [(i, poses[i]) for i in idx]


def score(row, target_p50=None):
    """Lower is better; None if a hard limit is broken. Pixels lost at either end, then distance
    from the target mid-grey - by default 120, or the level's own median when the caller knows it
    (lighting_calibrate passes it, so the exposure stays near the author's brightness instead of
    re-lighting a night scene into day)."""
    if row["blown_frac"] > MAX_BLOWN or row["black_frac"] > MAX_BLACK:
        return None
    t = TARGET_P50 if target_p50 is None else target_p50
    return row["blown_frac"] + row["black_frac"] + abs(row["p50"] - t) / 400.0


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


COARSE_EVS = [float(x) for x in range(-2, 17, 2)]


def _render_set(req, fz, out, poses, variants, w, h, tag):
    fov = float(fz["task"]["camera"]["fov_deg"])
    rows = {name: [] for name, _ in variants}
    t0 = time.time()
    for k, (i, p) in poses:
        xyz = (p["x_cm"], p["y_cm"], p["z_cm"])      # frozen z already carries the eye height
        for name, ev in variants:
            path = out / f"{tag}pose{k:02d}_f{i:06d}_{name}.png"
            r = engine.rgb_capture(req, xyz, p["yaw_deg"], p["pitch_deg"], fov, w, h, str(path),
                                   exposure_bias_ev=(ev or 0.0), manual_exposure=(ev is not None))
            if not r.get("ok"):
                print(f"[probe] {tag}pose {k} {name}: FAILED {r.get('error')}", flush=True)
                continue
            r.update(pose_index=k, frame=i, variant=name, ev=ev, file=path.name, stage=tag or "fine")
            rows[name].append(r)
        print(f"[probe] {tag}pose {k + 1}/{len(poses)} done ({time.time() - t0:.0f} s)", flush=True)
    return rows


def sweep(req, fz, out, evs=None, n=N, w=W, h=H, lighting=None, target_p50=None):
    """Two stages, then the table and sweep.json; returns (table, chosen_ev).

    Manual exposure meters from the default camera (ISO 100, 1/60 s, f/4 - EV100 ~ 9.9), and a
    purchased level is lit in whatever units its author liked: the first Downtown sweep at
    -2..+3 EV came back BLACK at every bias, p50 = 0, because the level's sun is a few lux against
    a camera set for a sunlit exterior. So stage one is coarse and wide (-2..+16 EV, step 2, on 6
    poses) to find where the picture is at all, and stage two is +-1.5 EV around that, step 0.5,
    on all N poses. EVS in the environment skips stage one.
    """
    out.mkdir(parents=True, exist_ok=True)
    all_poses = list(enumerate(pick_poses(fz, n)))
    if evs is None:
        coarse_poses = all_poses[::max(1, len(all_poses) // 6)][:6]
        cvars = [(f"ev{ev:+.1f}", ev) for ev in COARSE_EVS]
        crow = _render_set(req, fz, out, coarse_poses, cvars, w, h, "coarse_")
        ctab = []
        for name, ev in cvars:
            rs = crow[name]
            if rs:
                ctab.append({"ev": ev, "p50": median([r["p50"] for r in rs]),
                             "blown": median([r["blown_frac"] for r in rs]),
                             "black": median([r["black_frac"] for r in rs])})
        print("[probe] coarse: " + "  ".join(f"{t['ev']:+.0f}EV p50={t['p50']}" for t in ctab), flush=True)
        # nearest median to the target, limits or not - stage two applies the limits
        best = min(ctab, key=lambda t: abs(t["p50"] - (TARGET_P50 if target_p50 is None else target_p50)))["ev"]
        evs = [round(best + d, 1) for d in (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5)]
        print(f"[probe] coarse best {best:+.0f} EV -> fine grid {evs}", flush=True)
        coarse_frames = [r for rs in crow.values() for r in rs]
    else:
        coarse_frames = []
    variants = [("auto", None)] + [(f"ev{ev:+.1f}", ev) for ev in evs]
    rows = _render_set(req, fz, out, all_poses, variants, w, h, "")

    table = []
    for name, ev in variants:
        rs = rows[name]
        if not rs:
            continue
        agg = {"variant": name, "ev": ev, "n": len(rs),
               "p10": median([r["p10"] for r in rs]), "p50": median([r["p50"] for r in rs]),
               "p99": median([r["p99"] for r in rs]),
               "mean_y": round(median([r["mean_y"] for r in rs]), 1),
               "blown_frac": median([r["blown_frac"] for r in rs]),
               "black_frac": median([r["black_frac"] for r in rs]),
               "blown_frac_max": max(r["blown_frac"] for r in rs),
               "black_frac_max": max(r["black_frac"] for r in rs)}
        agg["score"] = score(agg, target_p50) if ev is not None else None
        table.append(agg)
    cands = [t for t in table if t["ev"] is not None and t["score"] is not None]
    chosen = min(cands, key=lambda t: t["score"]) if cands else None

    print(f"\n{'variant':9s} {'p10':>4} {'p50':>4} {'p99':>4} {'mean':>6} {'blown%':>7} {'black%':>7}  verdict")
    for t in table:
        v = ("REFERENCE (auto)" if t["ev"] is None else
             ("CHOSEN" if chosen and t is chosen else ("ok" if t["score"] is not None else "fails a limit")))
        print(f"{t['variant']:9s} {t['p10']:4d} {t['p50']:4d} {t['p99']:4d} {t['mean_y']:6.1f} "
              f"{t['blown_frac']*100:7.2f} {t['black_frac']*100:7.2f}  {v}")
    if chosen is None:
        print("[probe] NO candidate bias satisfies the limits - widen EVS or look at the frames; "
              "nothing is chosen")
    else:
        print(f"[probe] chosen exposure_bias_ev = {chosen['ev']:+.1f} for {fz['map_id']}")
    (out / "sweep.json").write_text(json.dumps({
        "map_id": fz["map_id"], "width": w, "height": h, "poses": n, "evs": evs,
        "limits": {"target_p50": TARGET_P50, "max_blown": MAX_BLOWN, "max_black": MAX_BLACK, "rule": "min blown+black within limits, p50 tie-break"},
        "lighting": lighting, "table": table, "chosen_ev": chosen["ev"] if chosen else None,
        "frames": [r for rs in rows.values() for r in rs], "coarse_frames": coarse_frames}, indent=1))
    print("wrote", out / "sweep.json")
    return table, (chosen["ev"] if chosen else None)


def main():
    fz = json.loads(FROZEN.read_text())
    if fz["map_id"] != MAP:
        raise SystemExit(f"frozen plan is for {fz['map_id']}, MAP says {MAP}")
    ucv = engine.connect(1280, 720)
    if ucv is None:
        raise SystemExit("UE never became reachable")
    req = ucv.client.request
    import capture_engine as cape
    lighting = cape.ensure_dynamic_sky(req)   # the same session-only skylight fix a recording gets
    sweep(req, fz, OUT, evs=([float(x) for x in os.environ["EVS"].split(",")] if os.environ.get("EVS") else None), lighting=lighting)


if __name__ == "__main__":
    main()
