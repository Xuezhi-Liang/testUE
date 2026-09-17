#!/usr/bin/env python3
"""Motion shimmer metric: on MOVING frames, warp frame t+1 back onto frame t with dense optical
flow and measure what is left. Real motion is compensated; aliasing crawl, temporal noise and
exposure flicker are not. Also reports the static-camera metric for the same recording.
    python3 motion_shimmer.py <episode_dir> [<episode_dir> ...]
"""
import csv, sys, os
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2, numpy as np
from pathlib import Path

def metric(ep, n_moving=90, n_static=90):
    rows = list(csv.DictReader(open(Path(ep) / "frames.csv")))
    step = np.array([float(r["step_cm"]) for r in rows]); yawr = np.array([abs(float(r["yaw_rate_deg_s"])) for r in rows])
    moving = [i for i in range(len(rows) - 1) if step[i] > 0.5 and step[i + 1] > 0.5]
    static = [i for i in range(len(rows) - 1) if step[i] < 0.01 and yawr[i] < 0.01 and step[i + 1] < 0.01]
    moving = moving[::max(1, len(moving) // n_moving)][:n_moving]; static = static[:n_static]
    res = []
    for i in moving:
        a = cv2.imread(str(Path(ep) / "rgb" / f"{i:06d}.jpg")); b = cv2.imread(str(Path(ep) / "rgb" / f"{i+1:06d}.jpg"))
        ga, gb = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        flow = cv2.calcOpticalFlowFarneback(ga, gb, None, 0.5, 4, 21, 3, 7, 1.5, 0)
        h, w = ga.shape; gx, gy = np.meshgrid(np.arange(w), np.arange(h))
        mapx = (gx + flow[..., 0]).astype(np.float32); mapy = (gy + flow[..., 1]).astype(np.float32)
        bw = cv2.remap(b, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        d = np.abs(a.astype(np.float32) - bw.astype(np.float32)).mean(axis=2)
        # ignore a border where the warp is undefined
        d = d[20:-20, 20:-20]; res.append((float(np.median(d)), float(d.mean()), float((d > 20).mean())))
    st = []
    for i in static:
        a = cv2.imread(str(Path(ep) / "rgb" / f"{i:06d}.jpg")).astype(np.float32); b = cv2.imread(str(Path(ep) / "rgb" / f"{i+1:06d}.jpg")).astype(np.float32)
        st.append(float(np.abs(a - b).mean()))
    r = np.array(res)
    return {"moving_pairs": len(res), "motion_residual_median": float(np.median(r[:, 0])), "motion_residual_mean": float(np.median(r[:, 1])),
            "motion_sparkle_pct": float(np.median(r[:, 2])) * 100, "static_diff": float(np.median(st)) if st else float("nan")}

if __name__ == "__main__":
    for ep in sys.argv[1:]:
        m = metric(ep); print(Path(ep).parent.parent.name if Path(ep).parent.name in ("asis", "newdefault") else Path(ep).parent.name, {k: round(v, 3) for k, v in m.items()})
