#!/usr/bin/env python3
"""Score recorded clips on motion and scene richness, and say which to keep.

Two independent things can go wrong with a clip, and they need different checks:

  motion   - inter-frame pixel difference. A frozen clip sits near 1.1 (sensor noise);
             a real look-around lands at 15-40.
  richness - Canny edge density. Some start points sit where World Partition has not
             streamed content in: bright, high-contrast, but really just an untextured
             grey ground plane and empty sky. Good streets measure 5-11% edges, bare
             wall-and-sky spots 1.8-2.3%.

Usage:  python3 local_run/score_clips.py [min_edge_pct]
"""
import glob
import json
import os
import sys

import cv2
import numpy as np

ROOT = "/home/ubuntu/WM-Unreal-data-collection/local_run/fpv_data"
MIN_EDGE = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
MIN_MOTION = 3.0


def score(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if n <= 1:
        cap.release()
        return None
    laps, edges, grays = [], [], []
    for t in (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * t))
        ok, fr = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        laps.append(cv2.Laplacian(g, cv2.CV_64F).var())
        edges.append((cv2.Canny(g, 60, 160) > 0).mean() * 100)
        grays.append(cv2.resize(g, (160, 90)).astype(np.int16))
    cap.release()
    if not edges:
        return None
    motion = float(np.mean([np.abs(b - a).mean()
                            for a, b in zip(grays, grays[1:])])) if len(grays) > 1 else 0.0
    return {"frames": n, "laplacian": float(np.mean(laps)),
            "edge_pct": float(np.median(edges)), "sampled_motion": motion}


rows = []
for d in sorted(glob.glob(os.path.join(ROOT, "*", "fpv_*"))):
    s = score(os.path.join(d, "video.mp4"))
    if s is None:
        print(f"{os.path.basename(d):24s} UNREADABLE (still recording?)")
        continue
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:
            pass
    pv = (meta.get("pixel_verify") or {})
    s.update(name=os.path.basename(d), dir=d,
             start=meta.get("start_name", "?"),
             interframe=pv.get("interframe_mean"),
             frozen=pv.get("frozen_fraction"))
    s["keep"] = s["edge_pct"] >= MIN_EDGE and (s["interframe"] or 0) >= MIN_MOTION
    rows.append(s)

print(f"{'clip':24s} {'start':10s} {'frames':>6s} {'interframe':>10s} "
      f"{'frozen':>7s} {'lap':>8s} {'edge%':>6s}  verdict")
for r in rows:
    print(f"{r['name']:24s} {r['start']:10s} {r['frames']:6d} "
          f"{str(r['interframe']):>10s} {str(r['frozen']):>7s} "
          f"{r['laplacian']:8.1f} {r['edge_pct']:6.2f}  "
          f"{'KEEP' if r['keep'] else 'DROP (bare geometry)'}")

keep = [r for r in rows if r["keep"]]
drop = [r for r in rows if not r["keep"]]
print(f"\nkeep {len(keep)} / {len(rows)}")
if drop:
    print("drop dirs:", " ".join(r["dir"] for r in drop))
    print("used starts (exclude on the refill pass):",
          ",".join(sorted({r["start"] for r in rows})))
