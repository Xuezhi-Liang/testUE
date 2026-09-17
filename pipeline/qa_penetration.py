#!/usr/bin/env python3
"""Find every frame where the camera is inside geometry, from the depth we already write.

Why depth and not the capsule sweeps we already run: the sweeps can only see what has collision.
A tree that ships with collision disabled - common for foliage instances, which is exactly what
the tree in RussianWinterTownDemo01 turned out to be - cuts no hole in the navmesh and stops no
sweep, so the planner routes straight through it and every collision check reports clean. The
renderer is the only part of the engine that knows the tree is there, and depth is its answer.

Two thresholds, both measured rather than chosen:
  - NEAR_M 0.60: a standing viewpoint has nothing that close across a large part of the frame.
    On the four sound episodes the median frame has 0% of its pixels inside 0.6 m.
  - FRAC 0.20: frame 1400 of the winter episode, the one visible as walking through a trunk, has
    52% of valid pixels inside 0.6 m and its nearest pixel sitting on the 10 cm near clip plane.

Runs at stride 1 deliberately. A pass-through lasts a fraction of a second - the winter one spans
fewer than 12 frames, so the half-second sampling used elsewhere missed it entirely and reported
the episode clean.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

NEAR_M = 0.60
FRAC = 0.20
CLIP_M = 0.11          # anything at the near plane is geometry crossing it, not a measurement


def _one(arg):
    i, path = arg
    d = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if d is None:
        return None
    # OpenCV decodes EXR as BGRA: our R channel is index 2. Index 0 is B, which we never
    # write, so reading it returns all zeros and every check silently passes.
    z = d[..., 2] if d.ndim == 3 else d
    v = z[z > 0]
    if v.size == 0:
        return None
    return (i, float(v.min()), float((v < NEAR_M).mean()))


def scan(ep: Path, stride=1, workers=None) -> dict:
    """Threaded, because stride 1 is not negotiable and this was 82% of packaging.

    Profiled on a 14,400-frame episode: `acceptance` took 257 s and this scan was 212 s of it,
    almost all inside cv2.imread. Extrapolated to a 150k-frame episode that is 37 minutes, and it
    runs strictly after the last frame is captured.

    Sampling less is not the fix - the docstring above says why stride 1 is required, and it was
    written after a pass-through spanning fewer than 12 frames went unreported. So the reads are
    threaded instead. cv2.imread releases the GIL for the decode, and the frames are independent,
    so this changes throughput and nothing else: same frames read, same numbers out, results
    reassembled in frame order.
    """
    exrs = sorted((ep / "depth").glob("*.exr"))
    if not exrs:
        return {"episode": ep.name, "error": "no depth frames"}
    if workers is None:
        workers = min(16, (os.cpu_count() or 4))
    jobs = [(i, str(exrs[i])) for i in range(0, len(exrs), stride)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        frames = [r for r in pool.map(_one, jobs, chunksize=16) if r is not None]
    if not frames:
        return {"episode": ep.name, "error": "no valid depth"}
    idx = [f[0] for f in frames]
    mn = np.array([f[1] for f in frames])
    fr = np.array([f[2] for f in frames])
    bad = [idx[k] for k in range(len(idx)) if fr[k] > FRAC]
    # contiguous runs, so "40 bad frames" reads as the two events it actually is
    runs = []
    for f in bad:
        if runs and f - runs[-1][1] <= stride * 2:
            runs[-1][1] = f
        else:
            runs.append([f, f])
    return {
        "episode": ep.name,
        "frames": len(frames),
        "min_depth_m": round(float(mn.min()), 4),
        "frames_at_near_clip": int((mn <= CLIP_M).sum()),
        "median_near_fraction": round(float(np.median(fr)), 4),
        "penetrating_frames": len(bad),
        "penetration_events": [{"first": a, "last": b, "frames": (b - a) // stride + 1,
                                "t_s": round(a / 24.0, 2)} for a, b in runs],
    }


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    stride = 1
    for a in sys.argv[1:]:
        if a.startswith("--stride="):
            stride = int(a.split("=")[1])
    for r in [scan(Path(a), stride) for a in args]:
        print(json.dumps(r))
