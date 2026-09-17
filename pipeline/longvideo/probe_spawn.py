#!/usr/bin/env python3
"""Why a start point does not project: the traced ground, and the z extent it would need.

    MAP=/Game/... SLUG=Game_... PORT=9208 python3 probe_spawn.py

A start point can sit far above the walkable surface - MedievalCastle's are up on the castle, and
the core region they belong to has a median z 17 m below them. `nav_project`'s default z extent is
800 cm, so the projection finds nothing and every candidate is refused, which reads as "this map
has no navmesh". This prints the numbers that separate the two.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
import engine  # noqa: E402

SP_DIR = PIPE.parent.parent / "batch_inference" / "start_positions"
MAP = os.environ["MAP"]
SLUG = os.environ.get("SLUG") or "Game_" + MAP.lstrip("/").removeprefix("Game/").replace("/", "_")
Z_EXTENTS = [800.0, 2000.0, 5000.0, 20000.0]

ucv = engine.connect(1280, 720)
if ucv is None:
    raise SystemExit("UE never became reachable")
req = ucv.client.request

pts = [p for p in json.load(open(SP_DIR / f"{SLUG}.json"))["positions"]
       if all(isinstance(p.get(k), (int, float)) for k in ("x", "y", "z"))]
print(f"{SLUG}: {len(pts)} start points; probing the first 4", flush=True)

for cand in pts[:4]:
    cx, cy, cz = float(cand["x"]), float(cand["y"]), float(cand["z"])
    traced = engine.ground_z(req, [(cx, cy)], cz + 400.0)[0]
    z0 = traced if traced is not None else cz
    boot = engine.nav_ensure(req, (cx, cy), z0)
    print(f"\n{cand.get('name')} at ({cx:.0f}, {cy:.0f}, {cz:.0f})")
    print(f"  downward trace from {cz+400:.0f}: "
          + (f"{traced:.0f} cm" if traced is not None else "NOTHING HIT"))
    print(f"  nav_ensure: ok={boot.get('ok')} settled={boot.get('settled')} "
          f"has_navmesh={boot.get('has_navmesh')} wait={boot.get('build_wait_s')}s "
          f"agent r={boot.get('agent_radius_cm')} h={boot.get('agent_height_cm')}")
    for ze in Z_EXTENTS:
        pr = engine.nav_project(req, (cx, cy, z0), extent=(400.0, 400.0, ze))
        if pr.get("ok"):
            pt = pr["point"]
            print(f"  z extent {ze:7.0f} cm -> PROJECTED to z {pt[2]:.0f} cm "
                  f"({(z0 - pt[2])/100:.1f} m below the probe point)")
            break
        print(f"  z extent {ze:7.0f} cm -> nothing")
