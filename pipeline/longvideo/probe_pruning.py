#!/usr/bin/env python3
"""Why does head-clearance pruning blame the terrain? Compare its own z against a ground trace.

    MAP=/Game/... SLUG=... FROZEN=frozen/<episode>.json python3 probe_pruning.py

`prune_impassable` places each sweep point at `z + GROUND_CLEARANCE_CM + half_h` where z comes
from `nav.inside_xy(..., tol=60)`, falling back to the nearest corridor VERTEX's height when the
point is not inside a triangle. On rolling terrain that fallback can be metres out. WinterTown's
longest blocked roads are all blocked by `Landscape_0` at 0.0-31.3 cm, which is the capsule
starting inside the ground.

An earlier probe sampled the FROZEN ROUTE and found the plan's surface a median 14 cm above the
traced ground - no problem at all. That probe sampled the wrong population: the route only uses
the roads that survived pruning. This one rebuilds the sweep points for every road, blocked ones
included, and splits the comparison by which roads the frozen report says were blocked.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
import coverage as C  # noqa: E402
import engine  # noqa: E402
import plan_navmesh as pnm  # noqa: E402

MAP = os.environ["MAP"]
SLUG = os.environ.get("SLUG") or "Game_" + MAP.lstrip("/").removeprefix("Game/").replace("/", "_")
FROZEN = PIPE / os.environ["FROZEN"]
SAMPLE_CM = 40.0
TRACE_FROM_CM = float(os.environ.get('TRACE_FROM_CM', '50'))
GROUND_CLEARANCE_CM = 6.0

fz = json.loads(FROZEN.read_text())
blocked_keys = {tuple(map(tuple, b["road"][:2])) + (b["road"][2],)
                for b in fz["head_clearance_pruning"]["worst_blocked"]}

nav, meta = pnm.load(str(PIPE / "frozen/nav" / f"{SLUG}.bin"),
                     str(PIPE / "frozen/nav" / f"{SLUG}.json"))
G, rep, region = C.build_network(nav, core_only=True)
print(f"rebuilt: {rep['roads']} roads, {rep['centreline_m']:.0f} m", flush=True)

pts, tag, from_fallback = [], [], []
EVERY = int(os.environ.get("EVERY", "6"))   # every Nth road, to keep the round trips sane
for ei, (u, v, k, d) in enumerate(G.edges(keys=True, data=True)):
    if ei % EVERY:
        continue
    poly = np.asarray(d["poly"], dtype=float)
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    n = max(2, int(round(arc[-1] / SAMPLE_CM)) + 1)
    su = np.linspace(0.0, arc[-1], n)
    xy = np.column_stack([np.interp(su, arc, poly[:, 0]), np.interp(su, arc, poly[:, 1])])
    key = (tuple(u), tuple(v), k)
    for q in xy:
        z = nav.inside_xy(np.array([q[0], q[1], 0.0]), region, tol=60.0)
        fb = z is None
        if fb:
            verts = nav.verts[nav.wtris[region]].reshape(-1, 3)
            z = float(verts[int(np.argmin(np.linalg.norm(verts[:, :2] - q, axis=1))), 2])
        pts.append((float(q[0]), float(q[1]), float(z)))
        tag.append(key in blocked_keys or (key[1], key[0], key[2]) in blocked_keys)
        from_fallback.append(fb)

print(f"{len(pts)} sweep points; {sum(from_fallback)} used the nearest-vertex fallback "
      f"({sum(from_fallback)/len(pts)*100:.1f}%)", flush=True)

ucv = engine.connect(1280, 720)
if ucv is None:
    raise SystemExit("UE never became reachable")
req = ucv.client.request

zs = np.array([p[2] for p in pts])
traced = []
CH = 250
for s in range(0, len(pts), CH):
    part = pts[s:s + CH]
    # Trace from just ABOVE each point's own navmesh height, not from the top of the map. A trace
    # that starts high finds the first thing under it, which on a town is a roof - this probe's
    # own earlier run reported a +892 cm outlier for exactly that reason, and freeze.py has
    # carried the same warning since a start point snapped to a rooftop.
    t = []
    for pz in part:
        t.extend(engine.ground_z(req, [(pz[0], pz[1])], float(pz[2]) + TRACE_FROM_CM))
    traced.extend(t)
    print(f"  traced {min(s + CH, len(pts))}/{len(pts)}", flush=True)

miss = sum(1 for t in traced if t is None)
d = np.array([(t - z) for t, z in zip(traced, zs) if t is not None])
fb = np.array([f for f, t in zip(from_fallback, traced) if t is not None])
bl = np.array([b for b, t in zip(tag, traced) if t is not None])


def show(name, m):
    if m.sum() == 0:
        print(f"  {name:34s} (none)")
        return
    x = d[m]
    print(f"  {name:34s} n={m.sum():5d}  min {x.min():8.1f}  p10 {np.percentile(x,10):7.1f}  "
          f"median {np.median(x):7.1f}  p90 {np.percentile(x,90):7.1f}  max {x.max():8.1f}  "
          f"| ground above the capsule bottom: {int((x > GROUND_CLEARANCE_CM).sum())} "
          f"({(x > GROUND_CLEARANCE_CM).mean()*100:.0f}%)")


print(f"\ntraced ground minus the pruning z, cm ({miss} points hit nothing):")
show("all points", np.ones_like(bl, dtype=bool))
show("on roads the report blocked", bl)
show("on the other roads", ~bl)
show("z from inside_xy", ~fb)
show("z from nearest-vertex fallback", fb)
