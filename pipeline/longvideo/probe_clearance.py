#!/usr/bin/env python3
"""How much ground clearance does the body capsule need on this map? Measure, don't argue.

    MAP=/Game/... SLUG=... python3 probe_clearance.py

The sweep is a straight segment between road samples 40 cm apart, and on rolling terrain the
ground bulges between them. With 6 cm of clearance WinterTown reports 272 of 2028 roads blocked by
`Landscape_0` itself, and 1320 more fall out as unreachable - 80% of the core network, for a reason
that has nothing to do with head clearance.

Raising the clearance buys those roads back and costs the sweep its ability to see obstacles below
that height. This prints both sides of that at once, on one editor session and one road network,
so the choice is made against numbers.
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
import plan_navmesh as pnm  # noqa: E402

MAP = os.environ["MAP"]
SLUG = os.environ.get("SLUG") or "Game_" + MAP.lstrip("/").removeprefix("Game/").replace("/", "_")
VALUES = [float(v) for v in os.environ.get("CLEARANCES", "6,15,25,40,60").split(",")]

ucv = engine.connect(1280, 720)
if ucv is None:
    raise SystemExit("UE never became reachable")
req = ucv.client.request

sp, gz, boot = fc.pick_spawn(req, MAP)
print(f"spawn {sp.get('name')} ground {gz:.1f} cm", flush=True)
nbin = PIPE / "frozen/nav" / f"{SLUG}.bin"
njson = PIPE / "frozen/nav" / f"{SLUG}.json"
exp = engine.nav_export(req, str(nbin), str(njson))
if not exp.get("ok"):
    raise SystemExit(f"nav export failed: {exp.get('error')}")
nav, meta = pnm.load(str(nbin), str(njson))
G0, rep0, region = C.build_network(nav, core_only=True)
print(f"core network: {rep0['roads']} roads, {rep0['centreline_m']:.0f} m", flush=True)

_, gr = fc.trace_ground_table(req, nav, region,
                              [q for _, _, _, xy in fc.road_sample_points(G0) for q in xy])
print(f"navmesh vs traced surface: {json.dumps(gr['traced_minus_navmesh_cm'])}", flush=True)

print(f"\n{'clearance':>9s} {'blocked':>8s} {'unreach':>8s} {'kept roads':>10s} "
      f"{'kept m':>8s} {'kept %':>7s}  worst blocker")
for cl in VALUES:
    fc.GROUND_CLEARANCE_CM = cl
    try:
        G, pr = fc.prune_impassable(req, G0, nav, region, 40.0, 88.0)
    except RuntimeError as e:
        print(f"{cl:8.0f}c  RuntimeError: {str(e)[:80]}")
        continue
    wb = pr["worst_blocked"][0] if pr["worst_blocked"] else {}
    print(f"{cl:8.0f}c {pr['roads_blocked']:8d} {pr['dropped_disconnected']:8d} "
          f"{pr['roads_kept_connected']:10d} {pr['centreline_kept_m']:8.0f} "
          f"{pr['kept_fraction']*100:6.0f}%  {wb.get('blocked_by')} @ {wb.get('at_cm')} cm",
          flush=True)
