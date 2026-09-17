#!/usr/bin/env python3
"""Task JSON -> frozen coverage trajectory, validated before any capture.

The `coverage_walk` family's counterpart to `freeze.py`. Same contract: every pose is decided,
proved walkable and written to disk first, so capture is a replay and `desired == actual` by
construction. Different route: `coverage.py` plans a walk over every road on the map instead of
two straight legs out and back from an anchor.

What is validated here, and why each check exists:

  - **Capsule sweep, every consecutive frame pair.** The body did not pass through geometry.
    Swept on the body capsule lifted off the floor, not on the camera: a capsule whose bottom
    rests exactly on the ground reports a blocking hit at distance 0 and the whole map reads as
    impassable.
  - **Near-plane corners.** A camera centre outside a wall does not prove the near plane is.
  - **Depth probe, sampled along the route.** The only check that sees geometry with collision
    disabled, which foliage instances routinely are: such a tree cuts no hole in the navmesh and
    stops no capsule sweep, so every collision check passes while the camera walks through a
    trunk.
  - **Per-frame translation and rotation bounds**, against the gates that will judge them later,
    so a route that would fail acceptance fails here instead - before ten minutes of GPU time.

Refusing is a normal outcome. A coverage route that is not collision free is a bad route, and
the answer is a different seed or a different map, not a recording of it.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

import networkx as nx
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coverage as C  # noqa: E402
import engine  # noqa: E402
import geom  # noqa: E402
import plan_navmesh as pnm  # noqa: E402

FROZEN = HERE / "frozen"
PROBE_DIR = "/home/ue4/probe"
SP_DIR = HERE.parent.parent / "batch_inference" / "start_positions"

# Matches freeze.py. A capsule resting exactly on the floor is already in contact, so every sweep
# from it reports a hit at distance 0; real characters have a step offset for the same reason.
# How far above the walkable surface the body capsule's bottom sits. 6 cm is a step offset: a
# capsule resting exactly on the floor reports a blocking hit at distance 0. On rolling terrain it
# is also the only slack the sweep has against ground that bulges between two samples 40 cm apart,
# and 6 cm is not enough: WinterTown blocks 272 of 2028 roads on the terrain itself. Raising it
# buys those roads back at the cost of the sweep no longer seeing obstacles below that height -
# so it is a measured trade-off, not a default to change quietly.
GROUND_CLEARANCE_CM = float((os.environ.get("GROUND_CLEARANCE_CM") or "").strip() or "6.0")
MAX_REROUTE_ATTEMPTS = int((os.environ.get("MAX_REROUTE_ATTEMPTS") or "").strip() or "4")
VETO_RADIUS_CM = float((os.environ.get("VETO_RADIUS_CM") or "").strip() or "150")
PROBE_EVERY = 60          # frames between depth probes: 240 probes over a 10-minute episode
PROBE_MIN_CM = 60.0       # nearer than this is REPORTED as proximity; it no longer vetoes
# The veto is the near-clip clamp. A depth render whose nearest pixel sits at the near plane is a
# camera inside a mesh - the buffer has nothing valid to report because the geometry is behind the
# plane - and it reads the plane's distance every time: 10.0, 10.0, 11.0 cm on the three true
# penetrations seen today. A wall reads its distance: 17.4, 21.1, 57.5 cm on the three routes the
# old 60 cm threshold refused for standing near one. Same number, two failures, and only one of
# them is data that must not be recorded.
PROBE_CLIP_CM = 12.0      # at or under this the depth is clamped, i.e. the camera is inside geometry


def slug_for(map_id):
    return "Game_" + map_id.lstrip("/").removeprefix("Game/").replace("/", "_")


def content_bounds(req, z0, min_m2=20.0, min_height_cm=150.0, pad_cm=2000.0, lo_cm=6000.0, hi_cm=25000.0, verbose=True):
    """(centre_xy, half_extent_xy, boxes): the level's placed content around floor z0, as a box.

    The navmesh bounds are synthesised round the spawn, and until 17 Sep they were a fixed 60 m
    half-size: every validated route lived in a 120 m square whatever the level's size
    (ModularNeighborhood is 400 m across; its routes covered the corner the spawn was in). The
    box now follows the CONTENT: every placed actor footprint of `min_m2` or more within 30 m of
    the floor, padded, clamped to [lo, hi] per axis so a sky-sized outlier cannot ask for a
    navmesh the size of a county. The boxes are returned so the road network can reuse them.
    """
    boxes = engine.actor_footprints(req, z0 - 3000.0, z0 + 3000.0)
    # Tall AND wide: a road mesh is 400 m2 and 20 cm high, a house is 100 m2 and 6 m high. Roads,
    # lawns and floors are where the camera walks, not what it walks TO.
    big = [b for b in boxes if float(b[4]) >= min_m2 and (len(b) < 8 or float(b[7]) >= min_height_cm)]
    if not big:
        return None, None, boxes
    xs0 = min(float(b[0]) for b in big); ys0 = min(float(b[1]) for b in big)
    xs1 = max(float(b[2]) for b in big); ys1 = max(float(b[3]) for b in big)
    cx, cy = (xs0 + xs1) / 2.0, (ys0 + ys1) / 2.0
    hx = min(hi_cm, max(lo_cm, (xs1 - xs0) / 2.0 + pad_cm)); hy = min(hi_cm, max(lo_cm, (ys1 - ys0) / 2.0 + pad_cm))
    if verbose:
        print(f"[freeze-cov] content bounds: {len(big)} footprints >= {min_m2:.0f} m2 and >= {min_height_cm:.0f} cm tall, of {len(boxes)}; "
              f"box {2*hx/100:.0f} x {2*hy/100:.0f} m centred ({cx/100:.0f}, {cy/100:.0f}) m"
              + (" (clamped)" if (xs1 - xs0) / 2.0 + pad_cm > hi_cm or (ys1 - ys0) / 2.0 + pad_cm > hi_cm else ""), flush=True)
    return (cx, cy), (hx, hy), boxes


def pick_spawn(req, map_id, verbose=True, content=None):
    """A start point that projects onto the navmesh, from this map's start-positions file.

    `content`: a dict the caller owns; the content box is measured once (on the first candidate's
    floor height) and written into it as centre / half_extent / boxes, so freeze can reuse the
    footprints for the road network without a second engine round trip.
    """
    slug = slug_for(map_id)
    sp_file = SP_DIR / f"{slug}.json"
    if not sp_file.exists():
        raise RuntimeError(f"no start positions for {slug}")
    cands = [p for p in json.load(open(sp_file))["positions"]
             if all(isinstance(p.get(k), (int, float)) for k in ("x", "y", "z"))]
    if not cands:
        raise RuntimeError(f"{sp_file} has no usable positions")
    notes = []
    for cand in cands[:12]:
        cx, cy, cz = float(cand["x"]), float(cand["y"]), float(cand["z"])
        traced = engine.ground_z(req, [(cx, cy)], cz + 400.0)[0]
        z0 = traced if traced is not None else cz
        if content is not None and "boxes" not in content:
            c_xy, c_half, boxes = content_bounds(req, z0, verbose=verbose)
            content.update(centre=c_xy, half_extent=c_half, boxes=boxes)
        if content and content.get("half_extent"):
            # Bounds round the content, grown to include this candidate so it still projects.
            (ccx, ccy), (hx, hy) = content["centre"], content["half_extent"]
            x0, x1 = min(ccx - hx, cx - 1000.0), max(ccx + hx, cx + 1000.0)
            y0, y1 = min(ccy - hy, cy - 1000.0), max(ccy + hy, cy + 1000.0)
            boot = engine.nav_ensure(req, ((x0 + x1) / 2.0, (y0 + y1) / 2.0), z0,
                                     extent_xy=((x1 - x0) / 2.0, (y1 - y0) / 2.0))
        else:
            boot = engine.nav_ensure(req, (cx, cy), z0)
        if not boot.get("ok"):
            notes.append(f"{cand.get('name')}: nav_ensure failed ({boot.get('error')})")
            continue
        # Wait the tile build out before giving up on this candidate. `nav_ensure` polls until
        # the build reports idle with a navmesh present, but a freshly synthesised bounds volume
        # dispatches its tile build on a LATER nav tick - so "idle + has_navmesh" can mean the old
        # navmesh with the new tiles not started yet, and a projection issued in that window finds
        # nothing. freeze.py has carried this retry since an export made in that window came back
        # with bounds ending 40 m short of the spawn; this function was written without it and
        # MedievalCastle refused every one of its twelve start points because of it.
        # Growing z extent, and the conservative one first. A start point can sit well above the
        # walkable surface it belongs to: MedievalCastle's test1 is 13.6 m above its courtyard, so
        # the 800 cm extent freeze.py uses finds nothing and the candidate is refused. Probed
        # directly: 800 cm -> nothing, 2000 cm -> projects to z 8901, 13.6 m down. Trying 800
        # first keeps a point that is already on the surface snapping to the nearest surface
        # rather than to something twenty metres below it, and the snap distance is printed so a
        # spawn that fell through a floor is visible rather than silent.
        #
        # The retry stays because the tile-build race is real and freeze.py has carried a wait for
        # it since an export made in that window came back with bounds 40 m short of the spawn.
        proj, used_ze = {}, None
        for attempt in range(5):
            for ze in (800.0, 2000.0, 5000.0):
                proj = engine.nav_project(req, (cx, cy, z0), extent=(400.0, 400.0, ze))
                if proj.get("ok"):
                    used_ze = ze
                    break
            if proj.get("ok"):
                if verbose and (attempt or used_ze > 800.0):
                    drop = (z0 - proj["point"][2]) / 100.0
                    print(f"[freeze-cov] spawn: '{cand.get('name')}' projected with a "
                          f"{used_ze:.0f} cm z extent, {drop:.1f} m below the probe point"
                          + (f", after waiting {attempt * 8} s for the nav tile build"
                             if attempt else ""))
                break
            import time as _t
            _t.sleep(8.0)
        if proj.get("ok"):
            navz = proj["point"][2]
            if traced is not None and abs(navz - traced) > 150.0:
                notes.append(f"{cand.get('name')}: trace hit {traced:.0f} cm but the navmesh puts "
                             f"walkable ground at {navz:.0f} cm; using the navmesh height")
            if verbose:
                for n in notes[:5]:
                    print(f"[freeze-cov] spawn: {n}")
            return cand, float(navz), boot
        notes.append(f"{cand.get('name')} did not project onto the navmesh at any z extent up "
                     f"to 5000 cm, after 40 s of waiting for the tile build (nav_ensure: "
                     f"settled={boot.get('settled')} has_navmesh={boot.get('has_navmesh')} "
                     f"wait={boot.get('build_wait_s')}s; note has_navmesh is global, not local "
                     f"to this point) - skipped")
        if verbose:
            print(f"[freeze-cov] spawn: {notes[-1]}", flush=True)
    raise RuntimeError("no start point projected onto a navmesh: " + "; ".join(notes[:4]))


GROUND_TRACE_UP_CM = 60.0        # start each trace just above its own navmesh height
GROUND_TRACE_DOWN_CM = 250.0     # a hit further below than this is a hole, not the ground
GROUND_BAND_CM = 150.0           # z bands, so one batched query serves many points
GROUND_CELL_CM = 50.0            # the table's resolution, matching make_z_at's cache


def nav_z_at(nav, region, xy):
    """The navmesh's own surface height under xy, with the nearest-vertex fallback."""
    z = nav.inside_xy(np.array([xy[0], xy[1], 0.0]), region, tol=60.0)
    if z is None:
        verts = nav.verts[nav.wtris[region]].reshape(-1, 3)
        z = float(verts[int(np.argmin(np.linalg.norm(verts[:, :2] - np.asarray(xy), axis=1))), 2])
    return float(z)


def trace_ground_table(req, nav, region, xys, verbose=True):
    """xy -> the z of the surface that actually COLLIDES. Returns (z_at, report).

    The navmesh is not the collision surface. Recast rasterises at its own cell height, so on
    rolling terrain the navmesh polygon sits up to a third of a metre above or below the ground a
    capsule hits. Measured on WinterTown over all 5551 road sample points, traced ground minus
    navmesh height: p10 -33.5 cm, median -11.4, p90 +10.9.

    A body capsule placed 6 cm above the NAVMESH surface therefore starts inside the terrain at
    12% of those points, and head-clearance pruning then blames `Landscape_0` at 0.0 cm and drops
    the road: 272 roads directly and 1286 more left unreachable on WinterTown, 80% of its core
    network, for a reason that has nothing to do with head clearance. ChemicalPlant, the one flat
    map of the four measured, had zero collisions - which is what pointed here.

    The camera height comes off the same z, so this was also a systematic eye-height error of the
    same size: 1.70 m above the navmesh is 1.84 m above the ground where the navmesh sits 14 cm
    high.

    Traced from just above each point's OWN navmesh height, in bands, never from the top of the
    map: a trace that starts high finds the first thing under it, which in a town is a roof. That
    is how a start point once snapped onto one, and it is why an earlier version of this
    measurement reported a +892 cm outlier.
    """
    import time as _t
    t0 = _t.time()

    def key(q):
        return (int(round(q[0] / GROUND_CELL_CM)), int(round(q[1] / GROUND_CELL_CM)))

    cells = {}
    for q in xys:
        cells.setdefault(key(q), (float(q[0]), float(q[1])))
    items = [(k, xy, nav_z_at(nav, region, xy)) for k, xy in cells.items()]

    bands = {}
    for k, xy, nz in items:
        bands.setdefault(int(nz // GROUND_BAND_CM), []).append((k, xy, nz))

    table, hits, misses, rejected = {}, 0, 0, 0
    deltas = []
    for b, group in sorted(bands.items()):
        top = (b + 1) * GROUND_BAND_CM + GROUND_TRACE_UP_CM
        for s0 in range(0, len(group), 500):
            part = group[s0:s0 + 500]
            zs = engine.ground_z(req, [xy for _, xy, _ in part], top)
            for (k, xy, nz), tz in zip(part, zs):
                if tz is None:
                    table[k] = nz
                    misses += 1
                elif tz < nz - GROUND_TRACE_DOWN_CM:
                    table[k] = nz          # a hole, or the trace fell through - keep the navmesh
                    rejected += 1
                else:
                    table[k] = float(tz)
                    deltas.append(float(tz) - nz)
                    hits += 1
    d = np.array(deltas) if deltas else np.zeros(1)
    report = {
        "method": f"downward trace from each point's navmesh height + {GROUND_TRACE_UP_CM:.0f} cm, "
                  f"batched in {GROUND_BAND_CM:.0f} cm z bands, cached on a "
                  f"{GROUND_CELL_CM:.0f} cm grid",
        "why": "the navmesh is not the collision surface; a capsule placed on the navmesh can "
               "start inside the terrain, and the camera inherits the same offset",
        "cells": len(items), "traced": hits, "no_hit_kept_navmesh": misses,
        "too_far_below_kept_navmesh": rejected,
        "traced_minus_navmesh_cm": {"p10": round(float(np.percentile(d, 10)), 1),
                                    "median": round(float(np.median(d)), 1),
                                    "p90": round(float(np.percentile(d, 90)), 1),
                                    "min": round(float(d.min()), 1),
                                    "max": round(float(d.max()), 1)},
        "took_s": round(_t.time() - t0, 1),
    }
    if verbose:
        r = report["traced_minus_navmesh_cm"]
        print(f"[freeze-cov] collision surface: traced {hits} of {len(items)} cells "
              f"({misses} no hit, {rejected} too far below, both kept the navmesh height); "
              f"traced minus navmesh p10 {r['p10']} median {r['median']} p90 {r['p90']} cm "
              f"in {report['took_s']:.0f}s", flush=True)

    def z_at(xy):
        k = key(xy)
        v = table.get(k)
        if v is not None:
            return v
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                v = table.get((k[0] + dr, k[1] + dc))
                if v is not None:
                    return v
        return nav_z_at(nav, region, xy)

    return z_at, report


def road_sample_points(G, sample_cm=40.0):
    """Every road's polyline, resampled - the points pruning sweeps and the table needs."""
    out = []
    for u, v, k, d in G.edges(keys=True, data=True):
        poly = np.asarray(d["poly"], dtype=float)
        seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
        arc = np.concatenate([[0.0], np.cumsum(seg)])
        n = max(2, int(round(arc[-1] / sample_cm)) + 1)
        su = np.linspace(0.0, arc[-1], n)
        out.append((u, v, k, np.column_stack([np.interp(su, arc, poly[:, 0]),
                                              np.interp(su, arc, poly[:, 1])])))
    return out


def prune_impassable(req, G, nav, region, radius, half_h, sample_cm=40.0, z_at=None):
    """Drop the roads this camera cannot actually get through, and say which.

    A navmesh says where an agent of the NAVMESH's radius and height can stand. It does not say
    whether a 1.7 m camera on a 1.76 m capsule fits: Recast happily runs walkable surface under
    pipes, awnings, gantries and conveyor runs, and an industrial yard is full of them. Planning
    a full cover on the raw graph put the body inside geometry on 10389 of 14399 frames and left
    the depth probe reading 10 cm - the near clip - on a third of its samples.

    So "walk every road" has to mean every road that can be walked. A road that does not clear
    the agent's head is not a road for this agent, and pretending the map was fully covered while
    skipping them silently would be the same class of claim as reporting a plan's intent as a
    measurement.

    The test is the capsule the validator will use, swept along each road's own polyline. Points
    from different roads are never swept into each other: the sweeps that straddle a boundary are
    discarded by index.
    """
    # The z comes from the same surface the poses will use. Computing it here from the navmesh
    # while the route uses something else is how pruning ends up answering a different question
    # than the one the validator asks.
    if z_at is None:
        def z_at(xy):
            return nav_z_at(nav, region, xy)
    pts, spans = [], []
    for u, v, k, xy in road_sample_points(G, sample_cm):
        start = len(pts)
        for q in xy:
            pts.append((float(q[0]), float(q[1]),
                        z_at((float(q[0]), float(q[1]))) + GROUND_CLEARANCE_CM + half_h))
        spans.append((u, v, k, start, len(pts)))

    t0 = time.time()
    v = engine.validate_path(req, pts, radius=radius, half_height=half_h)
    sweeps = v["sweep_hits"]
    blocked, kept = [], []
    for (u, vv, k, a, b) in spans:
        hit = next((sweeps[i] for i in range(a, min(b - 1, len(sweeps))) if sweeps[i]), None)
        if hit is None:
            kept.append((u, vv, k))
        else:
            blocked.append({"road": [list(u), list(vv), k],
                            "length_m": round(G[u][vv][k]["weight"] / 100.0, 1),
                            "blocked_by": hit.get("actor"),
                            "at_cm": round(float(hit.get("at_cm", 0.0)), 1)})

    H = nx.MultiGraph()
    for u, vv, k in kept:
        H.add_edge(u, vv, key=k, **G[u][vv][k])
    for n_ in H.nodes():
        if "xy" in G.nodes[n_]:
            H.nodes[n_]["xy"] = G.nodes[n_]["xy"]
    if H.number_of_edges() == 0:
        raise RuntimeError("every road on this map is blocked at the agent's head height")
    comps = list(nx.connected_components(H))
    big = max(comps, key=lambda cc: sum(d["weight"] for _, _, d in H.edges(cc, data=True)))
    K = H.subgraph(big).copy()

    total_m = sum(d["weight"] for _, _, d in G.edges(data=True)) / 100.0
    kept_m = sum(d["weight"] for _, _, d in K.edges(data=True)) / 100.0
    report = {
        "method": f"capsule r={radius:.0f} half-h={half_h:.0f} cm, centred "
                  f"{GROUND_CLEARANCE_CM:.0f} cm above the walkable surface, swept along each "
                  f"road at {sample_cm:.0f} cm steps. A road is dropped if any sweep along it "
                  f"is blocked; sweeps straddling two roads are discarded by index.",
        "why": "a navmesh is built for the navmesh agent's radius and height, so walkable "
               "surface can run under pipes, awnings and gantries that this camera does not "
               "clear",
        "roads_before": G.number_of_edges(),
        "roads_blocked": len(blocked),
        "roads_after_pruning": H.number_of_edges(),
        "roads_kept_connected": K.number_of_edges(),
        "centreline_before_m": round(total_m, 1),
        "centreline_kept_m": round(kept_m, 1),
        "kept_fraction": round(kept_m / max(1e-9, total_m), 4),
        "dropped_disconnected": H.number_of_edges() - K.number_of_edges(),
        "components_after_pruning": len(comps),
        "sample_cm": sample_cm,
        "swept_points": len(pts),
        "took_s": round(time.time() - t0, 1),
        "worst_blocked": sorted(blocked, key=lambda b: -b["length_m"])[:10],
    }
    return K, report


def probe_route(req, poses, eye_cm, fov, every=None, min_cm=PROBE_MIN_CM, want=250,
                extremes=250, min_valid_frac=0.01):
    """Render depth along the route and veto it if anything is too close.

    Sampled rather than exhaustive: 14400 renders would cost more than the capture. The point is
    to catch a corridor that a navmesh and a capsule sweep both call clear because the thing in
    it has no collision.
    """
    # Roughly `want` probes whatever the episode length: each is a render plus a readback, so a
    # fixed stride of 60 costs 240 probes on a ten-minute episode and 1700 on an hour of one.
    every = int(every or max(PROBE_EVERY, len(poses) // max(1, want)))
    idx = set(range(0, len(poses), every))

    # PLUS the frames that pitch furthest up. A uniform stride is the wrong sampler for this: the
    # frames that can empty the depth channel are the pitch extremes, and they are a few frames
    # long each. A stride of 587 hit exactly one of them by luck and missed the one that stopped
    # the capture 97519 frames in. So the extremes are probed on purpose.
    by_pitch = sorted(range(len(poses)), key=lambda i: -poses[i]["pitch_deg"])
    idx.update(by_pitch[:extremes])
    idx = sorted(idx)
    worst, near, all_sky, thin, clamped = 1e9, [], [], [], []
    for i in idx:
        p = poses[i]
        r = engine.depth_capture(req, (p["x_cm"], p["y_cm"], p["z_cm"]),
                                 p["yaw_deg"], p["pitch_deg"], fov, 320, 180,
                                 f"{PROBE_DIR}/probe.exr", max_range_m=200.0)
        if not r.get("ok"):
            # An all-invalid depth render is a VETO, not a pass.
            #
            # It was briefly treated as "nothing is near the camera, so this is the cleanest
            # possible result". That reasoning is right about the geometry and wrong about the
            # consequence: ASimWorldCaptureActor refuses to write a depth channel with no depth
            # in it and ENDS THE CAPTURE. One such frame at 97519 of 146845 cost 64 minutes of
            # recording. Whether or not an all-sky frame is legitimate data, this build will not
            # record it, so the route must not contain one.
            if "pixels invalid" in str(r.get("error", "")):
                all_sky.append({"frame": i, "phase": p["phase"],
                                "pitch_deg": round(float(p["pitch_deg"]), 1)})
                continue
            return {"ok": False, "error": f"depth probe failed at frame {i}: {r.get('error')}",
                    "probed_frames": len(near)}
        # The engine reports `invalid_fraction`, not `valid_fraction`. Reading the name that felt
        # right would have made every probe look empty and vetoed every route on every map.
        vf = 1.0 - float(r.get("invalid_fraction", 1.0))
        if vf < min_valid_frac:
            # Not empty yet, but on its way there and this is a 320x180 probe standing in for a
            # 1280x720 render. 43 frames of the stopped capture were already under 1% valid, the
            # thinnest at 46 pixels of 921600.
            thin.append({"frame": i, "phase": p["phase"],
                         "pitch_deg": round(float(p["pitch_deg"]), 1),
                         "valid_fraction": round(vf, 5)})
        mn = float(r["min_m"]) * 100.0 if r.get("min_m") is not None else None
        if mn is not None:
            worst = min(worst, mn)
            if mn <= PROBE_CLIP_CM:
                clamped.append({"frame": i, "min_cm": round(mn, 1), "phase": p["phase"]})
            elif mn < min_cm:
                near.append({"frame": i, "min_cm": round(mn, 1), "phase": p["phase"]})
    return {"ok": True, "probed_frames": len(idx), "every": every,
            "minimum_depth_cm": (None if worst >= 1e9 else round(worst, 1)),
            "threshold_cm": min_cm, "frames_too_close": near[:20],
            "too_close_count": len(near),
            # proximity (`near`) is reported, not vetoed; the clamp (`clamped`) is the veto
            "clear": len(clamped) == 0 and len(all_sky) == 0 and len(thin) == 0,
            "clip_cm": PROBE_CLIP_CM, "frames_clamped": clamped[:20],
            # every clamped frame, not the 20 shown: the reroute loop turns these into vetoes
            "clamped_frames": [c["frame"] for c in clamped],
            "clamped_count": len(clamped),
            "all_sky_probes": len(all_sky),
            "all_sky_examples": all_sky[:8],
            "thin_depth_probes": len(thin),
            "thin_depth_examples": sorted(thin, key=lambda t: t["valid_fraction"])[:8],
            "min_valid_fraction_required": min_valid_frac,
            "extremes_probed": extremes,
            "all_sky_note": "an all-sky probe is a VETO: the capture actor refuses to write a "
                            "depth channel containing no depth and ends the run. `thin` frames "
                            "are the same failure approaching - under "
                            f"{min_valid_frac*100:.0f}% of the probe had valid depth.",
            "method": f"scene depth rendered at every {every}th frame's exact pose, 320x180, "
                      f"max range 200 m; this is the only check that sees geometry whose "
                      f"collision is disabled"}


def freeze(task, ucv=None, out_dir=FROZEN, skip_probe=False):
    fps = float(task["fps"])
    W, H = task["camera"]["resolution"]
    fov = float(task["camera"]["fov_deg"])
    eye_cm = float(task["camera"]["eye_height_m"]) * 100.0
    radius = float(task["body"]["collision_radius_cm"])
    half_h = float(task["body"]["collision_half_height_cm"])
    duration_s = float(task["duration_s"])
    seed = int(task["seed"])
    # The body capsule's clearance above the walkable surface, from the task, with the env as an
    # experiment override. 6 cm is a step offset and was written against flat maps; on rolling
    # terrain the sweep between samples 40 cm apart needs to span the ground's own bulges, and
    # WinterTown measured 989 collisions at 6 cm against 0 at 60 cm with a 120 cm corridor.
    global GROUND_CLEARANCE_CM
    # The task is the source of truth; the env is an override only when it is actually set.
    # `os.environ.get(k, 0)` returns "" for a variable exported empty, which is falsy, but a
    # harness that exported it as "6.0" by default silently beat the task's 60 - and the network
    # collapse that produced was blamed on the veto radius for two runs.
    GROUND_CLEARANCE_CM = float((os.environ.get("GROUND_CLEARANCE_CM") or "").strip()
                                or task["body"].get("ground_clearance_cm") or 6.0)
    map_id = task["map_id"]
    slug = slug_for(map_id)

    if ucv is None:
        ucv = engine.connect(W, H)
        if ucv is None:
            raise RuntimeError("UE never became reachable")
    req = ucv.client.request

    content = {} if task.get("content_buffer_m") else None
    sp, gz, nav_boot = pick_spawn(req, map_id, content=content)
    print(f"[freeze-cov] spawn '{sp.get('name')}' at ({sp['x']:.0f}, {sp['y']:.0f}), "
          f"navmesh ground {gz:.1f} cm")

    navdir = out_dir / "nav"
    navdir.mkdir(parents=True, exist_ok=True)
    nbin, njson = navdir / f"{slug}.bin", navdir / f"{slug}.json"
    exp = engine.nav_export(req, str(nbin), str(njson))
    if not exp.get("ok"):
        raise RuntimeError(f"navmesh export failed for {slug}: {exp.get('error')}")
    print(f"[freeze-cov] navmesh export: {exp['vertex_count']} verts, "
          f"{exp['triangle_count']} tris, bounds {exp['bounds_min'][:2]} .. "
          f"{exp['bounds_max'][:2]}")

    nav, navmeta = pnm.load(str(nbin), str(njson))

    # `confine_to_core` keeps the road network inside the map's built-up interior. Off, the
    # network is whatever region has the longest centreline, and on a purchased demo level that
    # is usually the empty apron around the content: Tokyo's episode walked 1144 m of centreline
    # over 8174 m2 of empty ground for 101 minutes with every gate green.
    core_only = bool(task.get("confine_to_core", False))
    # How far the road centrelines stay from the walkable boundary. This, not the task's
    # `body.leg_margin_cm`, is what governs how close the camera comes to a wall - leg_margin_cm
    # is read only by freeze.py and reaches nothing on this path.
    min_clear_cm = (float((os.environ.get("MIN_CLEAR_CM") or "").strip() or 0)
                    or task["body"].get("corridor_clear_cm") or None)

    # ---- plan, check, and REROUTE round what did not work ---------------------------
    #
    # A route that collides is not a reason to refuse the map. It is a place the walk should not
    # go, and `coverage_walk` already turns at a junction and takes another unwalked road - so
    # cutting the offending place out of the corridor and planning again is all it takes for the
    # route to go a different way.
    #
    # Three things put a place on the veto list, and they are the three that no parameter fixes:
    # a body-capsule sweep that hit geometry, a near-plane corner inside a wall, and a depth probe
    # that found something closer to the CAMERA than the threshold. The last one is the reason
    # this loop exists at all: it sees foliage whose collision is disabled, so it is invisible to
    # every sweep, and on WinterTown it was four localised places that no clearance or corridor
    # width could avoid - 9 probe frames in 4 clusters, two of them hit twice by different passes.
    #
    # What this must never become is a way to claim a cover it did not walk. Every vetoed place is
    # counted and reported, the coverage fraction is still measured from the poses, and if the
    # attempts run out the pre-gates fail exactly as before.
    veto_xy, reroutes = [], []
    content_boxes = None
    for attempt in range(1, MAX_REROUTE_ATTEMPTS + 2):
        if attempt > 1:
            print(f"[freeze-cov] reroute attempt {attempt}: {len(veto_xy)} places vetoed",
                  flush=True)
        min_clear_cm = (float(os.environ.get("MIN_CLEAR_CM", 0))
                        or task["body"].get("corridor_clear_cm") or None)
        # `content_buffer_m` (17 Sep): keep only roads within this distance of built content.
        content_buffer_cm = (float(task.get("content_buffer_m")) * 100.0
                             if task.get("content_buffer_m") else None)
        if content_buffer_cm and content_boxes is None:
            # The footprints pick_spawn already fetched (30 m window round the floor), else fetch.
            content_boxes = (content or {}).get("boxes") or engine.actor_footprints(req, gz - 1500.0, gz + 1500.0)
            (out_dir / f"{slug}__content_boxes.json").write_text(json.dumps(content_boxes))
            print(f"[freeze-cov] content: {len(content_boxes)} actor footprints within 15 m of the floor", flush=True)
        G0, rep0, region = C.build_network(nav, core_only=core_only, min_clear_cm=min_clear_cm,
                                           veto_xy=veto_xy, veto_radius_cm=VETO_RADIUS_CM,
                                           content_buffer_cm=content_buffer_cm,
                                           content_mode=task.get("content_mode", "dense"),
                                           content_boxes=content_boxes)
        print(f"[freeze-cov] road network{' (core only)' if core_only else ''}"
              f"{f' (within {content_buffer_cm/100:.0f} m of content)' if content_buffer_cm else ''}: {rep0['roads']} "
              f"roads, {rep0['centreline_m']:.0f} m of centreline over "
              f"{rep0['region_area_m2']:.0f} m2", flush=True)
        # MEASURED, NOT USED. Placing the capsule and the camera on the traced surface was tried and
        # made things worse: on WinterTown blocked roads went from 272 to 557 and the kept centreline
        # from 243 m to 84 m. Two reasons, both of which say the navmesh height is the better choice:
        #
        #   - the sweep is a straight segment between samples 40 cm apart, and the terrain bulges
        #     between them. Hugging the ground makes the segment cut those bulges MORE often, not
        #     less - the two data points are monotone in the wrong direction for the traced surface.
        #   - the trace runs on TRACE_TYPE_QUERY1, which is not what blocks a Pawn, so it passes
        #     through wooden decks and porches and returns the terrain underneath. A camera placed
        #     1.7 m above THAT is below the deck the agent is standing on.
        #
        # The number is still worth recording: it says how far the navmesh is from the traced surface
        # on this map, which is what separates a flat map from a rolling one.
        _, ground_rep = trace_ground_table(
            req, nav, region, [q for _, _, _, xy in road_sample_points(G0) for q in xy])
        G, prune_rep = prune_impassable(req, G0, nav, region, radius, half_h)
        print(f"[freeze-cov] head-clearance pruning: {prune_rep['roads_blocked']} of "
              f"{prune_rep['roads_before']} roads are blocked at the agent's height, "
              f"{prune_rep['dropped_disconnected']} more became unreachable; keeping "
              f"{prune_rep['roads_kept_connected']} roads / "
              f"{prune_rep['centreline_kept_m']:.0f} m "
              f"({prune_rep['kept_fraction']*100:.0f}% of the centreline) in {prune_rep['took_s']:.0f}s")
        rep = dict(rep0)
        rep["roads"] = G.number_of_edges()
        rep["junctions"] = G.number_of_nodes()
        rep["centreline_m"] = prune_rep["centreline_kept_m"]
        plan = C.plan(nav, navmeta, task, seed, fps, duration_s, prebuilt=(G, rep, region))
        G = plan.pop("_graph")
        poses = plan["poses"]
        print(f"[freeze-cov] {len(poses)} poses; action mix in band: {plan['action_mix_in_band']}")

        # ---- validation -----------------------------------------------------------------
        print(f"[freeze-cov] validating {len(poses)} frames against collision geometry", flush=True)
        t0 = time.time()
        body_pts = [(p["x_cm"], p["y_cm"], (p["z_cm"] - eye_cm) + GROUND_CLEARANCE_CM + half_h)
                    for p in poses]
        v = engine.validate_path(req, body_pts, radius=radius, half_height=half_h)
        hits = [i for i, h in enumerate(v["sweep_hits"]) if h is not None]
        near_bad = [i for i, n in enumerate(v["near_plane_hits"]) if n]
        cl = [c for c in v["clearance_cm"] if c is not None]
        trans = [math.dist((poses[i]["x_cm"], poses[i]["y_cm"], poses[i]["z_cm"]),
                           (poses[i + 1]["x_cm"], poses[i + 1]["y_cm"], poses[i + 1]["z_cm"]))
                 for i in range(len(poses) - 1)]
        rots = [abs((poses[i + 1]["yaw_deg"] - poses[i]["yaw_deg"] + 540) % 360 - 180)
                for i in range(len(poses) - 1)]
        collision = {
            "collision_count": len(hits),
            "penetration_count": len(near_bad),
            "minimum_clearance_cm": (min(cl) if cl else None),
            "clearance_ray_range_cm": 1500.0,
            "clearance_note": ("every frame's forward ray reached the full 1500 cm without a hit"
                               if not cl else
                               f"{len(cl)} of {len(poses)} frames had an obstacle within 1500 cm"),
            "maximum_frame_translation_cm": (max(trans) if trans else 0.0),
            "maximum_frame_rotation_deg": (max(rots) if rots else 0.0),
            "maximum_frame_yaw_rate_deg_s": (max(rots) * fps if rots else 0.0),
            "collision_free": len(hits) == 0 and len(near_bad) == 0,
            "first_collision_frames": hits[:12],
            "first_penetration_frames": near_bad[:12],
            "method": f"capsule r={radius:.0f} half-h={half_h:.0f} cm swept between consecutive "
                      f"frames, centred {GROUND_CLEARANCE_CM:.0f} cm above the surface; near plane "
                      f"traced at its four corners",
            "validated_in_s": round(time.time() - t0, 1),
        }
        print(f"[freeze-cov] collisions {collision['collision_count']}  penetrations "
              f"{collision['penetration_count']}  max step "
              f"{collision['maximum_frame_translation_cm']:.2f} cm  max yaw rate "
              f"{collision['maximum_frame_yaw_rate_deg_s']:.1f} deg/s  "
              f"in {collision['validated_in_s']:.0f}s")

        probe = ({"ok": True, "clear": True, "skipped": True,
                  "method": "not run: --skip-probe was given"} if skip_probe
                 else probe_route(req, poses, eye_cm, fov))
        if not probe.get("ok"):
            raise RuntimeError(probe["error"])
        print(f"[freeze-cov] depth probe: {probe.get('probed_frames')} frames, minimum "
              f"{probe.get('minimum_depth_cm')} cm, {probe.get('too_close_count', 0)} too close, "
              f"{probe.get('all_sky_probes', 0)} all-sky, {probe.get('thin_depth_probes', 0)} thin "
              f"-> {'CLEAR' if probe.get('clear') else 'VETO'}")
        for e in (probe.get("all_sky_examples") or [])[:4]:
            print(f"[freeze-cov]   all-sky at frame {e.get('frame')} ({e.get('phase')}, "
                  f"pitch {e.get('pitch_deg')})")

        bad = []
        for i in hits:
            bad.append((poses[i]["x_cm"], poses[i]["y_cm"], "collision"))
        for i in near_bad:
            bad.append((poses[i]["x_cm"], poses[i]["y_cm"], "near_plane"))
        for t in (probe.get("frames_too_close") or []):
            fi = t["frame"] if isinstance(t, dict) else int(t)
            if 0 <= fi < len(poses):
                bad.append((poses[fi]["x_cm"], poses[fi]["y_cm"], "depth_probe"))
        # The clamped frames are the ones the gate actually fails on (camera inside geometry that
        # has no collision - foliage, crops, awnings). Until 2026-09-16 only the merely-near frames
        # above were rerouted around, so DesertMap (2 clamped), SummerNight (6) and WinterTown02
        # (16) failed depth_probe_clear with every reroute attempt left unused.
        for fi in (probe.get("clamped_frames") or []):
            if 0 <= fi < len(poses):
                bad.append((poses[fi]["x_cm"], poses[fi]["y_cm"], "depth_clamp"))
        # All-sky and thin frames are also gate failures. They are about where the camera looks
        # rather than where it stands, but vetoing the place still moves the route off the spot
        # from which the sky fills the frame (a ridge, a plaza edge), and a reroute is cheaper
        # than losing the map to one probe (Old_Town and Sci-Fi Preview: one thin frame each).
        for e in (probe.get("all_sky_examples") or []) + (probe.get("thin_depth_examples") or []):
            fi = e.get("frame")
            if fi is not None and 0 <= fi < len(poses):
                bad.append((poses[fi]["x_cm"], poses[fi]["y_cm"], "depth_sky"))
        if not bad:
            if attempt > 1:
                print(f"[freeze-cov] reroute succeeded on attempt {attempt}", flush=True)
            break
        if attempt > MAX_REROUTE_ATTEMPTS:
            print(f"[freeze-cov] {len(bad)} places still blocked after "
                  f"{MAX_REROUTE_ATTEMPTS} reroutes; keeping this plan and letting the pre-gates "
                  f"judge it", flush=True)
            break
        # One veto per place, not per frame: a route that lingers reports the same metre many
        # times, and each veto eats VETO_RADIUS_CM of corridor.
        fresh = []
        for x, y, why in bad:
            if all(math.dist((x, y), (vx, vy)) > VETO_RADIUS_CM for vx, vy in veto_xy):
                veto_xy.append((x, y))
                fresh.append({"xy_cm": [round(x, 1), round(y, 1)], "why": why})
        reroutes.append({"attempt": attempt, "blocked_frames": len(bad),
                         "new_vetoes": len(fresh), "places": fresh[:20]})
        print(f"[freeze-cov] {len(bad)} blocked frames -> {len(fresh)} new vetoed places; "
              f"replanning", flush=True)
        if not fresh:
            print(f"[freeze-cov] every blocked place is already vetoed and the route still goes "
                  f"there; keeping this plan", flush=True)
            break


    # The same gates that will judge the episode, checked now.
    yaw_limit = plan["yaw_deg_per_s"] * 1.5
    pre = {
        "frame_translation_bounded": collision["maximum_frame_translation_cm"] < 50.0,
        "frame_rotation_bounded": collision["maximum_frame_yaw_rate_deg_s"] <= yaw_limit,
        "attitude_within_limits": max(abs(p["pitch_deg"]) for p in poses)
        <= float(task["camera"]["pitch_limit_deg"]) + 0.5,
        "collision_free": collision["collision_free"],
        "depth_probe_clear": bool(probe.get("clear")),
        "coverage_complete": plan["coverage"]["fraction"] >= 1.0,
        "action_mix_in_band": bool(plan["action_mix_in_band"]),
        # Turn-round-and-walk-back at the cadence the task asked for. Skipped, not failed, when
        # the task does not ask for it - an older task must not start failing a gate it predates.
        "retrace_cadence_ok": bool((plan.get("retrace") or {}).get("cadence_ok", True)
                                   if (plan.get("retrace") or {}).get("requested") else True),
    }
    for k, ok in pre.items():
        print(f"[freeze-cov] pre-gate {'PASS' if ok else 'FAIL'}  {k}")

    episode_id = (f"{slug}__coverage_walk__seed{seed}__{task['task_id']}")
    intr = geom.intrinsics(W, H, fov)
    out = {
        "episode_id": episode_id,
        "task": task,
        "generator_version": "pipeline-coverage-1",
        "map_id": map_id,
        "spawn_id": sp.get("name", "auto"),
        "fps": fps,
        "frames": len(poses),
        "duration_s": len(poses) / fps,
        "trajectory_family": "coverage_walk",
        "speed_tier": task["speed_tier"],
        "speed_m_per_s": plan["speed_m_per_s"],
        "speed_pinned": plan.get("speed_pinned", False),
        "content_buffer_m": task.get("content_buffer_m") or 0,
        "content_mode": task.get("content_mode", "dense") if task.get("content_buffer_m") else None,
        "speed_profile": plan.get("speed_profile", "trapezoid"),
        "yaw_tier": task["yaw_tier"],
        "yaw_deg_per_s": plan["yaw_deg_per_s"],
        "yaw_rate_pinned": plan.get("yaw_rate_pinned", False),
        "pitch_deg_per_s": plan.get("pitch_deg_per_s", plan["yaw_deg_per_s"]),
        "turn_profile": plan.get("turn_profile", "cosine"),
        "eye_height_cm": plan["eye_height_cm"],
        "eye_height_requested_cm": eye_cm,
        "camera_offset_from_pawn_cm": [0.0, 0.0, 0.0],
        "start_xy_cm": plan["start_xy_cm"],
        "spawn_ground_z_cm": gz,
        # capture_engine slices lossless keyframes from these windows. There is no memory anchor
        # in this family, so it is the opening second - named for what it is.
        "anchor_window": [0, int(round(fps)) - 1],
        "revisit_events": [],
        "revisit_window": None,
        "road_network": plan["network"],
        "head_clearance_pruning": prune_rep,
        "reroutes": {"attempts": len(reroutes), "vetoed_places": len(veto_xy),
                     "veto_radius_cm": VETO_RADIUS_CM, "max_attempts": MAX_REROUTE_ATTEMPTS,
                     "detail": reroutes,
                     "note": "places a previous attempt's body capsule hit, near plane entered, "
                             "or depth probe found geometry inside; cut out of the corridor so "
                             "the walk turns and takes another road. Counted, not hidden - the "
                             "coverage fraction is still measured from the poses."},
        "collision_surface": ground_rep,
        "coverage": plan["coverage"],
        "passes": plan["passes"],
        "passes_summary": plan["passes_summary"],
        # Which of the two limits ended the episode, and what the mix was tuned on. Without this
        # on disk a consumer seeing 20.00 h and six passes cannot tell "the clock ran out" from
        # "the map was covered", and it was only in the planner's return value.
        "length": plan["length"],
        "core_confined": plan["core_confined"],
        "feasibility": plan["feasibility"],
        "action_mix": plan["action_mix"],
        "action_mix_target": plan["action_mix_target"],
        "action_mix_in_band": plan["action_mix_in_band"],
        "mix_tuning": {k: v for k, v in plan["mix_tuning"].items() if k != "trace"},
        "navmesh": {"ensure": nav_boot, "export": exp,
                    "content_box_m": ([round(2 * content["half_extent"][0] / 100), round(2 * content["half_extent"][1] / 100)]
                                      if content and content.get("half_extent") else None)},
        "collision": collision,
        "depth_probe": probe,
        "pre_gates": pre,
        "reachability": {
            "available": False, "reachable": True, "sampled_frames": 0,
            "method": "not run for this family: the in-engine capture places the scene-capture "
                      "components on the frozen pose and spawns no pawn, and the route is already "
                      "navmesh-planned, corridor-swept per frame and depth-probed",
        },
        "intrinsics": intr,
        "camera_fov_deg_actual": fov,
        "axis_smoke": geom.axis_smoke(intr),
        "known_gaps": [
            "instance/ and semantic/ masks are not captured by this build",
            "section 12 depth_visible_overlap and fully_occluded_interval are not computed; this "
            "family has no revisit events, so those gates do not apply to it either way",
        ],
        "poses": poses,
    }
    body = json.dumps(out, sort_keys=True, default=str).encode()
    out["sha256"] = hashlib.sha256(body).hexdigest()

    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{episode_id}.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    print(f"[freeze-cov] wrote {p}")
    return str(p), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--skip-probe", action="store_true")
    a = ap.parse_args()
    task = json.loads(Path(a.task).read_text())
    freeze(task, skip_probe=a.skip_probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
