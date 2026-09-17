#!/usr/bin/env python3
"""Where does the static-camera shimmer come from? Render the same pose N times under several
console-variable sets and measure the frame-to-frame difference. Anti-aliasing was ruled out by
recording (FXAA / TSR / off all left the shimmer at 1.8-2.0/255), so the suspects are the
temporally-denoised effects: Lumen GI and reflections, ray-traced shadows, virtual shadow maps.

    MAP=... FROZEN=... OUT=... python3 flicker_probe.py
"""
import json, os, sys, time
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cv2, numpy as np
import engine, capture_engine as cape, exposure_probe as xp  # noqa: E402

MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"]); OUT.mkdir(parents=True, exist_ok=True)
SETS = [
    ("baseline", []),
    ("rt_shadows_off", ["r.RayTracing.Shadows 0"]),
    ("lumen_off", ["r.DynamicGlobalIlluminationMethod 0", "r.ReflectionMethod 0"]),
    ("lumen_sw", ["r.Lumen.HardwareRayTracing 0"]),
    ("vsm_off", ["r.Shadow.Virtual.Enable 0"]),
    ("lumen_off_rtshadow_off", ["r.DynamicGlobalIlluminationMethod 0", "r.ReflectionMethod 0", "r.RayTracing.Shadows 0"]),
    ("restore", ["r.DynamicGlobalIlluminationMethod 1", "r.ReflectionMethod 1", "r.RayTracing.Shadows 1", "r.Lumen.HardwareRayTracing 1", "r.Shadow.Virtual.Enable 1"]),
]
N_REP = 6


def main():
    fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP
    fov = float(fz["task"]["camera"]["fov_deg"])
    ucv = engine.connect(1280, 720)
    if ucv is None: raise SystemExit("UE never became reachable")
    req = ucv.client.request
    cape.ensure_dynamic_sky(req)
    poses = [p for _, p in xp.pick_poses(fz, 12)][2:8:2]      # three poses along the route
    results = {}
    for name, cvars in SETS:
        for c in cvars:
            engine.query(req, f'unreal.SystemLibrary.execute_console_command(w, {c!r})\nRESULT.update({{"ok": True}})')
        if name == "restore": break
        diffs, sparkle = [], []
        for k, p in enumerate(poses):
            frames = []
            for r in range(N_REP):
                path = OUT / f"{name}_pose{k}_{r}.png"
                res = engine.rgb_capture(req, (p["x_cm"], p["y_cm"], p["z_cm"]), p["yaw_deg"], p["pitch_deg"], fov, 1280, 720, str(path))
                if res.get("ok"): frames.append(cv2.imread(str(path)).astype(np.float32))
            for a, b in zip(frames, frames[1:]):
                d = np.abs(a - b).mean(axis=2); diffs.append(float(d.mean())); sparkle.append(float((d > 20).mean()))
        results[name] = {"cvars": cvars, "pairs": len(diffs), "diff_median": float(np.median(diffs)), "diff_max": float(max(diffs)), "sparkle_pct": float(np.median(sparkle)) * 100}
        print(f"[flicker] {name:24s} same-pose re-render diff median {results[name]['diff_median']:.2f}  max {results[name]['diff_max']:.2f}  sparkle {results[name]['sparkle_pct']:.2f}%", flush=True)
    (OUT / "flicker_probe.json").write_text(json.dumps(results, indent=1)); print("wrote", OUT / "flicker_probe.json")


if __name__ == "__main__":
    main()
