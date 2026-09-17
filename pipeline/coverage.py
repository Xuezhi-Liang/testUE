#!/usr/bin/env python3
"""Plan a coverage walk: visit every road on a map, with a varied action mix.

This is a third trajectory family, beside `nested_out_and_back` and `mirror_exact`. What it is
for is different: not "come back to a place and prove you were there before", but "walk
everywhere the agent can walk, doing more than one thing".

Two deliberate departures from the other families.

**Coverage, not closure.** The route is not the shortest closed tour. It is a randomised walk
that keeps taking an unwalked road when one is adjacent and otherwise takes the shortest path to
the nearest road it has not walked, until none are left. That is longer than the route-inspection
optimum by 20-60%, and it is what was asked for: the optimum is a single rigid answer, while a
randomised cover gives a different route from every seed and can be run again in a different
style over the same map.

**An action mix, not just forward.** Every episode this pipeline has produced so far is forward
travel with turns at the corners: 40% of the frames of a `nested_out_and_back` episode have no
translation at all, and none of them move backwards. Here the walk is cut into primitives -
forward, backward, turn, look up, look down, hold - and their proportions are targets that are
measured and reported per episode.

Most backward motion is a whole chunk of the route walked facing the other way, so it advances
along the route and covers new road while the body moves backwards. It costs an about-turn to
enter and one to leave, which is 2 seconds each at the medium yaw tier, and those frames are
turns the mix needs anyway.

Short retreats - back up along the ground just walked, heading unchanged - are kept as a rare
flavour rather than the main source of backward motion, because they re-walk what they just
covered. Sizing the whole backward budget out of retreats was the first version of this file: at
about twice the distance per back-up it inflated a 384 m covering walk to roughly 730 m, and the
covering pass stopped fitting inside ten minutes at all.

The frozen-file contract is unchanged: one commanded pose per frame, validated before capture,
and `phase` carries this frame's action label so the mix is machine-readable in the delivered
`frames.csv` rather than only in a report.

    python3 coverage.py <navmesh.bin> <navmesh.json> [--seed 7] [--duration 600] [--preview p.png]
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import cv2
import networkx as nx
from scipy import ndimage

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plan_navmesh as pnm
import survey_core as SC  # noqa: E402

CELL_CM = 25.0
# Body radius 40 cm plus a margin. NOT the other families' 150 cm leg margin: at 150 cm every
# road narrower than 3 m vanishes and a map of alleys reads as having no roads at all. The
# centreline of the >=90 cm region is by construction at least 90 cm from any boundary, and
# because thinning keeps the medial axis it sits near the widest point of each corridor.
MIN_CLEAR_CM = 90.0

# Speed profile is trapezoidal, not cosine. A cosine ease peaks at pi/2 times its mean, so a
# 1.3 m/s mean peaks at 2.04 m/s and pushes the p95 past the `speed_within_tier` gate's
# 1.25 x tier-high. A plateau with short ramps has max == nominal.
RAMP_S = 0.5

# `frame_rotation_bounded` fails any frame whose yaw rate exceeds 1.5 x the tier rate, so turns
# are sized by their PEAK rate rather than their mean. The envelope below is a RAISED COSINE
# (0.5 - 0.5*cos over a full period), whose peak is exactly 2x its mean - not the pi/2 that a
# half-cosine ease gives. Sizing it with pi/2 put the peak at 2/(pi/2) = 1.27x the intended
# ceiling: 83.8 deg/s against a 73.1 deg/s limit, close enough to look right and still a failure.
TURN_PEAK_FACTOR = 1.35
TURN_ENVELOPE_PEAK_OVER_MEAN = 2.0
# How a turn (and a look) is shaped in time. "cosine" is the raised-cosine envelope above: the
# rate rises from 0 to 1.35x the tier rate and falls back, so the tier rate is roughly the mean
# and every turn has a different instantaneous speed. "constant" is a fixed rate: every frame of
# every turn rotates exactly yaw_rate/fps degrees (the last frame takes the remainder), the peak
# IS the rate, and a 90 degree turn at 45 deg/s is 2.0 s to the frame. Asked for so the
# rotation speed is one known number across the dataset rather than a distribution.
TURN_PROFILES = ("cosine", "constant")
# How a walk is shaped in time. "trapezoid" is the plateau with RAMP_S ramps above: the camera
# accelerates into and out of every chunk, so 8-16% of walking frames are below the nominal
# speed. "constant" is a fixed step: every walking frame moves exactly speed/fps (the last frame
# of a segment takes the remainder), so the speed is one known number across the dataset. Asked
# for together with `speed_m_s`, the pinned pace, on 17 Sep.
SPEED_PROFILES = ("trapezoid", "constant")

Z_SLEW_CM_PER_S = 60.0
GROUND_CLEARANCE_CM = 0.0     # the camera is placed directly; there is no capsule to lift

# No single frame may move the camera further than this. The `frame_translation_bounded` gate's
# limit is 50 cm and the fastest legitimate step here is about 6 cm at 1.45 m/s and 24 fps, so
# anything past 60 cm is a defect in the planner rather than a fast walk. Enforced where the pose
# is written, not checked afterwards: the version that only checked afterwards shipped a 41 m
# teleport into validation and cost an editor round trip to find.
MAX_FRAME_STEP_CM = 60.0


# --------------------------------------------------------------------------- centrelines

def thin(mask):
    """Zhang-Suen thinning. cv2.ximgproc is absent from this build and skimage is not installed."""
    img = (mask > 0).astype(np.uint8)
    while True:
        removed = False
        for step in (0, 1):
            p = np.pad(img, 1)
            n = [p[0:-2, 1:-1], p[0:-2, 2:], p[1:-1, 2:], p[2:, 2:],
                 p[2:, 1:-1], p[2:, 0:-2], p[1:-1, 0:-2], p[0:-2, 0:-2]]
            B = sum(n)
            seq = n + [n[0]]
            A = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.uint8) for i in range(8))
            cond = (img == 1) & (B >= 2) & (B <= 6) & (A == 1)
            if step == 0:
                cond &= (n[0] * n[2] * n[4] == 0) & (n[2] * n[4] * n[6] == 0)
            else:
                cond &= (n[0] * n[2] * n[6] == 0) & (n[0] * n[4] * n[6] == 0)
            if cond.any():
                img[cond] = 0
                removed = True
        if not removed:
            return img


def surface_z(nav, region, xy):
    """Walkable-surface height at each xy, in cm. None where xy is off the surface."""
    out = np.full(len(xy), np.nan)
    for i, p in enumerate(xy):
        z = nav.inside_xy(np.array([p[0], p[1], 0.0]), region, tol=1.0)
        if z is not None:
            out[i] = z
    return out


def build_centrelines(nav, region, cell_cm=CELL_CM, min_clear_cm=MIN_CLEAR_CM,
                      core_mask=None, grid_origin=None, grid_shape=None,
                      veto_xy=None, veto_radius_cm=150.0, content_buffer_cm=None,
                      content_mode="dense"):
    """Walkable region -> a graph of road centrelines in world centimetres.

    Nodes are junctions and dead ends; each edge carries the polyline of the corridor between
    them, so the walk that comes out follows real road geometry rather than straight chords.

    `core_mask` restricts the corridor to the region's built-up interior before thinning. It is
    made by survey_core and passed in rather than recomputed here, so the maps the survey ranks
    and the roads a capture walks cannot drift apart. It must come with the grid it was made on
    (`grid_origin`, `grid_shape`), because a mask rasterised from a different origin lines up
    with nothing - survey_core rasters the whole export, this used to raster one region's own
    extent, and the offset between them is arbitrary.

    Without a mask the network is the whole walkable region, which on a purchased demo level is
    usually the flat apron around the content: Tokyo's coverage episode walked 1144 m of
    centreline over 8174 m2 of empty ground for 101 minutes with every gate green.

    `veto_xy` are places the route must not go: world xy where a previous attempt's body capsule
    hit geometry or its camera ended up inside something. Cutting them out of the corridor before
    thinning is what makes the walk turn and take a different road, rather than the run refusing
    the map - the graph simply no longer offers that way through, and `coverage_walk` picks
    another unwalked road at the junction as it always does.
    """
    tri = nav.wtris[region]
    pts = nav.verts[tri][:, :, :2].reshape(-1, 2)
    if grid_origin is not None:
        lo = np.asarray(grid_origin, dtype=float)
        H, W = grid_shape
    else:
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        W = int(np.ceil((hi[0] - lo[0]) / cell_cm)) + 4
        H = int(np.ceil((hi[1] - lo[1]) / cell_cm)) + 4
    grid = np.zeros((H, W), np.uint8)
    poly = np.round((nav.verts[tri][:, :, :2] - lo) / cell_cm).astype(np.int32) + 2
    # cv2.fillPoly takes points as (x, y) = (column, row), and this grid is indexed [row=y,
    # col=x], so the scaled (x, y) pairs go in AS THEY ARE. Reversing them here - which is what
    # this line used to do - transposes the whole map: the centrelines still came out looking
    # like a road network, so the preview passed inspection, but they did not correspond to the
    # walkable space. 8 of 12 sampled points on the longest road were not inside any navmesh
    # triangle, the height fell back to a downward trace that hit container roofs and a 13 m
    # gantry, and the body capsule ended up inside BP_Container8. On a non-square map it also
    # silently clipped: x-scaled values were written into rows sized from the y extent.
    cv2.fillPoly(grid, [p for p in poly], 1)

    clear = ndimage.distance_transform_edt(grid) * cell_cm
    safe = (clear >= min_clear_cm).astype(np.uint8)
    if safe.sum() == 0:
        raise RuntimeError(f"no part of this region is {min_clear_cm:.0f} cm from a boundary")
    if veto_xy is not None and len(veto_xy):
        rr = max(1, int(round(veto_radius_cm / cell_cm)))
        yy, xx = np.ogrid[-rr:rr + 1, -rr:rr + 1]
        disc = (xx * xx + yy * yy) <= rr * rr
        for wx, wy in veto_xy:
            c = int(round((float(wx) - lo[0]) / cell_cm)) + 2
            r = int(round((float(wy) - lo[1]) / cell_cm)) + 2
            r0, r1 = max(0, r - rr), min(H, r + rr + 1)
            c0, c1 = max(0, c - rr), min(W, c + rr + 1)
            if r1 <= r0 or c1 <= c0:
                continue
            safe[r0:r1, c0:c1] &= ~disc[rr - (r - r0):rr + (r1 - r), rr - (c - c0):rr + (c1 - c)]

    # `content_buffer_cm`: keep only walkable ground within this distance of BUILT CONTENT -
    # the enclosed obstacle footprints survey_core calls obstacles (buildings, walls, props of
    # 2 m2 or more), found on this same raster. Everything farther is open ground: it is walkable
    # and it is empty, and a purchased level ships a lot of it. Asked for on 17 Sep: "only record
    # where there is something". Softer than `core_mask`, which keeps the built-up INTERIOR only
    # and needed a 60 cm clearance and a 120 cm corridor to pass its gates - this keeps the
    # streets that run along the content, on both sides of it, and drops the fields.
    content_m2 = safe_before_buffer_m2 = None
    if content_buffer_cm:
        # "Content" here is where there is DENSITY of stuff, not any stuff: survey_core's density
        # core (walkable ground with >= 6% obstacle within 12 m - a street between buildings, a
        # grove, a yard full of props) plus any single footprint of 20 m2 or more (a building on
        # its own). A lone tree on a hillside is an obstacle but not content; taking every
        # obstacle kept 74% of a 4.6 km forest honeycomb on Mountains_Map, the density rule is
        # what removes the sparse outer terrain.
        _, _, obst, dcore = SC.core_of(grid)
        big = np.zeros_like(obst)
        if obst.any():
            lab, n = ndimage.label(obst)
            sz = ndimage.sum(obst, lab, range(1, n + 1)) * cell_cm ** 2 / 1e4
            k = np.zeros(n + 1, bool); k[1:] = sz >= 20.0
            big = k[lab]
        # content_mode "dense": density core OR any 20 m2 footprint (groves, yards, rock fields
        # count). "buildings": only footprints of 20 m2 or more - a forest or a rock field is
        # then empty ground and only the built structures keep their surroundings.
        if content_mode not in ("dense", "buildings"):
            raise RuntimeError(f"content_mode must be 'dense' or 'buildings', got {content_mode!r}")
        content = (dcore | big) if content_mode == "dense" else big
        content_m2 = round(float(content.sum()) * cell_cm ** 2 / 1e4, 1)
        safe_before_buffer_m2 = round(float(safe.sum()) * cell_cm ** 2 / 1e4, 1)
        if not content.any():
            raise RuntimeError("content buffer: this region has no dense content and no footprint "
                               "of 20 m2 or more - nothing to record here")
        near = ndimage.distance_transform_edt(~content) * cell_cm <= float(content_buffer_cm)
        safe = (safe.astype(bool) & near).astype(np.uint8)
        if safe.sum() == 0:
            raise RuntimeError(f"content buffer: no walkable ground within {content_buffer_cm/100:.0f} m "
                               f"of any built content that is also {min_clear_cm:.0f} cm from a boundary")
    if core_mask is not None:
        if np.shape(core_mask) != (H, W):
            raise RuntimeError(f"core mask is {np.shape(core_mask)} for a {(H, W)} grid; it was "
                               f"made on a different raster and would not line up")
        if cell_cm != SC.CELL:
            raise RuntimeError(f"core mask cells are {SC.CELL} cm, this raster's are {cell_cm}")
        safe = (safe.astype(bool) & np.asarray(core_mask, dtype=bool)).astype(np.uint8)
        if safe.sum() == 0:
            raise RuntimeError("no part of this region's built-up interior is "
                               f"{min_clear_cm:.0f} cm from a boundary")
    lab, n = ndimage.label(safe)
    sizes = ndimage.sum(safe, lab, range(1, n + 1))
    keep = int(np.argmax(sizes)) + 1
    safe = (lab == keep).astype(np.uint8)
    skel = thin(safe)

    def to_world(rc):
        r, c = rc
        return (float(lo[0] + (c - 2) * cell_cm), float(lo[1] + (r - 2) * cell_cm))

    sk = np.argwhere(skel > 0)
    idx = {(int(r), int(c)) for r, c in sk}
    nbrs = {}
    for (r, c) in idx:
        adj = [(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
               if not (dr == 0 and dc == 0) and (r + dr, c + dc) in idx]
        nbrs[(r, c)] = adj
    if not nbrs:
        # Thinning left nothing. Raise the same class of error every other "this region is not
        # usable" path raises, because build_network's loop skips RuntimeError and lets anything
        # else kill the whole map: `next(iter(nbrs))` on an empty dict raises StopIteration with
        # NO MESSAGE, which is how MiddleEast reported `error: ""` from a fleet machine and cost
        # three instances and a diagnosis.
        raise RuntimeError(f"thinning this region's {int(safe.sum())} safe cells left no "
                           f"centreline pixels at all")
    nodes = {p for p, a in nbrs.items() if len(a) != 2}
    if not nodes:
        nodes = {next(iter(nbrs))}

    G = nx.MultiGraph()
    for p in nodes:
        G.add_node(p, xy=to_world(p))
    walked = set()
    for start in nodes:
        for first in nbrs[start]:
            if (start, first) in walked:
                continue
            chain, prev, cur = [start], start, first
            while cur not in nodes:
                chain.append(cur)
                nxt = [q for q in nbrs[cur] if q != prev]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
            chain.append(cur)
            walked.add((start, first))
            walked.add((cur, chain[-2]))
            if cur not in nodes:
                continue
            world = np.array([to_world(p) for p in chain])
            length = float(np.sum(np.linalg.norm(np.diff(world, axis=0), axis=1)))
            if length < 1.0:
                continue
            G.add_edge(start, cur, weight=length, poly=world,
                       clearance_cm=float(min(clear[p] for p in chain)))

    # Only the largest connected component is walkable without teleporting.
    if G.number_of_edges() == 0:
        raise RuntimeError("thinning produced no corridors")
    comps = list(nx.connected_components(G))
    big = max(comps, key=lambda cc: sum(d["weight"] for _, _, d in G.edges(cc, data=True)))
    kept = G.subgraph(big).copy()
    dropped_m = sum(d["weight"] for _, _, d in G.edges(data=True)) / 100.0 - \
        sum(d["weight"] for _, _, d in kept.edges(data=True)) / 100.0

    # Do the centrelines actually lie on the walkable surface? They are derived from a raster of
    # it, so they must - and when the raster's axes were transposed they did not, silently. The
    # only symptom downstream was a height lookup that fell back to a downward trace and put the
    # body inside a shipping container 200 frames later. Sampled rather than exhaustive: this is
    # an axis check, and an axis error is never subtle.
    checked = 0
    off = 0
    for _, _, d in list(kept.edges(data=True))[::max(1, kept.number_of_edges() // 40)]:
        for q in np.asarray(d["poly"])[::max(1, len(d["poly"]) // 4)]:
            checked += 1
            if nav.inside_xy(np.array([q[0], q[1], 0.0]), region) is None:
                off += 1
    if checked and off / checked > 0.25:
        raise RuntimeError(
            f"{off} of {checked} sampled centreline points are not inside any triangle of the "
            f"region they were derived from ({off/checked*100:.0f}%). The raster and its inverse "
            f"disagree - check that fillPoly's (x, y) order matches to_world's (row, col).")

    report = {
        "cell_cm": cell_cm,
        "min_clearance_cm": min_clear_cm,
        "centreline_off_surface_sampled": checked,
        "centreline_off_surface_count": off,
        "grid": [int(H), int(W)],
        "region_area_m2": round(float(nav.tri_areas(region).sum()) / 1e4, 1),
        "corridor_area_m2": round(float(safe.sum()) * cell_cm ** 2 / 1e4, 1),
        "centreline_m": round(sum(d["weight"] for _, _, d in kept.edges(data=True)) / 100.0, 1),
        "junctions": kept.number_of_nodes(),
        "roads": kept.number_of_edges(),
        "components_found": len(comps),
        "dropped_unreachable_m": round(dropped_m, 1),
        "min_road_clearance_cm": round(min(d["clearance_cm"] for _, _, d in
                                           kept.edges(data=True)), 1),
        "content_buffer_cm": content_buffer_cm,
        "content_mode": content_mode if content_buffer_cm else None,
        "content_m2": content_m2,
        "corridor_before_content_buffer_m2": safe_before_buffer_m2,
    }
    return kept, report


# ------------------------------------------------------------------------------- the walk

def coverage_walk(G, rng, start=None):
    """A randomised walk that traverses every road at least once.

    Not the route-inspection optimum, on purpose. From the current junction it takes a random
    road it has not walked; when every adjacent road is walked it takes the shortest path to the
    nearest junction that still has one. It stops when no road is left unwalked.

    Returns the ordered list of (u, v, key, reversed) traversals and a per-step tag saying
    whether the step was covering new road or repositioning across old.
    """
    # Normalised on the way in as well as on the way out: G.edges() reports each edge in one
    # (u, v) order and G.edges(node) reports it in whichever order puts `node` first, so an
    # un-normalised set never matches half its own lookups and the walk cannot terminate.
    unwalked = {_norm(u, v, k) for u, v, k in G.edges(keys=True)}
    node = start if start is not None else _pick_start(G, rng)
    steps = []
    guard = 0
    while unwalked:
        guard += 1
        if guard > 20000:
            raise RuntimeError("coverage walk did not converge")
        here = [(u, v, k) for u, v, k in G.edges(node, keys=True)
                if _norm(u, v, k) in unwalked]
        if here:
            u, v, k = here[rng.integers(len(here))]
            steps.append((node, v if u == node else u, k, "cover"))
            unwalked.discard(_norm(u, v, k))
            node = v if u == node else u
            continue
        # nothing new here: walk to the nearest junction that still has unwalked road
        targets = {n for u, v, k in unwalked for n in (u, v)}
        dist, paths = nx.single_source_dijkstra(G, node, weight="weight")
        reachable = [(dist[t], t) for t in targets if t in dist]
        if not reachable:
            break
        _, goal = min(reachable)
        path = paths[goal]
        for a, b in zip(path[:-1], path[1:]):
            k = min(G[a][b], key=lambda kk: G[a][b][kk]["weight"])
            # A repositioning path can run over road that has not been walked yet. Counting that
            # as covered is not bookkeeping convenience: walking it again later would be pure
            # waste, and the walk is already there.
            nk = _norm(a, b, k)
            fresh = nk in unwalked
            unwalked.discard(nk)
            steps.append((a, b, k, "cover" if fresh else "reposition"))
        node = goal
    return steps, len(unwalked)


def _norm(u, v, k):
    return (u, v, k) if u <= v else (v, u, k)


def _pick_start(G, rng):
    """Start at a junction with the widest road, so the first frames are not in a doorway."""
    best = max(G.edges(keys=True, data=True), key=lambda e: e[3]["clearance_cm"])
    return best[0]


def walk_polyline(G, steps):
    """Traversals -> one continuous polyline of world xy, plus a per-vertex 'new road' flag."""
    pts, tags = [], []
    for (a, b, k, tag) in steps:
        poly = np.asarray(G[a][b][k]["poly"], dtype=float)
        ax = np.asarray(G.nodes[a]["xy"], dtype=float)
        if np.linalg.norm(poly[0] - ax) > np.linalg.norm(poly[-1] - ax):
            poly = poly[::-1]
        if pts and np.linalg.norm(poly[0] - pts[-1]) < 1e-6:
            poly = poly[1:]
        for p in poly:
            pts.append(p)
            tags.append(tag)
    return np.array(pts), tags


def smooth(poly, window=7):
    """Moving average over the resampled path, endpoints pinned.

    The skeleton is 8-connected, so every centreline runs along a grid direction and its heading
    is quantised to multiples of 45 degrees. Walking that raw gives a 45 degree heading change in
    a single frame - 1080 deg/s against a 73 deg/s limit - and it reads as a zigzag on screen.
    The quantisation is an artefact of the grid, not of the road, so it is smoothed out.

    Mild on purpose: a 7-sample window at 20 cm spacing averages over 1.2 m, which straightens
    the stair-stepping without cutting corners far enough to matter against the 90 cm clearance
    the centreline is guaranteed. The engine-side capsule sweep is still the judge of that, and
    it refuses the route rather than trimming it.
    """
    if len(poly) < window or window < 3:
        return poly.copy()
    k = np.ones(window) / window
    out = poly.copy()
    for axis in (0, 1):
        pad = np.pad(poly[:, axis], (window // 2, window // 2), mode="edge")
        out[:, axis] = np.convolve(pad, k, mode="valid")[:len(poly)]
    out[0], out[-1] = poly[0], poly[-1]
    return out


def resample(poly, step_cm=20.0):
    """Even arc-length resampling, so a 'metre along the path' means the same everywhere."""
    d = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(d)])
    total = float(s[-1])
    if total <= 0:
        return poly.copy(), np.array([0.0])
    n = max(2, int(round(total / step_cm)) + 1)
    su = np.linspace(0.0, total, n)
    out = np.column_stack([np.interp(su, s, poly[:, 0]), np.interp(su, s, poly[:, 1])])
    return out, su


# --------------------------------------------------------------------------- action mix

# Each style is one pass over the map. Running the same map several times in different styles is
# what fills a long episode without the walk degenerating into one repeated behaviour.
STYLES = {
    # `speed_frac` and `back_frac` are positions INSIDE the task's speed tier, not m/s. They used
    # to be absolute: a style walked at 1.05-1.30 m/s whatever the task declared, so setting
    # speed_tier to `medium` (0.6-1.0) changed the reported number and the gate's limit while the
    # camera kept strolling at 1.3 m/s. Same class of defect as the trapezoid that overshot its
    # own nominal speed - the metadata said one thing and the poses did another.
    #
    # back_frac is a fraction of THIS style's forward speed: walking backwards is slower than
    # walking forwards at any pace.
    "survey": dict(chunk_m=(9.0, 18.0), p_back_chunk=0.16, p_retreat=0.10, retreat_m=(2.0, 3.5),
                   p_look=0.70, p_scan=0.40, speed_frac=(0.50, 0.85), back_frac=(0.45, 0.62),
                   look_deg=(14.0, 26.0), scan_deg=(50.0, 95.0), hold_s=(0.4, 0.9),
                   look_hold_s=(0.5, 1.1), p_look_both=0.35),
    "inspect": dict(chunk_m=(5.0, 10.0), p_back_chunk=0.28, p_retreat=0.18, retreat_m=(2.0, 4.0),
                    p_look=0.95, p_scan=0.60, speed_frac=(0.25, 0.60), back_frac=(0.42, 0.58),
                    look_deg=(18.0, 29.0), scan_deg=(65.0, 120.0), hold_s=(0.6, 1.3),
                    look_hold_s=(0.8, 1.6), p_look_both=0.60),
    "patrol": dict(chunk_m=(14.0, 26.0), p_back_chunk=0.12, p_retreat=0.06, retreat_m=(1.5, 2.5),
                   p_look=0.45, p_scan=0.30, speed_frac=(0.75, 1.00), back_frac=(0.50, 0.66),
                   look_deg=(10.0, 22.0), scan_deg=(40.0, 80.0), hold_s=(0.3, 0.7),
                   look_hold_s=(0.4, 0.8), p_look_both=0.25),
    # For the passes AFTER the map is covered, and for a strolling episode generally: short
    # chunks and near-certain looks make this action-dominated rather than travel-dominated.
    "study": dict(chunk_m=(2.5, 5.0), p_back_chunk=0.30, p_retreat=0.35, retreat_m=(1.5, 3.0),
                  p_look=1.00, p_scan=0.90, speed_frac=(0.10, 0.45), back_frac=(0.40, 0.55),
                  look_deg=(18.0, 29.0), scan_deg=(70.0, 130.0), hold_s=(0.8, 1.8),
                  look_hold_s=(1.0, 2.0), p_look_both=0.80),
    # A tourist: ambling, stopping often, looking up at things. Longer dwells than `survey` and a
    # slower position in the tier, but it still makes progress along the route - unlike `study`,
    # which is for spending leftover budget.
    "stroll": dict(chunk_m=(6.0, 13.0), p_back_chunk=0.14, p_retreat=0.14, retreat_m=(2.0, 3.5),
                   p_look=0.95, p_scan=0.65, speed_frac=(0.20, 0.55), back_frac=(0.42, 0.58),
                   look_deg=(16.0, 29.0), scan_deg=(55.0, 115.0), hold_s=(0.8, 1.8),
                   look_hold_s=(1.1, 2.2), p_look_both=0.65),
}

# Turn round, walk back the way you came for a while, turn round again and carry on.
#
# This is NOT the `backward` primitive. A backward chunk keeps facing the direction of travel's
# opposite - the body moves back while the camera still looks where it was going - so the pixels
# are the ones already seen. A RETRACE turns the camera about and walks forwards down the route
# it just covered, so the same ground arrives from the opposite view direction. For a spatial
# memory dataset that is the point: a place is only revisited if it is seen again, and seen from
# somewhere else.
RETRACE = {"median_interval_s": 300.0, "interval_jitter": 0.25, "max_interval_s": 420.0,
           "min_duration_s": 10.0, "max_duration_s": 25.0}
# A chunk is followed by look, scan and hold actions before the next cadence check; this is the
# allowance for them, so the interval is met from the event's END to the next event's START.
TAIL_MARGIN_S = 12.0
# Start looking for a good place to turn round at this fraction of the interval; past 1.0 the
# next straight-enough spot is no longer waited for.
RETRACE_LOOK_FOR = 0.75

LABELS = ["forward", "backward", "turn_left", "turn_right", "look_up", "look_down", "hold"]

# What "a reasonable range" is made concrete. Every episode this pipeline had produced before was
# forward travel with corner turns and no backward motion at all, so these bands exist to stop
# the mix collapsing back to that. They are targets for a search, not assertions: the achieved
# fractions are measured, reported, and gated separately.
# These are the second set. The first was a guess made before anything had been measured, and two
# of its bands turned out narrower than the mechanism can hit while still covering the map:
# covering 413 m inside 14400 frames pins the travel share near two thirds, which in turn pins
# how much is left for everything else. Widened once, against measurement, and the widening is
# recorded here rather than presented as the original target.
#
# `hold` is deliberately the loosest. Standing still is a by-product of scanning, not one of the
# six actions this family exists to produce, so it is bounded rather than aimed at.
TARGET_MIX = {
    "forward":    (0.35, 0.52),
    "backward":   (0.12, 0.25),
    "turn_left":  (0.06, 0.15),
    "turn_right": (0.06, 0.15),
    "look_up":    (0.04, 0.12),
    "look_down":  (0.04, 0.12),
    "hold":       (0.005, 0.08),
}

# Multiplicative knobs the tuner is allowed to move. Chunk length is inverted on purpose: a
# shorter forward chunk means the other primitives come round more often.
KNOBS = ("back", "look", "scan", "chunk", "hold", "speed")


def scale_styles(knobs):
    """`knobs["_tier"]` carries the task's (lo, hi) speed tier in m/s; the rest are multipliers."""
    """Apply the tuner's multipliers to every style at once, so the styles keep their character
    relative to each other while the overall mix moves."""
    out = {}
    for name, cfg in STYLES.items():
        c = dict(cfg)
        c["p_back_chunk"] = min(0.75, cfg["p_back_chunk"] * knobs["back"])
        c["p_retreat"] = min(0.5, cfg["p_retreat"] * knobs["back"])
        c["p_look"] = min(1.0, cfg["p_look"] * knobs["look"])
        c["look_hold_s"] = tuple(v * min(2.2, knobs["look"]) for v in cfg["look_hold_s"])
        c["p_scan"] = min(1.0, cfg["p_scan"] * knobs["scan"])
        c["scan_deg"] = tuple(min(140.0, v * min(1.5, knobs["scan"])) for v in cfg["scan_deg"])
        c["chunk_m"] = tuple(max(3.0, v * knobs["chunk"]) for v in cfg["chunk_m"])
        c["hold_s"] = tuple(v * min(3.0, knobs["hold"]) for v in cfg["hold_s"])
        # Walking pace is a knob because it is the only lever on the travel share that is not
        # already pinned. Covering 413 m costs a fixed distance; turns are mostly structural
        # (dead-end spurs and the about-turns into backward stretches), and trading backward
        # distance for forward pushes forward through its ceiling. Walking the same distance
        # faster frees frames AND lowers the forward share at once.
        #
        # Capped at the `fast` tier's own 1.5 m/s, well inside the speed_within_tier gate's
        # 1.875 m/s p95 limit, so tuning cannot buy frames by breaking the tier.
        lo, hi = knobs["_tier"]
        sp = tuple(lo + f * (hi - lo) for f in cfg["speed_frac"])
        sp = tuple(float(np.clip(v * knobs["speed"], lo * 0.5, hi)) for v in sp)
        c["speed"] = sp
        c["back_speed"] = tuple(float(np.clip(sp[i] * cfg["back_frac"][i], 0.15, hi))
                                for i in (0, 1))
        # A pinned pace (`knobs["_pin"]`, from the task's speed_m_s) overrides the tier, the
        # style's position in it, the backward fraction and the tuner's speed knob alike: every
        # walking primitive - forward, backward, retreat, the retrace legs - moves at that one
        # speed. The tuner is told not to touch the speed knob when this is set.
        if knobs.get("_pin") is not None:
            v = float(knobs["_pin"])
            c["speed"] = (v, v)
            c["back_speed"] = (v, v)
        out[name] = c
    return out


def retrace_report(passes, n_frames, fps, cfg):
    """What the turn-round-and-walk-back events actually came out as.

    The cadence is reported as the largest gap between one event ENDING and the next STARTING,
    with the run-in from frame 0 counted as a gap so an episode cannot satisfy the rule by doing
    nothing for four minutes and then catching up. The run-out is reported but not judged: an
    episode may legitimately end at any point after the last event.
    """
    if not cfg:
        return {"requested": None, "events": 0}
    evs = sorted((r for p in passes for r in p.get("retraces", [])), key=lambda r: r["start_frame"])
    gaps, prev_end = [], 0
    for r in evs:
        gaps.append((r["start_frame"] - prev_end) / fps)
        prev_end = r["end_frame"]
    tail = (n_frames - prev_end) / fps
    short = [r for r in evs if r["back_seconds"] < cfg["min_duration_s"] - 1e-6]
    return {
        "requested": cfg,
        "events": len(evs),
        "max_gap_s": round(max(gaps), 1) if gaps else round(tail, 1),
        "run_out_s": round(tail, 1),
        "min_back_seconds": round(min((r["back_seconds"] for r in evs), default=0.0), 2),
        "median_turn_deg": round(float(np.median([r["turn_deg"] for r in evs])), 1) if evs else 0.0,
        "events_without_turn": sum(1 for r in evs if r["turn_deg"] < 150.0),
        "median_back_seconds": round(float(np.median([r["back_seconds"] for r in evs])), 2) if evs else 0.0,
        "total_back_m": round(sum(r["back_m"] for r in evs), 1),
        "short_events": len(short),
        "median_gap_s": round(float(np.median(gaps)), 1) if gaps else None,
        # A route shorter than the cap owes no event at all: judging it "no retrace" failed every
        # one-pass validation of a small map (a 15 s route cannot contain a 300 s cadence). And
        # the median band is only meaningful with a few gaps to take a median of; with fewer than
        # four it is one draw's jitter, so only the cap and the minimum walk-back are judged.
        "cadence_ok": bool(
            (not evs and n_frames / fps <= cfg["max_interval_s"])
            or (evs and not short
                and max(gaps) <= cfg["max_interval_s"] + 1.0
                and (len(gaps) < 4
                     or 0.8 * cfg["median_interval_s"] <= float(np.median(gaps)) <= 1.25 * cfg["median_interval_s"]))),
        "median_judged": len(gaps) >= 4,
        "events_detail": evs[:40],
        "note": "each event is: about-turn, walk the covered route facing that way, about-turn "
                "back. Both legs are labelled `forward` - the camera faces the way it moves; "
                "what changes is which direction the ground is seen from.",
    }


def mix_penalty(frac, target=TARGET_MIX):
    """Distance outside the target bands. Zero means every action is inside its band."""
    pen = 0.0
    worst = ("", 0.0)
    for k, (lo, hi) in target.items():
        v = frac.get(k, 0.0)
        d = (lo - v) if v < lo else ((v - hi) if v > hi else 0.0)
        pen += d * d
        if d > worst[1]:
            worst = (k, d)
    return pen, worst


class PoseWriter:
    """Expands motion primitives into one pose per frame, enforcing the pipeline's limits."""

    def __init__(self, fps, eye_cm, yaw_rate, pitch_limit, z_at, pitch_limit_up=None,
                 turn_profile="cosine", speed_profile="trapezoid"):
        self.fps = float(fps)
        self.eye = float(eye_cm)
        self.yaw_rate = float(yaw_rate)
        if turn_profile not in TURN_PROFILES:
            raise ValueError(f"turn_profile must be one of {TURN_PROFILES}, got {turn_profile!r}")
        self.turn_profile = turn_profile
        if speed_profile not in SPEED_PROFILES:
            raise ValueError(f"speed_profile must be one of {SPEED_PROFILES}, got {speed_profile!r}")
        self.speed_profile = speed_profile
        self.retraces = []          # one entry per turn-round-and-walk-back event
        self.retrace_target = None  # seconds until the next one, redrawn after each
        # The fastest any single frame may rotate; the walking loop stops and turns rather than
        # exceed it, and `frame_rotation_bounded` checks 1.5x the tier rate against it.
        self.peak_rate = self.yaw_rate * (TURN_PEAK_FACTOR if turn_profile == "cosine" else 1.0)
        self.pitch_limit = float(pitch_limit)
        # Looking UP is capped harder than looking down, and not for taste.
        #
        # A frame whose depth is entirely invalid is REFUSED by the capture actor - and rightly
        # so: for a dataset frame an empty depth channel cannot be told apart from a capture that
        # produced nothing. Looking up walks straight into that. Measured on a Tokyo street: at
        # pitch 0 only 35.5% of the frame has valid depth because the rest is sky, and pitching up
        # collapses it - 16.4% at 11 deg, 3.6% at 20 deg, 0.99% at 24 deg, 0.005% at 27 deg (46
        # pixels of 921600), and zero at 27.5 deg, which stopped a capture 97519 frames in.
        #
        # There is no universal geometric limit: how far up you can look before the ground leaves
        # frame depends on how open the spot is. Half the vertical FOV (29.4 deg here) is far too
        # generous - the near ground had left the frame long before that. So the cap is
        # conservative and the depth probe checks the extremes as well.
        #
        # Down is unconstrained by this: the ground is always in view looking down.
        self.pitch_limit_up = float(pitch_limit if pitch_limit_up is None else pitch_limit_up)
        self.z_at = z_at            # xy -> surface z, in cm
        self.poses = []
        self.labels = []
        self.yaw = 0.0
        self.pitch = 0.0
        self.xy = None
        self.z = None

    # ---- helpers
    def _emit(self, xy, yaw, pitch, label):
        if self.xy is not None:
            jump = float(np.linalg.norm(np.asarray(xy, dtype=float) - self.xy))
            if jump > MAX_FRAME_STEP_CM:
                raise RuntimeError(
                    f"frame {len(self.poses)} ({label}) would move the camera {jump/100:.2f} m "
                    f"in one frame, past the {MAX_FRAME_STEP_CM:.0f} cm limit. The trajectory is "
                    f"not continuous - consecutive passes must start where the previous one "
                    f"ended, and a travel primitive must not begin on a different path.")
        z_surface = self.z_at(xy)
        target = z_surface + self.eye + GROUND_CLEARANCE_CM
        if self.z is None:
            self.z = target
        else:
            # A per-frame surface lookup turns a 20 cm kerb into a single-frame teleport. A head
            # moves vertically at well under a metre per second, so the rate is bounded.
            cap = Z_SLEW_CM_PER_S / self.fps
            self.z += max(-cap, min(cap, target - self.z))
        self.poses.append({"x_cm": float(xy[0]), "y_cm": float(xy[1]), "z_cm": float(self.z),
                           "yaw_deg": float(yaw % 360.0), "pitch_deg": float(pitch),
                           "roll_deg": 0.0, "phase": label})
        self.labels.append(label)
        self.xy = np.asarray(xy, dtype=float)
        self.yaw = yaw
        self.pitch = pitch

    @property
    def n(self):
        return len(self.poses)

    # ---- primitives
    def travel(self, path, s_from, s_to, s_axis, speed_ms, backward):
        """Move along the resampled path between two arc-length positions.

        Heading is the direction of travel when going forwards and its opposite when going
        backwards - facing the way it came is what makes it a backwards step rather than a turn
        followed by a forwards one.
        """
        forward_sign = 1.0 if s_to >= s_from else -1.0
        dist = abs(s_to - s_from)
        if dist < 1.0:
            return s_from
        v = speed_ms * 100.0
        # Solve the frame count from the trapezoid's own AREA, not from the distance at nominal
        # speed. Sizing n as dist/(v/fps) and then normalising the weights to `dist` forces the
        # plateau above nominal, because the ramps carry less than their share: with quarter-
        # length ramps at both ends the plateau came out n/(n-ramp) = 1.33x too fast, so a 1.3 m/s
        # walk peaked at 1.74 m/s. The speed_within_tier gate still passed - its limit is 1.25x
        # the tier - which is exactly why this had to be caught by arithmetic rather than by the
        # gate. A trapezoid of n frames with ramps of r covers (n - r) * v/fps.
        if self.speed_profile == "constant":
            # n-1 frames at exactly v/fps and ONE remainder frame (at most a full step), the same
            # shape as the constant turn profile: the sum is exact and no frame exceeds the speed.
            step_cm = v / self.fps
            n = max(1, int(math.ceil(dist / step_cm - 1e-9)))
            w = np.full(n, step_cm)
            w[-1] = dist - step_cm * (n - 1)
        else:
            frames_at_v = dist * self.fps / v
            ramp = min(int(self.fps * RAMP_S), max(1, int(frames_at_v) // 4))
            n = max(1, int(round(frames_at_v + ramp)))
            w = np.ones(n)
            if ramp > 0 and n > 2 * ramp:
                w[:ramp] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, ramp))
                w[-ramp:] = 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, ramp))
            # A final uniform correction so the segment lands exactly on `dist`. It is within
            # 1/n of unity by construction, so it cannot restore the overshoot this replaced.
            w = w / w.sum() * dist
        s = s_from
        label = "backward" if backward else "forward"
        # Per-frame yaw ceiling, the same one `frame_rotation_bounded` will apply.
        max_dyaw = self.peak_rate / self.fps
        for i in range(n):
            s = s + forward_sign * w[i]
            xy = self._at(path, s_axis, s)
            head = self._heading(path, s_axis, s, forward_sign)
            yaw = head + (180.0 if backward else 0.0)
            d = _shortest(self.yaw, yaw)
            if abs(d) > max_dyaw:
                # The path reverses on itself - a dead-end spur walked out and back - or corners
                # harder than the camera can follow at this speed. Stop and turn, rather than
                # snapping: snapping produced exact 180 degree steps in a single frame, inside a
                # phase labelled `forward`, at 4320 deg/s.
                self.turn(d)
            self._emit(xy, yaw, self.pitch, label)
        return s

    def _rotation_profile(self, delta_deg):
        """Per-frame increments summing to delta_deg, shaped by `turn_profile`."""
        if self.turn_profile == "constant":
            step = self.yaw_rate / self.fps
            n = max(1, int(math.ceil(abs(delta_deg) / step - 1e-9)))
            # n-1 frames at exactly the rate and ONE remainder frame (at most a full step): the
            # sum is exact and no frame exceeds the rate. Spreading the remainder over all n
            # frames instead - the first version - made a 1.5-step turn two frames at 0.75x,
            # and the median turning frame on Tokyo came out at 44.6 deg/s with a floor of 22.5.
            sign = 1.0 if delta_deg >= 0 else -1.0
            prof = np.full(n, sign * step)
            prof[-1] = delta_deg - sign * step * (n - 1)
            return prof
        peak = self.yaw_rate * TURN_PEAK_FACTOR
        n = max(2, int(math.ceil(abs(delta_deg) / peak * self.fps
                                 * TURN_ENVELOPE_PEAK_OVER_MEAN)))
        prof = 0.5 - 0.5 * np.cos(np.linspace(0, 2 * np.pi, n + 1))[1:]
        return prof / prof.sum() * delta_deg

    def turn(self, delta_deg, label=None):
        if abs(delta_deg) < 0.5:
            return
        prof = self._rotation_profile(delta_deg)
        lab = label or ("turn_left" if delta_deg < 0 else "turn_right")
        y = self.yaw
        for d in prof:
            y += d
            self._emit(self.xy, y, self.pitch, lab)

    def look(self, delta_deg, hold_s=0.0):
        """Pitch away from level, dwell there, and come back.

        The dwell is not decoration: without it a look is 15 frames each way and the action is
        over before it is legible in the video or usefully sampled in the data.
        """
        target = float(np.clip(delta_deg, -self.pitch_limit, self.pitch_limit_up))
        lab = "look_up" if target > 0 else "look_down"
        self._pitch_to(target, lab)
        if hold_s > 0:
            self.hold(hold_s, lab)
        self._pitch_to(0.0, lab)

    def _pitch_to(self, signed, lab):
        delta = signed - self.pitch
        if abs(delta) < 0.25:
            return
        prof = self._rotation_profile(delta)
        p = self.pitch
        for d in prof:
            p += d
            self._emit(self.xy, self.yaw, p, lab)

    def hold(self, seconds, label="hold"):
        for _ in range(max(1, int(round(seconds * self.fps)))):
            self._emit(self.xy, self.yaw, self.pitch, label)

    # ---- path sampling
    @staticmethod
    def _at(path, s_axis, s):
        s = float(np.clip(s, s_axis[0], s_axis[-1]))
        return np.array([np.interp(s, s_axis, path[:, 0]), np.interp(s, s_axis, path[:, 1])])

    def _heading(self, path, s_axis, s, sign):
        h = 70.0
        a = self._at(path, s_axis, s - h * sign)
        b = self._at(path, s_axis, s + h * sign)
        d = b - a
        if np.linalg.norm(d) < 1e-6:
            return self.yaw
        return math.degrees(math.atan2(d[1], d[0]))


def _retrace(W, path, s, s_axis, cfg, rng, seconds, min_seconds):
    """Turn about, walk back down the route facing that way, turn about again. Returns the new s.

    Both legs are labelled `forward` because that is the action being commanded: the camera faces
    the way it moves. What changes is the heading, and with it the view of ground already covered.

    Refuses rather than shortens: if there is not `min_seconds` of route behind the camera, no
    retrace happens here and the caller tries again after the next chunk. A three-second stub
    would satisfy a counter while delivering none of the reverse-view frames it exists for.
    """
    # The two headings are the same 1.4 m chord read in opposite directions, so they are exactly
    # 180 degrees apart wherever the path is not degenerate: the about-turn is a real about-turn
    # by construction, and there is no "straighter spot" to wait for.
    fwd = W._heading(path, s_axis, s, 1.0)
    rev = W._heading(path, s_axis, s, -1.0)
    speed = rng.uniform(*cfg["speed"])
    have_cm = s - float(s_axis[0])
    want_cm = seconds * speed * 100.0
    if have_cm < min_seconds * speed * 100.0:
        return s, None
    back_cm = min(want_cm, have_cm)
    s_back = s - back_cm
    n0 = W.n
    # Face the way we have been going before turning about, so the event always contains the
    # reversal. Without this a retrace that follows a backward chunk starts already facing the
    # way it is about to walk, and turns by nothing at all.
    _turn_to(W, fwd, rng)
    yaw_before = W.yaw
    _turn_to(W, rev, rng)                                    # about-turn, split L/R by coin toss
    turn_deg = abs(_shortest(yaw_before, W.yaw))             # measured HERE: by the time the
    n_turn = W.n                                             # record is built the camera has
                                                             # already turned back to face forward
    s = W.travel(path, s, s_back, s_axis, speed, backward=False)
    n_back = W.n
    _turn_to(W, W._heading(path, s_axis, s, 1.0), rng)       # face the original direction again
    W.retraces.append({
        "start_frame": n0, "end_frame": W.n,
        "back_start_frame": n_turn, "back_end_frame": n_back,
        "back_frames": n_back - n_turn,
        "back_seconds": round((n_back - n_turn) / W.fps, 2),
        "back_m": round(back_cm / 100.0, 1),
        "speed_ms": round(speed, 2),
        # How far the camera actually turned to start the walk back. Normally about 180; near
        # zero when the chunk before this one was itself walked backwards, because the camera was
        # already facing the way it is about to go. The walk back is the same either way - this
        # number is here so "it turned round" is a measurement rather than an assumption.
        "turn_deg": round(turn_deg, 1),
    })
    return s, W.retraces[-1]


def script_pass(W, path, s_axis, cfgs, rng, budget_frames, pace_reserve=None,
                segment_m=45.0, retrace=None):
    """Walk one full pass of the path, injecting the style's action primitives.

    `cfgs` is a list of already-scaled style dicts, not style names: the mix tuner moves these
    numbers between evaluations. The route is divided into `segment_m` stretches and the styles
    rotate across them, so one covering pass is walked several different ways along its length.
    That is what carries "walk it more than once, differently" when a second full lap will not
    fit: the covering walk already re-walks 1.6x its own centreline, and a second lap's travel
    frames would push forward past its band on their own.

    Returns True if the pass completed, False if the frame budget ran out first - the caller
    guarantees the FIRST pass completes, so coverage is never partial.
    """
    s = float(s_axis[0])
    end = float(s_axis[-1])
    n_start = W.n
    span = max(1e-9, end - float(s_axis[0]))

    def behind():
        """Is this pass falling behind the pace it needs to finish inside its reserve?

        Without this the completion of the covering pass depended on the seed: changing a knob
        changes how many random draws happen, so one setting finished the route and a neighbouring
        one ran out of frames halfway. Coverage is the hard requirement, so the optional
        primitives yield to it and come back when the pace recovers.
        """
        if pace_reserve is None:
            return False
        progress = (s - float(s_axis[0])) / span
        used = (W.n - n_start) / max(1, pace_reserve)
        return used > progress + 0.06

    # face the way we are about to go before the first step, so frame 0 is not a snap
    if W.n == 0:
        W.xy = W._at(path, s_axis, s)
        W.yaw = W._heading(path, s_axis, s, 1.0)
        W.hold(0.4, "hold")
    else:
        _turn_to(W, W._heading(path, s_axis, s, 1.0), rng)

    while s < end - 1.0:
        if W.n >= budget_frames:
            return False
        cfg = cfgs[int((s - float(s_axis[0])) / (segment_m * 100.0)) % len(cfgs)]
        chunk = rng.uniform(*cfg["chunk_m"]) * 100.0
        s_goal = min(end, s + chunk)

        # Walk this chunk facing forwards or facing backwards. A backward chunk still advances
        # along the route, so it covers new road while the body moves backwards - unlike a
        # retreat, which re-walks ground it just covered and inflates the route. The first
        # version of this did only retreats: at ~2x the distance per back-up it turned a 384 m
        # route into ~730 m and the covering pass no longer fitted in ten minutes at all.
        # Due for a retrace? A REQUIREMENT, not a probability: it fires even when the pass is
        # behind its pace reserve, because the tuner can walk faster but cannot invent an event
        # the task asked for. It is tested BEFORE the chunk and against what the chunk will cost,
        # since a check afterwards overruns the interval by a whole chunk plus its look/scan tail
        # - measured 181 to 203 s against a 180 s requirement. `_retrace` declines when there is
        # not enough route behind, so this is re-tested every chunk rather than scheduled once.
        if retrace:
            since = W.n - (W.retraces[-1]["end_frame"] if W.retraces else 0)
            # Cost of the chunk about to be walked, at the style's MEAN pace. Using the slow
            # end of the range overestimates it by 3x on a slow style, which pushed `due` past 1
            # after 20 s and fired a retrace every 80 s instead of every 180.
            pace = max(0.5 * (cfg["speed"][0] + cfg["speed"][1]), 0.05)
            cost = chunk / (pace * 100.0) * W.fps + TAIL_MARGIN_S * W.fps
            # Two bounds, both stated in the task rather than emergent: never sooner than
            # min_interval_s, never later than max_interval_s. Between them the walk looks for
            # locally straight ground so the event reads as a real about-turn; at the deadline it
            # takes whatever ground it is on.
            # Each gap is drawn around the task's MEDIAN rather than clamped to a floor: a
            # floor makes every gap sit on the floor, because the walk fires as soon as it is
            # allowed to. Straight ground is looked for from 90% of the target; past 120% of it
            # (or the hard cap) the next spot is taken whatever its shape.
            if float(retrace.get("median_interval_s") or 0) <= 0:
                retrace = None                   # belt and braces: never a zero-length target
            elif W.retrace_target is None:
                j = float(retrace.get("interval_jitter") or 0.0)
                W.retrace_target = float(retrace["median_interval_s"]) * rng.uniform(1.0 - j, 1.0 + j)
            tgt = W.retrace_target * W.fps
            # Fire at the first chunk boundary at or after the target, and in any case before
            # the hard cap. `cost` is what the chunk about to be walked will take, so the cap is
            # respected from the event's END to the next event's START.
            # Fire at the chunk boundary NEAREST the target, not the first one past it: waiting
            # for the boundary after the target overshoots by a whole chunk, which on a map with
            # long chunks pulled the realised median to 355 s against a 300 s target.
            if since + 0.5 * cost >= tgt or (since + cost) >= retrace["max_interval_s"] * W.fps:
                want = rng.uniform(retrace["min_duration_s"], retrace["max_duration_s"])
                s, _rec = _retrace(W, path, s, s_axis, cfg, rng, want, retrace["min_duration_s"])
                if _rec:
                    W.retrace_target = None      # draw a fresh target for the next gap
                if _rec:
                    s_goal = min(end, s + chunk)     # the chunk is walked from where we ended up

        backward = rng.random() < cfg["p_back_chunk"]
        head = W._heading(path, s_axis, s, 1.0)
        _turn_to(W, head + (180.0 if backward else 0.0), rng)
        speed = rng.uniform(*(cfg["back_speed"] if backward else cfg["speed"]))
        s = W.travel(path, s, s_goal, s_axis, speed, backward=backward)

        # A short retreat, kept rare on purpose: it is the most natural-looking backward step
        # and the most expensive in route length.
        if not behind() and rng.random() < cfg["p_retreat"]:
            back = rng.uniform(*cfg["retreat_m"]) * 100.0
            s_back = max(s_axis[0], s - back)
            s = W.travel(path, s, s_back, s_axis, rng.uniform(*cfg["back_speed"]),
                         backward=not backward)
            s = W.travel(path, s, min(end, s + back), s_axis, speed, backward=backward)

        if not behind() and rng.random() < cfg["p_look"]:
            # Alternate rather than coin-toss: over ~30 look events a fair coin still left
            # look_up at 5.9% and look_down at 4.6%, straddling the bottom of the band for no
            # reason other than sampling noise.
            W.look_parity = not getattr(W, "look_parity", False)
            up = W.look_parity
            mag = rng.uniform(*cfg["look_deg"])
            hold = rng.uniform(*cfg["look_hold_s"])
            W.look(mag if up else -mag, hold)
            # sometimes both ways in one go - a person checking a roof then the ground
            if rng.random() < cfg.get("p_look_both", 0.0):
                W.look(-mag if up else mag, hold * 0.8)
        if not behind() and rng.random() < cfg["p_scan"]:
            a = rng.uniform(*cfg["scan_deg"]) * (1 if rng.random() < 0.5 else -1)
            W.turn(a)
            W.hold(rng.uniform(*cfg["hold_s"]))
            W.turn(-a)
    return True


def _shortest(frm, to):
    return (to - frm + 540.0) % 360.0 - 180.0


def _turn_to(W, target_yaw, rng):
    """Turn to a heading, breaking the 180-degree tie at random.

    `_shortest` maps a difference of exactly 180 to -180, so every about-turn came out as a left
    turn: entering and leaving backward chunks is where about-turns happen, and it skewed the mix
    to 12.7% turn_left against 5.8% turn_right. There is no shorter way round a half turn, so the
    direction is a free choice and it should be a coin toss rather than a rounding artefact.
    """
    d = _shortest(W.yaw, target_yaw)
    # Anything past 150 degrees is an about-turn for practical purposes. A 0.5 degree tolerance
    # around exactly 180 never fired: the route curves, so a real about-turn comes out as 176 to
    # 184 degrees, and the left bias survived at 9.8% turn_left against 5.8% turn_right.
    if abs(d) > 150.0 and rng.random() < 0.5:
        d = d - 360.0 if d > 0 else d + 360.0
    W.turn(d)


def measure_coverage(G, poses, radius_cm=160.0, sample_cm=40.0):
    """Which roads the DELIVERED frames actually walked.

    Measured from the poses, not from the plan. The planner knows what route it intended; only
    the poses know what the episode contains. Reporting the intent as the result is how an
    episode truncated halfway would have claimed 221 of 221 roads covered - which is exactly
    what the first version of this did.

    A road counts as walked when every point along it is within `radius_cm` of some frame's
    camera position.
    """
    from scipy.spatial import cKDTree
    xy = np.array([[p["x_cm"], p["y_cm"]] for p in poses])
    tree = cKDTree(xy)
    walked, missed, total_m, walked_m = 0, [], 0.0, 0.0
    for u, v, k, d in G.edges(keys=True, data=True):
        poly = np.asarray(d["poly"], dtype=float)
        seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        n = max(2, int(round(s[-1] / sample_cm)) + 1)
        su = np.linspace(0.0, s[-1], n)
        pts = np.column_stack([np.interp(su, s, poly[:, 0]), np.interp(su, s, poly[:, 1])])
        dist, _ = tree.query(pts)
        total_m += d["weight"] / 100.0
        if float(dist.max()) <= radius_cm:
            walked += 1
            walked_m += d["weight"] / 100.0
        else:
            missed.append({"road": [list(u), list(v), k],
                           "worst_gap_cm": round(float(dist.max()), 1),
                           "length_m": round(d["weight"] / 100.0, 1)})
    return {
        "method": f"every point of a road within {radius_cm:.0f} cm of some frame's camera "
                  f"position, sampled every {sample_cm:.0f} cm; measured from the delivered "
                  f"poses, not from the plan",
        "radius_cm": radius_cm,
        "roads_total": G.number_of_edges(),
        "roads_walked": walked,
        "roads_missed": len(missed),
        "fraction": round(walked / max(1, G.number_of_edges()), 4),
        "centreline_m": round(total_m, 1),
        "centreline_walked_m": round(walked_m, 1),
        "length_fraction": round(walked_m / max(1e-9, total_m), 4),
        "worst_misses": sorted(missed, key=lambda m: -m["worst_gap_cm"])[:8],
    }


def feasibility(centreline_walk_m, fps, budget_frames, speed_ms):
    """The floor on frames: the covering walk with no turns, looks, holds or backward steps.
    If this alone does not fit, no action mix will make it fit and the map is the wrong size."""
    floor = fps * centreline_walk_m / speed_ms
    return {
        "walk_m": round(centreline_walk_m, 1),
        "min_frames_all_forward": int(round(floor)),
        "budget_frames": int(budget_frames),
        "slack_frames": int(round(budget_frames - floor)),
        "slack_fraction": round(1.0 - floor / budget_frames, 3),
        "feasible": floor < budget_frames,
    }


def mix_report(labels):
    n = len(labels)
    counts = {k: 0 for k in LABELS}
    for l in labels:
        counts[l] = counts.get(l, 0) + 1
    return {"frames": n,
            "fraction": {k: round(counts.get(k, 0) / n, 4) for k in LABELS},
            "frames_by_action": {k: counts.get(k, 0) for k in LABELS}}


# ------------------------------------------------------------------------------- planning

def build_network(nav, core_only=False, min_clear_cm=None, veto_xy=None,
                  veto_radius_cm=150.0, content_buffer_cm=None, content_mode="dense"):
    """Pick the region the agent can use and reduce it to a road graph. Done once - thinning is
    the expensive step and it does not depend on anything the action tuner moves.

    Longest centreline wins, which is why this used to pick Tokyo's empty apron: 8174 m2 of flat
    ground with 1144 m of centreline against the 2151 m2 street network that has every building
    on the map in it. Under `core_only` the corridor is the region's built-up interior, so the
    apron contributes almost no centreline and loses on its own terms - and the region that wins
    is then the one with the most interior road, which is the thing worth recording.
    """
    if core_only:
        # The region is chosen by its interior, by the same code that ranked the maps. Choosing
        # it here by trying every region would also mean computing every region's core - 16
        # dilations with a 50 m kernel each, which on a 149-region map is most of the runtime -
        # and could pick a different region than the survey did, which is worse than slow.
        sel = SC.best_core_region(nav, verbose=True)
        # Passed explicitly, never left to the default. `min_clear_cm=MIN_CLEAR_CM` in the
        # signature binds at definition time, so reassigning the module constant after import
        # changes nothing - an experiment that raised it to 120 cm got a road network identical
        # to the 90 cm one and read as "the lever does nothing".
        G, rep = build_centrelines(nav, sel["region"], core_mask=sel["core"],
                                   grid_origin=sel["lo"], grid_shape=sel["shape"],
                                   min_clear_cm=(MIN_CLEAR_CM if min_clear_cm is None
                                                 else float(min_clear_cm)),
                                   veto_xy=veto_xy, veto_radius_cm=veto_radius_cm)
        rep["min_clear_cm"] = (MIN_CLEAR_CM if min_clear_cm is None else float(min_clear_cm))
        rep["vetoed_places"] = 0 if not veto_xy else len(veto_xy)
        rep["veto_radius_cm"] = veto_radius_cm
        rep["core_m2"] = sel["stats"]["core_m2"]
        rep["core_of_region_safe"] = round(
            sel["stats"]["core_m2"] / max(sel["stats"]["safe_m2"], 1e-9), 3)
        return G, rep, sel["region"]

    best, skipped = None, []
    for region in nav.regions:
        if float(nav.tri_areas(region).sum()) / 1e4 < 20.0:
            continue
        try:
            # With a content buffer the region is still chosen by its centreline, but the
            # centreline is measured AFTER the buffer: an empty apron loses its length and the
            # region with the streets wins on its own terms.
            G, rep = build_centrelines(nav, region,
                                       min_clear_cm=(MIN_CLEAR_CM if min_clear_cm is None
                                                     else float(min_clear_cm)),
                                       veto_xy=veto_xy, veto_radius_cm=veto_radius_cm,
                                       content_buffer_cm=content_buffer_cm, content_mode=content_mode)
        except Exception as e:
            # One unusable region must not decide the map. Only RuntimeError used to be skipped,
            # so a degenerate region raising anything else took the whole build down with it.
            skipped.append(f"{type(e).__name__}: {str(e)[:120]}")
            continue
        if best is None or rep["centreline_m"] > best[1]["centreline_m"]:
            best = (G, rep, region)
    if best is None:
        raise RuntimeError("no region on this map has a usable road network"
                           + (f"; {len(skipped)} region(s) were skipped: {skipped[:4]}"
                              if skipped else ""))
    return best


def make_z_at(nav, region):
    """Walkable-surface height, cached on a 50 cm grid: a per-frame triangle search over 1200
    triangles for 14400 frames is 17 million point-in-triangle tests for no added accuracy."""
    zcache = {}
    verts = nav.verts[nav.wtris[region]].reshape(-1, 3)

    def z_at(xy):
        key = (int(round(xy[0] / 50.0)), int(round(xy[1] / 50.0)))
        if key not in zcache:
            z = nav.inside_xy(np.array([xy[0], xy[1], 0.0]), region, tol=60.0)
            if z is None:
                # nearest corridor vertex rather than an invented height
                d = np.linalg.norm(verts[:, :2] - np.asarray(xy), axis=1)
                z = float(verts[int(np.argmin(d)), 2])
            zcache[key] = float(z)
        return zcache[key]
    return z_at


def prepare_walks(G, seed, count):
    """One randomised covering walk per pass, resampled. Independent of the action knobs, so the
    tuner can re-script the same routes instead of re-solving them.

    Each walk STARTS WHERE THE PREVIOUS ONE ENDED. Drawing every walk an independent start put
    the second pass somewhere else on the map, and the writer then walked to it in a single
    frame: 41.4 m of translation in one 24th of a second, a 180 degree yaw step, and 10372
    capsule hits because the sweep between those two points crossed the whole level. It refused
    correctly, but only at the end - the invariant in PoseWriter._emit now refuses at the source.
    """
    rng = np.random.default_rng(seed)
    walks = []
    start = None
    for i in range(count):
        sd = int(rng.integers(1 << 30))
        steps, left = coverage_walk(G, np.random.default_rng(sd), start=start)
        if left:
            raise RuntimeError(f"walk left {left} of {G.number_of_edges()} roads unwalked")
        poly, _ = walk_polyline(G, steps)
        path, s_axis = resample(poly, step_cm=20.0)
        # Smooth on the evenly spaced points, then RE-MEASURE: the axis must be the arc length of
        # the path that is actually walked. Keeping the pre-smoothing axis - what this did until
        # 17 Sep - made a 4.17 cm advance along s land anywhere from 0.2 to 6 cm of real chord
        # (chord/ds 0.04-1.43 on ModularCourtyard, the axis 9% longer than the path), so the
        # camera's speed was never the commanded one and folded dead-end spurs nearly stood still.
        # Found the day the pace was pinned, because a pinned pace is the first setting under
        # which the per-frame step has one expected value.
        path, s_axis = resample(smooth(path), step_cm=20.0)
        cover_m = sum(G[a][b][k]["weight"] for a, b, k, t in steps if t == "cover") / 100.0
        repos_m = sum(G[a][b][k]["weight"] for a, b, k, t in steps if t == "reposition") / 100.0
        walks.append({"seed": sd, "path": path, "s_axis": s_axis,
                      "roads_walked": sum(1 for _, _, _, t in steps if t == "cover"),
                      "covering_m": cover_m, "repositioning_m": repos_m,
                      "start_node": list(steps[0][0]), "end_node": list(steps[-1][1])})
        start = steps[-1][1]
    return walks


def generate(G, rep, walks, styles, knobs, fps, budget, eye_cm, pitch_limit, yaw_rate, z_at,
             reserve_frac=0.97, fill_styles=("study", "inspect"), pitch_limit_up=None,
             turn_profile="cosine", retrace=None, speed_profile="trapezoid"):
    """One episode's worth of poses for a given knob setting.

    The covering pass is paced to finish within `reserve_frac` of the budget, and the action
    density is what fills the rest of it. Only if the route runs out with frames still unspent
    does a further pass start, in the dense `fill_styles`.
    """
    scaled = scale_styles(knobs)
    W = PoseWriter(fps, eye_cm, yaw_rate, pitch_limit, z_at, pitch_limit_up=pitch_limit_up,
                   turn_profile=turn_profile, speed_profile=speed_profile)
    passes = []
    for i, walk in enumerate(walks):
        if W.n >= budget:
            break
        if i == 0:
            names = list(styles)
            reserve = int(budget * reserve_frac)
        else:
            names = [fill_styles[(i - 1) % len(fill_styles)]]
            reserve = None
        style = "+".join(names)
        prng = np.random.default_rng(walk["seed"] ^ 0x5EED)
        n0 = W.n
        complete = script_pass(W, walk["path"], walk["s_axis"], [scaled[n] for n in names],
                               prng, budget, pace_reserve=reserve, retrace=retrace)
        passes.append({
            "pass": i + 1, "style": style, "walk_seed": walk["seed"],
            "frames": min(W.n, budget) - n0, "from_frame": n0,
            "to_frame": min(W.n, budget) - 1,
            "complete_pass": bool(complete),
            "roads_walked": walk["roads_walked"], "roads_total": G.number_of_edges(),
            "covering_m": round(walk["covering_m"], 1),
            "repositioning_m": round(walk["repositioning_m"], 1),
            "walk_m": round(walk["covering_m"] + walk["repositioning_m"], 1),
            "retraces": [r for r in W.retraces if n0 <= r["start_frame"] < min(W.n, budget)],
            "walk_over_centreline": round(
                (walk["covering_m"] + walk["repositioning_m"]) / rep["centreline_m"], 3),
        })
    return W.poses[:budget], W.labels[:budget], passes


def measure_passes(G, rep, walks, styles, knobs, fps, eye_cm, pitch_limit, yaw_rate, z_at,
                   fill_styles, pitch_limit_up=None, turn_profile="cosine", retrace=None,
                   speed_profile="trapezoid"):
    """Frames each covering pass actually needs at this pace and mix, measured not estimated.

    Run the scripting once against a budget large enough that it cannot bind and read off what
    every pass consumed. Guessing a duration and hoping the pass fits is how an episode ends up
    covering part of a map while reporting the plan's intent as coverage.

    One run answers every sizing question, which matters once the answer is wanted for eight
    passes as well as one: a huge-budget run scripts all of them, so asking twice would cost a
    second full-length scripting for a number already in hand.
    """
    huge = 40_000_000
    _, _, passes = generate(G, rep, walks, styles, knobs, fps, huge, eye_cm, pitch_limit,
                            yaw_rate, z_at, reserve_frac=1.0, fill_styles=fill_styles,
                            pitch_limit_up=pitch_limit_up, turn_profile=turn_profile, retrace=retrace,
                            speed_profile=speed_profile)
    if not passes or not passes[0]["complete_pass"]:
        raise RuntimeError("the covering pass did not finish even against an unbounded budget")
    return [int(pp["frames"]) for pp in passes if pp["complete_pass"]]


def size_episode(G, rep, walks, styles, knobs, fps, eye_cm, pitch_limit, yaw_rate, z_at,
                 fill_styles, headroom=1.04, verbose=True, pitch_limit_up=None,
                 passes_wanted=1, turn_profile="cosine"):
    """How many frames do `passes_wanted` full covering passes need at this pace and mix?"""
    per = measure_passes(G, rep, walks, styles, knobs, fps, eye_cm, pitch_limit, yaw_rate,
                         z_at, fill_styles, pitch_limit_up=pitch_limit_up,
                         turn_profile=turn_profile)
    if len(per) < passes_wanted:
        raise RuntimeError(
            f"only {len(per)} of {passes_wanted} covering passes completed against an unbounded "
            f"budget; prepare_walks was asked for {len(walks)}")
    need = int(sum(per[:passes_wanted]) * headroom)
    if verbose:
        one = per[0]
        print(f"[coverage] one full covering pass needs {one} frames "
              f"({one/fps/60:.1f} min) at this pace"
              + (f"; {passes_wanted} passes need {sum(per[:passes_wanted])} frames "
                 f"({sum(per[:passes_wanted])/fps/3600:.2f} h)" if passes_wanted > 1 else "")
              + f"; sizing the episode to {need} frames ({need/fps/60:.1f} min), "
                f"{(headroom-1)*100:.0f}% headroom", flush=True)
    return need


def trim_to_pass(poses, labels, passes, passes_wanted):
    """Cut the episode at the frame the Nth pass completes. -> (poses, labels, passes, frames)

    "N passes, or the clock, whichever comes first" means the episode STOPS when the Nth pass
    finishes. Without this it runs to the sized budget, and the budget is N passes times the
    headroom - so on a map where N passes fit, a 25% headroom bought a 9th and 10th pass in the
    fill styles and the episode was neither N passes nor the cap. The headroom's job is to give
    the tuner room to add looking without overflowing a pass, not to add passes.

    Applied inside the tuner's scoring, not after it. Scoring the untrimmed episode and
    delivering the trimmed one put the mix out of band by the width of the discarded tail: on
    Tokyo at 2 passes the full 287588 frames scored a penalty of exactly 0, and the 201878 frames
    that were actually delivered had forward at 0.259 against a floor of 0.26. That is a rounding
    error in size and a rejected episode in consequence - `accepted` is `len(failed) == 0`, so a
    0.001 miss on one band throws away the whole recording.
    """
    if len(passes) < passes_wanted:
        return poses, labels, passes, len(poses)
    done = passes[passes_wanted - 1]
    if not done["complete_pass"]:
        return poses, labels, passes, len(poses)
    cut = int(done["to_frame"]) + 1
    if cut >= len(poses):
        return poses, labels, passes, len(poses)
    kept = [pp for pp in passes if pp["from_frame"] < cut][:passes_wanted]
    return poses[:cut], labels[:cut], kept, cut


def plan(nav, meta, task, seed, fps, duration_s, styles=("survey", "inspect", "patrol"),
         iters=18, verbose=True, prebuilt=None, z_at=None):
    """Plan a coverage episode and tune the action mix into its target bands.

    The tuner is a coordinate descent over four multipliers. It exists because hand-chosen
    constants gave 76% forward and 4% looking - which is the very bias this family was added to
    remove - and because "a reasonable proportion" is only meaningful if it is measured against
    a stated band and the achieved value is reported.
    """
    rng = np.random.default_rng(seed)
    # A task may name its own style rotation and its own target bands: a strolling episode wants a
    # slower rotation and more of its frames spent looking than a brisk one, and both are
    # properties of the task rather than of this module.
    styles = tuple(task.get("styles") or styles)
    fill_styles = tuple(task.get("fill_styles") or ("study", "inspect"))
    target = {k: tuple(v) for k, v in (task.get("action_mix_target") or TARGET_MIX).items()}
    budget = int(round(fps * duration_s)) if duration_s else None
    # `prebuilt` is (graph, report, region) from build_network, usually after the caller has
    # pruned roads this camera cannot fit through. Passing it in keeps the expensive thinning and
    # the engine-side clearance sweeps out of the tuner's inner loop.
    G, rep, region = prebuilt if prebuilt is not None else build_network(nav)
    # `z_at` maps xy to the surface the poses sit on. The caller passes one built from downward
    # traces when it has an engine: the navmesh is not the collision surface, and placing poses on
    # the navmesh puts the body capsule inside terrain that is up to a third of a metre higher.
    # Without one this falls back to the navmesh height, which is right for a flat map and is what
    # this module can compute on its own.
    if z_at is None:
        z_at = make_z_at(nav, region)

    eye_cm = float(task["camera"]["eye_height_m"]) * 100.0
    pitch_limit = float(task["camera"]["pitch_limit_deg"])
    pitch_limit_up = float(task["camera"].get("pitch_limit_up_deg", pitch_limit))
    yaw_lo, yaw_hi = {"slow": (15.0, 30.0), "medium": (30.0, 60.0),
                      "fast": (60.0, 90.0)}[task["yaw_tier"]]
    yaw_rate = float(rng.uniform(yaw_lo, yaw_hi))
    # A task may pin the rate instead of drawing it from the tier. The draw above still happens
    # so the rest of the seed's random stream (walk order, style choice) is unchanged by pinning.
    if task.get("yaw_deg_per_s") is not None:
        yaw_rate = float(task["yaw_deg_per_s"])
        if not (yaw_lo <= yaw_rate <= yaw_hi):
            print(f"[coverage] note: yaw_deg_per_s {yaw_rate:.1f} is outside the declared "
                  f"{task['yaw_tier']} tier {yaw_lo:.0f}-{yaw_hi:.0f}; the pinned value is used")
    turn_profile = task.get("turn_profile", "cosine")
    # Turn round, walk back, turn round again - a cadence the task states, not a probability.
    retrace = dict(RETRACE, **(task.get("retrace") or {}))
    # A non-positive median switches the behaviour off. It used to be read off max_interval_s,
    # which a task overriding only the median leaves at its default - so "median 0" meant a
    # target of zero and a retrace at EVERY chunk, 24 of them in half an hour.
    if float(retrace.get("median_interval_s") or 0) <= 0:
        retrace = None
    if turn_profile not in TURN_PROFILES:
        raise RuntimeError(f"task turn_profile must be one of {TURN_PROFILES}, got {turn_profile!r}")
    spd_lo, spd_hi = {"slow": (0.3, 0.6), "medium": (0.6, 1.0),
                      "fast": (1.0, 1.5)}[task["speed_tier"]]
    # A task may pin the walking speed instead of letting each style draw one inside the tier,
    # the same way yaw_deg_per_s pins the rotation rate. The tier stays declared: the
    # speed_within_tier gate still judges the poses against it.
    speed_pin = float(task["speed_m_s"]) if task.get("speed_m_s") is not None else None
    if speed_pin is not None and not (spd_lo <= speed_pin <= spd_hi):
        print(f"[coverage] note: speed_m_s {speed_pin:.2f} is outside the declared "
              f"{task['speed_tier']} tier {spd_lo}-{spd_hi} m/s; the pinned value is used")
    speed_profile = task.get("speed_profile", "trapezoid")
    if speed_profile not in SPEED_PROFILES:
        raise RuntimeError(f"task speed_profile must be one of {SPEED_PROFILES}, got {speed_profile!r}")

    # How many times over the map, and the wall-clock ceiling that overrides it. Whichever is
    # reached first ends the episode: a random covering walk re-walks roads it has already seen
    # before it clears the last few, so on a large core the ceiling is what binds, and on a small
    # one the pass count is.
    passes_wanted = int(task.get("target_passes", 1))
    max_frames = (int(round(fps * float(task["max_duration_s"])))
                  if task.get("max_duration_s") else None)
    walks = prepare_walks(G, int(rng.integers(1 << 30)), count=max(8, passes_wanted))
    base_knobs = {k: 1.0 for k in KNOBS}
    base_knobs["_tier"] = (spd_lo, spd_hi)
    base_knobs["_pin"] = speed_pin
    # The knobs the tuner may move. Pace is a lever only when it is not pinned.
    tunable = tuple(k for k in KNOBS if not (k == "speed" and speed_pin is not None))
    length_bound = "duration_s" if budget else None

    # What each pass costs, measured once. Both sizing questions - how long is one pass, how
    # long are `passes_wanted` of them - are answered from this single scripting run, and the
    # one-pass figure is also the proxy budget the tuner is scored on further down. Skipped for
    # a short preset duration, where nothing needs it and it would be a full extra scripting.
    per_pass = None
    if (not budget) or budget > 200_000:
        per_pass = measure_passes(G, rep, walks, styles, base_knobs, fps, eye_cm, pitch_limit,
                                  yaw_rate, z_at, fill_styles, pitch_limit_up=pitch_limit_up, turn_profile=turn_profile,
                                  speed_profile=speed_profile)
    one_pass_frames = per_pass[0] if per_pass else (budget or 0)

    # duration_s falsy means "however long the requested number of passes takes at this pace"
    if not budget:
        # Headroom is the tuner's room to manoeuvre. Sizing happens with every knob at 1.0, and
        # the tuner then moves them - adding looks makes the covering pass need MORE frames than
        # it was sized for, which trips the incomplete-pass penalty and forces the tuner back.
        # A strolling episode wants more of that room than a brisk one.
        if len(per_pass) < passes_wanted:
            raise RuntimeError(
                f"only {len(per_pass)} of the {passes_wanted} requested covering passes "
                f"completed against an unbounded budget; prepare_walks made {len(walks)}")
        headroom = float(task.get("size_headroom", 1.04))
        budget = int(sum(per_pass[:passes_wanted]) * headroom)
        length_bound = f"target_passes={passes_wanted}"
        if verbose:
            print(f"[coverage] one covering pass needs {one_pass_frames} frames "
                  f"({one_pass_frames/fps/60:.1f} min); {passes_wanted} passes need "
                  f"{sum(per_pass[:passes_wanted])} frames "
                  f"({sum(per_pass[:passes_wanted])/fps/3600:.2f} h), sized to {budget} with "
                  f"{(headroom-1)*100:.0f}% headroom", flush=True)
        if max_frames and budget > max_frames:
            if verbose:
                print(f"[coverage] {passes_wanted} passes want {budget} frames "
                      f"({budget/fps/3600:.2f} h); max_duration_s caps the episode at "
                      f"{max_frames} frames ({max_frames/fps/3600:.2f} h) - the cap binds",
                      flush=True)
            budget, length_bound = max_frames, "max_duration_s"
        elif max_frames and verbose:
            print(f"[coverage] {passes_wanted} passes want {budget} frames "
                  f"({budget/fps/3600:.2f} h), inside the {max_frames/fps/3600:.2f} h cap - "
                  f"the pass count binds", flush=True)
        duration_s = budget / fps

    feas = feasibility(walks[0]["covering_m"] + walks[0]["repositioning_m"],
                       fps, budget, (spd_lo + spd_hi) / 2.0)
    if not feas["feasible"]:
        raise RuntimeError(
            f"the covering walk is {feas['walk_m']:.0f} m, which needs "
            f"{feas['min_frames_all_forward']} frames of pure forward travel against a budget of "
            f"{budget}. No action mix makes that fit - this map is too large for "
            f"{duration_s:.0f} s. Pick a map with a shorter road network.")
    if verbose:
        print(f"[coverage] {rep['roads']} roads, {rep['centreline_m']:.0f} m centreline; "
              f"covering walk {feas['walk_m']:.0f} m needs "
              f"{feas['min_frames_all_forward']} of {budget} frames at minimum "
              f"({feas['slack_fraction']*100:.0f}% slack for actions)", flush=True)

    # The tuner is scored on a proxy budget of one covering pass, not on the whole episode. Its
    # coordinate descent evaluates up to 72 knob settings per iteration, and each evaluation
    # scripts every pose in the budget: at a 20 h budget of 1.73 M frames that is tens of
    # millions of poses per iteration and the tune never finishes. The action mix is a per-frame
    # statistic, so one pass measures it; the full-length episode is generated once, afterwards,
    # and the mix that gets REPORTED is the one measured on that - never the proxy's.
    tune_budget = budget
    if budget > 2 * one_pass_frames:
        tune_budget = one_pass_frames
        if verbose:
            print(f"[coverage] tuning the mix on a {tune_budget}-frame proxy "
                  f"({tune_budget/fps/60:.1f} min, one covering pass) and generating the full "
                  f"{budget} frames once with the result", flush=True)

    trim_passes = passes_wanted if length_bound.startswith("target_passes") else 0

    def evaluate(knobs, bud=None):
        poses, labels, passes = generate(G, rep, walks, styles, knobs, fps,
                                         bud if bud is not None else tune_budget,
                                         eye_cm, pitch_limit, yaw_rate, z_at,
                                         fill_styles=fill_styles,
                                         pitch_limit_up=pitch_limit_up, turn_profile=turn_profile,
                                         retrace=retrace, speed_profile=speed_profile)
        if trim_passes:
            poses, labels, passes, _ = trim_to_pass(poses, labels, passes, trim_passes)
        mix = mix_report(labels)
        pen, worst = mix_penalty(mix["fraction"], target)
        # Coverage is the hard requirement and the mix is the soft one. A knob setting whose
        # first pass does not finish inside the budget delivers a partly-walked map, so it is
        # penalised far beyond anything the mix bands can contribute.
        if not (passes and passes[0]["complete_pass"]):
            pen += 10.0
            worst = ("first_pass_incomplete", 1.0)
        return pen, worst, (poses, labels, passes, mix)

    knobs = dict(base_knobs)
    pen, worst, payload = evaluate(knobs)
    trace = [{"iter": 0, "knobs": {k: v for k, v in knobs.items() if not k.startswith("_")},
              "penalty": round(pen, 6),
              "worst": worst[0], "off_by": round(worst[1], 4),
              "fraction": payload[3]["fraction"]}]
    if verbose:
        print(f"[coverage] mix tune 0: penalty {pen:.5f}  worst {worst[0]} off by "
              f"{worst[1]:.3f}", flush=True)

    for it in range(1, iters + 1):
        if pen <= 1e-9:
            break
        improved = False
        for k in tunable:
            for factor in (1.3, 0.77):
                cand = dict(knobs)
                cand[k] = float(np.clip(knobs[k] * factor, 0.25, 4.0))
                if abs(cand[k] - knobs[k]) < 1e-9:
                    continue
                p2, w2, pay2 = evaluate(cand)
                if p2 < pen - 1e-9:
                    knobs, pen, worst, payload = cand, p2, w2, pay2
                    improved = True
        if not improved:
            # Pairs, because the frame budget couples the knobs and single moves stall on it.
            # Once the covering pass fills the episode there is no slack left, so the only way to
            # buy frames for one action is to spend fewer on another - and every single-knob step
            # towards that either overflows the budget (penalty +10) or pushes the action it took
            # the frames from out of its own band. Looking up and down sat at 3.4% against a 4%
            # floor for exactly this reason until scanning could come down in the same move.
            for a, b in ((x, y) for i, x in enumerate(tunable) for y in tunable[i + 1:]):
                for fa in (1.3, 0.77):
                    for fb in (1.3, 0.77):
                        cand = dict(knobs)
                        cand[a] = float(np.clip(knobs[a] * fa, 0.25, 4.0))
                        cand[b] = float(np.clip(knobs[b] * fb, 0.25, 4.0))
                        p2, w2, pay2 = evaluate(cand)
                        if p2 < pen - 1e-9:
                            knobs, pen, worst, payload = cand, p2, w2, pay2
                            improved = True
                if improved:
                    break
        trace.append({"iter": it,
                      "knobs": {k: v for k, v in knobs.items() if not k.startswith("_")},
                      "penalty": round(pen, 6),
                      "worst": worst[0], "off_by": round(worst[1], 4),
                      "fraction": payload[3]["fraction"]})
        if verbose:
            print(f"[coverage] mix tune {it}: penalty {pen:.5f}  worst {worst[0]} off by "
                  f"{worst[1]:.3f}  knobs " +
                  " ".join(f"{a}={knobs[a]:.2f}" for a in tunable), flush=True)
        if not improved:
            break

    if tune_budget != budget:
        # The knobs are tuned; this is the episode. Everything reported from here - the achieved
        # mix, whether it is in band, the per-pass table - comes from these poses.
        if verbose:
            print(f"[coverage] generating {budget} frames with the tuned knobs", flush=True)
        pen, worst, payload = evaluate(knobs, bud=budget)
        if verbose:
            print(f"[coverage] full-length mix: penalty {pen:.5f}  worst {worst[0]} off by "
                  f"{worst[1]:.3f}", flush=True)
        trace.append({"iter": "full_length", "knobs": {k: v for k, v in knobs.items()
                                                       if not k.startswith("_")},
                      "penalty": round(pen, 6), "worst": worst[0],
                      "off_by": round(worst[1], 4), "fraction": payload[3]["fraction"]})

        # A proxy one pass long cannot see the blend of one styled pass with N-1 fill passes, so
        # the full-length mix can land just outside a band the proxy had inside it. One round of
        # single-knob moves AT FULL LENGTH, and only when it is needed: 12 evaluations of a 5-14 h
        # episode is minutes, the pair loop would be hours, and `accepted` turns on this.
        if pen > 1e-9:
            if verbose:
                print(f"[coverage] refining at full length (single-knob moves only)", flush=True)
            for k in tunable:
                for factor in (1.15, 0.87):
                    cand = dict(knobs)
                    cand[k] = float(np.clip(knobs[k] * factor, 0.25, 4.0))
                    if abs(cand[k] - knobs[k]) < 1e-9:
                        continue
                    p2, w2, pay2 = evaluate(cand, bud=budget)
                    if p2 < pen - 1e-9:
                        knobs, pen, worst, payload = cand, p2, w2, pay2
                        if verbose:
                            print(f"[coverage]   {k} x{factor}: penalty {pen:.5f}  worst "
                                  f"{worst[0]} off by {worst[1]:.3f}", flush=True)
                    if pen <= 1e-9:
                        break
                if pen <= 1e-9:
                    break
            trace.append({"iter": "full_length_refined",
                          "knobs": {k: v for k, v in knobs.items() if not k.startswith("_")},
                          "penalty": round(pen, 6), "worst": worst[0],
                          "off_by": round(worst[1], 4), "fraction": payload[3]["fraction"]})

    poses, labels, passes = payload[0], payload[1], payload[2]
    # A trimmed episode is legitimately shorter than its budget - that is the point of the trim.
    if not trim_passes and len(poses) < budget:
        raise RuntimeError(f"only {len(poses)} poses for a {budget}-frame budget: the map's "
                           f"road network is too short for {duration_s:.0f} s even repeated "
                           f"{len(walks)} times")

    # evaluate() already trimmed, so these poses ARE the episode; budget follows them.
    if trim_passes and len(poses) < budget:
        if verbose:
            print(f"[coverage] pass {passes_wanted} completes at frame {len(poses) - 1}; the "
                  f"episode is {len(poses)} frames ({len(poses)/fps/3600:.2f} h), not the "
                  f"{budget}-frame sized budget", flush=True)
        budget = len(poses)
        duration_s = budget / fps
    # The same two bounds the acceptance gates apply, checked before anything is written.
    P = np.array([[p["x_cm"], p["y_cm"]] for p in poses])
    step = np.linalg.norm(np.diff(P, axis=0), axis=1)
    dyaw = np.abs((np.diff([p["yaw_deg"] for p in poses]) + 540.0) % 360.0 - 180.0)
    yaw_limit = yaw_rate * 1.5
    if len(step) and step.max() >= 50.0:
        raise RuntimeError(f"max per-frame translation {step.max():.1f} cm reaches the "
                           f"frame_translation_bounded limit of 50 cm")
    if len(dyaw) and dyaw.max() * fps > yaw_limit:
        raise RuntimeError(f"max per-frame yaw rate {dyaw.max()*fps:.1f} deg/s exceeds the "
                           f"frame_rotation_bounded limit of {yaw_limit:.1f} deg/s "
                           f"({yaw_rate:.1f} deg/s tier x 1.5)")

    mix = mix_report(labels)
    cov = measure_coverage(G, poses)
    if verbose:
        print(f"[coverage] measured from the poses: {cov['roads_walked']}/{cov['roads_total']} "
              f"roads, {cov['centreline_walked_m']:.0f}/{cov['centreline_m']:.0f} m", flush=True)
    return {
        "poses": poses,
        "labels": labels,
        "network": rep,
        "passes": passes,
        "feasibility": feas,
        "speed_m_per_s": speed_pin if speed_pin is not None else float((spd_lo + spd_hi) / 2.0),
        "speed_pinned": speed_pin is not None,
        "speed_profile": speed_profile,
        "yaw_deg_per_s": yaw_rate,
        "yaw_rate_pinned": task.get("yaw_deg_per_s") is not None,
        # Looking up and down uses the same rate and profile as turning: _pitch_to shares
        # _rotation_profile with turn(). Reported so it is a stated fact, not an assumption.
        "pitch_deg_per_s": yaw_rate,
        "turn_profile": turn_profile,
        "eye_height_cm": eye_cm,
        "pitch_limits_deg": {"up": pitch_limit_up, "down": pitch_limit},
        "action_mix": mix,
        "action_mix_target": {k: list(v) for k, v in target.items()},
        "action_mix_in_band": bool(mix_penalty(mix["fraction"], target)[0] <= 1e-9),
        "retrace": retrace_report(passes, len(poses), fps, retrace),
        "styles": {"covering": list(styles), "fill": list(fill_styles)},
        "mix_tuning": {"knobs": {k: v for k, v in knobs.items() if not k.startswith("_")},
                       "speed_tier_m_s": [spd_lo, spd_hi],
                       "iterations": len(trace) - 1, "trace": trace},
        "start_xy_cm": [poses[0]["x_cm"], poses[0]["y_cm"]],
        "coverage": cov,
        "continuity": {
            "max_frame_translation_cm": round(float(step.max()) if len(step) else 0.0, 2),
            "p99_frame_translation_cm": round(float(np.percentile(step, 99)) if len(step) else 0.0, 2),
            "max_frame_yaw_rate_deg_s": round(float(dyaw.max() * fps) if len(dyaw) else 0.0, 1),
            "yaw_rate_limit_deg_s": round(yaw_limit, 1),
            "limit_cm": MAX_FRAME_STEP_CM,
        },
        "passes_summary": {
            "passes_started": len(passes),
            "complete_passes": sum(1 for p in passes if p["complete_pass"]),
            "first_pass_complete": bool(passes and passes[0]["complete_pass"]),
            "passes_requested": passes_wanted,
            "frames_per_pass_measured": per_pass,
        },
        # Which of the two limits actually ended the episode. A consumer that sees 20.00 h and a
        # pass count of 6 should be able to tell "the clock ran out" from "the map was covered",
        # without inferring it from the numbers.
        "length": {
            "frames": len(poses),
            "duration_s": round(len(poses) / fps, 3),
            "bound_by": length_bound,
            "target_passes": passes_wanted,
            "max_duration_s": (float(task["max_duration_s"])
                               if task.get("max_duration_s") else None),
            "tuned_on_frames": tune_budget,
            "tuned_on_note": ("the action mix was tuned on a one-pass proxy and the reported mix "
                              "was measured on the full-length episode"
                              if tune_budget != budget else
                              "the action mix was tuned on the episode itself"),
        },
        "core_confined": bool(task.get("confine_to_core", False)),
        "_graph": G,
    }


def preview(out_png, G, poses, rep):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 9), dpi=110)
    for _, _, d in G.edges(data=True):
        p = d["poly"]
        ax.plot(p[:, 0] / 100, p[:, 1] / 100, "-", color="#cbd5e1", lw=3, zorder=1)
    xy = np.array([[p["x_cm"], p["y_cm"]] for p in poses]) / 100
    lab = [p["phase"] for p in poses]
    ax.plot(xy[:, 0], xy[:, 1], "-", color="#2563eb", lw=0.8, alpha=0.85, zorder=2)
    back = np.array([i for i, l in enumerate(lab) if l == "backward"])
    if len(back):
        ax.plot(xy[back, 0], xy[back, 1], ".", color="#dc2626", ms=2.0, zorder=3,
                label="backward")
    ax.plot(xy[0, 0], xy[0, 1], "o", color="#16a34a", ms=9, zorder=4, label="start")
    ax.set_aspect("equal")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title(f"coverage walk - {rep['roads']} roads, {rep['centreline_m']:.0f} m of centreline")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("json")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--fps", type=float, default=24.0)
    ap.add_argument("--preview")
    a = ap.parse_args()
    nav, meta = pnm.load(a.bin, a.json)
    task = {"camera": {"eye_height_m": 1.7, "pitch_limit_deg": 30.0, "roll_limit_deg": 5.0},
            "speed_tier": "fast", "yaw_tier": "medium"}
    r = plan(nav, meta, task, a.seed, a.fps, a.duration)
    G = r.pop("_graph")
    drop = ("poses", "labels")
    r2 = {k: v for k, v in r.items() if k not in drop}
    r2["mix_tuning"] = {k: v for k, v in r2["mix_tuning"].items() if k != "trace"}
    print(json.dumps(r2, indent=1))
    if a.preview:
        preview(a.preview, G, r["poses"], r["network"])
        print(f"preview -> {a.preview}")


if __name__ == "__main__":
    main()
