#!/usr/bin/env python3
"""Is the navmesh height the ground? Compare `z_at` against a downward trace at real poses.

    MAP=/Game/... FROZEN=frozen/<episode>.json python3 probe_ground.py

Why this exists. Head-clearance pruning on WinterTown reported its longest blocked roads as
blocked by `Landscape_0` at 0.0-31.3 cm - the terrain, at the start of the sweep, meaning the body
capsule begins inside the ground. The capsule bottom is placed at `z_at(xy) + 6 cm` and `z_at` is
the NAVMESH height, which Recast rasterises at its own cell height and which on rolling terrain
can sit below the real collision surface. If that is what is happening then the pruning is
throwing away roads for the wrong reason - and, far worse, the camera is under the ground, because
the same z places it.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
import engine  # noqa: E402

MAP = os.environ["MAP"]
FROZEN = PIPE / os.environ["FROZEN"]
N = int(os.environ.get("N", "60"))

fz = json.loads(FROZEN.read_text())
poses = fz["poses"]
eye = float(fz["eye_height_cm"])
idx = np.linspace(0, len(poses) - 1, N).astype(int)
pts = [(float(poses[i]["x_cm"]), float(poses[i]["y_cm"])) for i in idx]
surf = [float(poses[i]["z_cm"]) - eye for i in idx]   # the walkable-surface z the plan used

ucv = engine.connect(1280, 720)
if ucv is None:
    raise SystemExit("UE never became reachable")
req = ucv.client.request

# Trace from well above each point. Started 500 cm over the plan's own surface so a trace cannot
# begin underground and miss the surface entirely.
traced = engine.ground_z(req, pts, max(surf) + 500.0)
d = []
for k, i in enumerate(idx):
    t = traced[k]
    if t is None:
        print(f"  frame {i:7d}: trace hit NOTHING (plan surface {surf[k]:.1f})")
        continue
    d.append(t - surf[k])
d = np.array(d)
print(f"\n{MAP}")
print(f"  {len(d)} of {N} sampled poses traced")
print(f"  traced ground minus plan surface, cm:")
print(f"     min {d.min():8.1f}   p10 {np.percentile(d,10):8.1f}   median {np.median(d):8.1f}"
      f"   p90 {np.percentile(d,90):8.1f}   max {d.max():8.1f}")
above = int((d > 6.0).sum())
print(f"  poses where the real ground is MORE than the capsule's 6 cm clearance above the "
      f"plan's surface: {above} of {len(d)} ({above/max(len(d),1)*100:.0f}%)")
print(f"  -> those are poses whose body capsule starts inside the terrain, and whose camera "
      f"sits {np.median(d[d>6.0]) if above else 0:.1f} cm underground at the median")
