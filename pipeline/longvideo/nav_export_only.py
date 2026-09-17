#!/usr/bin/env python3
"""Export one map's navmesh to frozen/nav/<slug>.{bin,json} and stop. Nothing else.

    MAP=/Game/... [SLUG=Game_...] PORT=9208 python3 nav_export_only.py

The core-size survey (survey_core.py) reads those two files and nothing from the engine, so a map
can be ranked without ever being planned or recorded. This is the part that needs a live editor,
pulled out of freeze() so it can run on a fleet against every map that has a start-positions file.
Same three calls freeze() makes, in the same order: connect, pick_spawn (which builds the navmesh
around a projectable start), nav_export.
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
import engine                      # noqa: E402
import freeze_coverage as fzc      # noqa: E402

MAP = os.environ["MAP"]
SLUG = os.environ.get("SLUG") or fzc.slug_for(MAP)
NAV = PIPE / "frozen" / "nav"
NAV.mkdir(parents=True, exist_ok=True)
t0 = time.time()
out = {"slug": SLUG, "map_id": MAP}
try:
    ucv = engine.connect(1280, 720)
    if ucv is None:
        raise RuntimeError("UE never became reachable")
    req = ucv.client.request
    try:
        sp, gz, nav_boot = fzc.pick_spawn(req, MAP)
        out["spawn"] = {"name": sp.get("name"), "x": sp["x"], "y": sp["y"], "ground_z": gz}
        out["spawn_projected"] = True
    except RuntimeError as e:
        if "did not project onto the navmesh" not in str(e):
            raise
        # No start point stands on the navmesh. freeze refuses here, rightly - a route needs a
        # spawn. A SURVEY does not: the navmesh the engine built around the first start point is
        # still the map's walkable surface and its core can still be measured. Export it and say
        # so - `spawn_projected: false` travels into the survey row, so a small core on such a map
        # reads as "measured around a point that is not on it", not as a fact about the map.
        # First fleet pass: CastleRiver, NorthenIsle, InfinityWeather Rain/World, GrassHills
        # Overview, ModularSciFi indoor all refused this way.
        cand = [p for p in json.load(open(fzc.SP_DIR / f"{SLUG}.json"))["positions"]
                if all(isinstance(p.get(k), (int, float)) for k in ("x", "y", "z"))][0]
        cx, cy, cz = float(cand["x"]), float(cand["y"]), float(cand["z"])
        traced = engine.ground_z(req, [(cx, cy)], cz + 400.0)[0]
        nav_boot = engine.nav_ensure(req, (cx, cy), traced if traced is not None else cz)
        if not nav_boot.get("ok"):
            raise RuntimeError(f"nav_ensure failed after unprojectable spawn: {nav_boot.get('error')}")
        out["spawn"] = {"name": cand.get("name"), "x": cx, "y": cy, "ground_z": traced}
        out["spawn_projected"] = False
        out["spawn_note"] = str(e)[:300]
    out["nav_ensure"] = {k: nav_boot.get(k) for k in ("ok", "error", "tiles", "took_s") if k in nav_boot}
    nbin, njson = NAV / f"{SLUG}.bin", NAV / f"{SLUG}.json"
    exp = engine.nav_export(req, str(nbin), str(njson))
    if not exp.get("ok"):
        raise RuntimeError(f"export failed: {exp.get('error')}")
    out.update(ok=True, vertex_count=exp.get("vertex_count"), triangle_count=exp.get("triangle_count"),
               bounds_min=exp.get("bounds_min"), bounds_max=exp.get("bounds_max"),
               bin_bytes=nbin.stat().st_size, took_s=round(time.time() - t0, 1))
except Exception as e:
    out.update(ok=False, error=f"{type(e).__name__}: {str(e)[:300]}", took_s=round(time.time() - t0, 1))
print("NAVEXPORT " + json.dumps(out), flush=True)
sys.exit(0 if out.get("ok") else 1)
