#!/usr/bin/env python3
"""Plan a route on the exported navmesh, instead of guessing bearings from a spawn point.

Why this replaces the bearing fan. The fan swept capsules along straight lines from one start point
and took whichever direction looked clear. On flat open city geometry that worked; on five arbitrary
maps it produced, in one batch: a route into a hedge (50-58% of pixels crushed to black because the
camera was inside foliage), a route down a 1.5 m dead end (detour ratio 36.5, 2788 capsule hits), a
route across ploughed farmland where the per-frame ground trace made the camera plough through the
soil (2843 hits with 8.3 m of forward clearance), and an anchor sitting in empty sky.

Every one of those is the same mistake: asking "is this direction clear" instead of "where can this
agent actually walk". The navmesh answers the second question directly, and we already export it.

Three properties this gives that the fan could not:

  1. **Positions come from the walkable surface**, so z is the surface height. No downward traces,
     so no per-frame kerb discontinuity to smooth away, and no way to end up in the sky.
  2. **Boundary clearance**. A hedge, a fence, a wall and a drop all appear as navmesh boundary
     edges. Requiring the route to stay a set distance from every boundary is what keeps the camera
     out of vegetation - which is the actual cause of the black frames, not the tonemapper.
  3. **A straight leg is verified surface by surface.** Sampling the segment and requiring every
     sample to land inside a triangle means "walkable in a straight line", which is what
     out-and-back needs, without trusting a trace channel that does not block pawns anyway.

    python3 plan_navmesh.py <navmesh.bin> <navmesh.json> [--leg1 1000 --leg2 2400]
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

WELD_EPS_CM = 1.0
SAMPLE_STEP_CM = 30.0


class NavMesh:
    """Exported triangles plus the connectivity Recast does not export.

    Vertices are welded first: the export repeats positions per tile, so untouched indices make
    every tile its own island and boundary edges appear along seams that are not boundaries.
    """

    def __init__(self, verts, tris):
        self.raw_verts = verts
        self.tris = tris
        self.vmap, self.verts = self._weld(verts, WELD_EPS_CM)
        self.wtris = self.vmap[tris]

        # edge -> triangles. An edge with exactly one triangle is a boundary: the walkable surface
        # ends there, which in a building is a wall and outdoors is a hedge, fence or a drop.
        edges = {}
        for t, (a, b, c) in enumerate(self.wtris):
            for u, v in ((a, b), (b, c), (c, a)):
                edges.setdefault((min(u, v), max(u, v)), []).append(t)
        self.edges = edges
        self.boundary = [e for e, ts in edges.items() if len(ts) == 1]

        # triangle adjacency, for connected regions
        adj = [[] for _ in range(len(self.wtris))]
        for e, ts in edges.items():
            if len(ts) == 2:
                adj[ts[0]].append(ts[1])
                adj[ts[1]].append(ts[0])
        self.adj = adj
        self.regions = self._regions()

    @staticmethod
    def _weld(verts, eps):
        keys = np.round(verts / eps).astype(np.int64)
        _, first, inv = np.unique(keys, axis=0, return_index=True, return_inverse=True)
        return inv, verts[first]

    def _regions(self):
        seen = np.full(len(self.wtris), -1, np.int32)
        regions = []
        for start in range(len(self.wtris)):
            if seen[start] >= 0:
                continue
            rid = len(regions)
            stack, members = [start], []
            seen[start] = rid
            while stack:
                t = stack.pop()
                members.append(t)
                for n in self.adj[t]:
                    if seen[n] < 0:
                        seen[n] = rid
                        stack.append(n)
            regions.append(np.array(members, np.int32))
        self.tri_region = seen
        return sorted(regions, key=len, reverse=True)

    def tri_areas(self, tri_idx=None):
        t = self.wtris if tri_idx is None else self.wtris[tri_idx]
        a, b, c = self.verts[t[:, 0]], self.verts[t[:, 1]], self.verts[t[:, 2]]
        return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)

    def centroids(self, tri_idx=None):
        t = self.wtris if tri_idx is None else self.wtris[tri_idx]
        return self.verts[t].mean(axis=1)

    def boundary_points(self):
        if not self.boundary:
            return np.zeros((0, 3))
        e = np.array(self.boundary)
        return 0.5 * (self.verts[e[:, 0]] + self.verts[e[:, 1]])

    def inside_xy(self, p, tri_idx, tol=1.0):
        """Is p (xy) inside any of these triangles? Returns the surface z, or None."""
        t = self.wtris[tri_idx]
        a, b, c = self.verts[t[:, 0]], self.verts[t[:, 1]], self.verts[t[:, 2]]
        # barycentric in xy, vectorised over triangles
        v0, v1, v2 = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2], p[:2] - a[:, :2]
        den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
        ok = np.abs(den) > 1e-9
        u = np.zeros(len(t))
        v = np.zeros(len(t))
        u[ok] = (v2[ok, 0] * v1[ok, 1] - v1[ok, 0] * v2[ok, 1]) / den[ok]
        v[ok] = (v0[ok, 0] * v2[ok, 1] - v2[ok, 0] * v0[ok, 1]) / den[ok]
        hit = ok & (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6)
        if not hit.any():
            return None
        i = np.argmax(hit)
        z = a[i, 2] + u[i] * (b[i, 2] - a[i, 2]) + v[i] * (c[i, 2] - a[i, 2])
        return float(z)


def load(bin_path, json_path):
    meta = json.loads(Path(json_path).read_text())
    blob = Path(bin_path).read_bytes()
    V, T = meta["vertex_count"], meta["triangle_count"]
    expect = V * 12 + T * 3 * 4
    if len(blob) != expect:
        raise ValueError(f"{bin_path} is {len(blob)} bytes, expected {expect} for "
                         f"{V} verts and {T} tris")
    verts = np.frombuffer(blob[:V * 12], "<f4").reshape(-1, 3).astype(np.float64)
    tris = np.frombuffer(blob[V * 12:], "<i4").reshape(-1, 3)
    return NavMesh(verts, tris), meta


MAX_BEARING_RETRIES = 6   # a veto bans one heading; six attempts covers a cluster of trees


def clearance_map(nav, points, boundary_pts):
    """Distance from each point to the nearest walkable-surface boundary, in xy.

    This is the number that keeps the camera out of hedges: a point 4 m from every boundary is in
    open space, a point 40 cm from one is against something.
    """
    if len(boundary_pts) == 0:
        return np.full(len(points), np.inf)
    bp = boundary_pts[:, :2]
    out = np.empty(len(points))
    for i, p in enumerate(points[:, :2]):
        out[i] = np.sqrt(((bp - p) ** 2).sum(axis=1)).min()
    return out


def walkable_line(nav, region_tris, a, b, min_clear_cm, boundary_pts, step=SAMPLE_STEP_CM):
    """Is the straight segment a->b entirely on the walkable surface, and clear of boundaries?

    Returns (ok, worst_clearance_cm, zs). Sampling rather than exact triangle walking: at 30 cm a
    sample cannot skip a triangle in any navmesh built for a 35 cm agent radius.
    """
    d = math.dist(a[:2], b[:2])
    n = max(2, int(d / step) + 1)
    zs, worst = [], math.inf
    for k in range(n):
        f = k / (n - 1)
        p = np.array([a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, 0.0])
        z = nav.inside_xy(p, region_tris)
        if z is None:
            return False, 0.0, []
        zs.append(z)
        c = clearance_map(nav, p.reshape(1, 3), boundary_pts)[0]
        worst = min(worst, c)
        if worst < min_clear_cm:
            return False, worst, []
    return True, worst, zs


def plan(nav, meta, leg1_cm, leg2_cm, min_clear_cm=150.0, turn_lo=60.0, turn_hi=170.0,
         anchor_hint=None, verbose=True, accept=None):
    """Pick an anchor and two straight legs, all on the walkable surface.

    `accept(anchor, legs)` is the caller's veto, and it exists because the navmesh cannot answer the
    whole question. A tree whose collision is disabled - ordinary for foliage instances - cuts no
    hole in the walkable surface, so every triangle here says "walkable" and the route goes through
    the trunk. That is what happened in RussianWinterTownDemo01 at t=57.75 s: 30 frames with over
    20% of the image inside 60 cm and the nearest pixels on the 10 cm near clip plane, with all
    three collision checks reporting clean. Only the renderer sees that tree, so the veto is where
    a caller hangs a depth probe. Returning False makes the search continue to the next candidate
    instead of failing, which is how the route goes AROUND rather than the episode being refused.
    """
    region = nav.regions[0]
    area_m2 = nav.tri_areas(region).sum() / 10000.0
    cents = nav.centroids(region)
    bpts = nav.boundary_points()
    clear = clearance_map(nav, cents, bpts)
    rejected = []

    if verbose:
        print(f"[nav-plan] {len(nav.verts)} welded verts, {len(nav.tris)} tris, "
              f"{len(nav.regions)} regions; largest {len(region)} tris / {area_m2:.0f} m2")
        print(f"[nav-plan] boundary edges {len(nav.boundary)}; clearance over the region: "
              f"median {np.median(clear):.0f} cm, p90 {np.percentile(clear, 90):.0f} cm, "
              f"max {clear.max():.0f} cm")

    # Anchors are ranked by open space. A hedge is a boundary, so the most open triangle centre is
    # the least likely place to end up with a frame full of leaves.
    order = np.argsort(-clear)
    if anchor_hint is not None:
        h = np.array(anchor_hint[:2], dtype=float)
        near = np.sqrt(((cents[:, :2] - h) ** 2).sum(axis=1))
        # prefer open ground, but stay in the neighbourhood of the requested spawn
        score = clear - 0.02 * near
        order = np.argsort(-score)

    def search(anchor, banned):
        """The two best legs from this anchor, skipping bearings the caller has already vetoed."""
        found = []
        for want in (leg1_cm, leg2_cm):
            best = None
            for bearing in range(0, 360, 5):
                if float(bearing) in banned:
                    continue
                r = math.radians(bearing)
                tgt = anchor + np.array([math.cos(r) * want, math.sin(r) * want, 0.0])
                z = nav.inside_xy(tgt, region)
                if z is None:
                    continue
                tgt[2] = z
                ok, worst, _ = walkable_line(nav, region, anchor, tgt, min_clear_cm, bpts)
                if not ok:
                    continue
                if found:
                    turn = abs((bearing - found[0][0] + 540) % 360 - 180)
                    if not (turn_lo <= turn <= turn_hi):
                        continue
                if best is None or worst > best[2]:
                    best = (float(bearing), tgt, worst)
            if best is None:
                break
            found.append(best)
        return found

    for ai in order[:40]:
        anchor = cents[ai].copy()
        if clear[ai] < min_clear_cm:
            continue
        # A veto costs one HEADING, not the whole anchor. An obstacle the navmesh cannot see blocks
        # one direction out of an otherwise good standing place, and abandoning the anchor for it
        # throws away the open space that made it the best candidate. The banned set is per anchor.
        banned, found = set(), []
        for _attempt in range(MAX_BEARING_RETRIES):
            found = search(anchor, banned)
            if len(found) != 2:
                break
            legs_try = [{"bearing_deg": bg, "target": [float(v) for v in tgt],
                         "length_cm": float(math.dist(anchor[:2], tgt[:2])),
                         "min_boundary_clearance_cm": float(worst)}
                        for bg, tgt, worst in found]
            if accept is None:
                break
            verdict = accept(anchor, legs_try)
            if verdict:
                break
            reason = getattr(verdict, "reason", "vetoed by the caller")
            bad_bearing = getattr(verdict, "bearing", None)
            rejected.append({"anchor": [float(v) for v in anchor],
                             "bearings": [l["bearing_deg"] for l in legs_try],
                             "banned_bearing": bad_bearing, "reason": reason})
            if verbose:
                print(f"[nav-plan] candidate at ({anchor[0]:.0f}, {anchor[1]:.0f}) rejected: "
                      f"{reason}")
            if bad_bearing is None:
                found = []
                break
            banned.add(float(bad_bearing))
        else:
            found = []
        if len(found) == 2:
            # already vetted inside the retry loop above - accept() is not called again here
            legs_out = [{"bearing_deg": bg, "target": [float(v) for v in tgt],
                         "length_cm": float(math.dist(anchor[:2], tgt[:2])),
                         "min_boundary_clearance_cm": float(worst)}
                        for bg, tgt, worst in found]
            if verbose:
                print(f"[nav-plan] anchor ({anchor[0]:.0f}, {anchor[1]:.0f}, {anchor[2]:.0f}) "
                      f"with {clear[ai]:.0f} cm of open space around it")
                for i, (bg, tgt, worst) in enumerate(found):
                    print(f"[nav-plan]   leg {i+1}: {bg:.0f} deg, "
                          f"{math.dist(anchor[:2], tgt[:2])/100:.1f} m, "
                          f"narrowest point {worst:.0f} cm from a boundary")
            return {
                "ok": True,
                "anchor": [float(v) for v in anchor],
                "anchor_clearance_cm": float(clear[ai]),
                "legs": legs_out,
                "candidates_rejected": rejected,
                "region_area_m2": float(area_m2),
                "region_triangles": int(len(region)),
                "regions_total": len(nav.regions),
                "boundary_edges": len(nav.boundary),
                "agent_radius_cm": meta.get("agent_radius_cm"),
                "agent_height_cm": meta.get("agent_height_cm"),
                "method": "anchor and both legs taken from the exported navmesh: every sampled "
                          "point lies inside a walkable triangle and is at least "
                          f"{min_clear_cm:.0f} cm from any boundary edge. Boundary edges are where "
                          "walkable space ends - a wall indoors, a hedge or a drop outdoors - which "
                          "is why this keeps the camera out of foliage.",
            }

    return {"ok": False,
            "candidates_rejected": rejected,
            "error": f"no anchor in the largest region ({area_m2:.0f} m2) has {min_clear_cm:.0f} cm "
                     f"of clearance and two legs of {leg1_cm/100:.1f} m and {leg2_cm/100:.1f} m "
                     f"that stay on the walkable surface",
            "region_area_m2": float(area_m2),
            "max_clearance_cm": float(clear.max()) if len(clear) else 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("json")
    ap.add_argument("--leg1", type=float, default=1000.0)
    ap.add_argument("--leg2", type=float, default=2400.0)
    ap.add_argument("--clear", type=float, default=150.0)
    a = ap.parse_args()
    nav, meta = load(a.bin, a.json)
    r = plan(nav, meta, a.leg1, a.leg2, min_clear_cm=a.clear)
    print(json.dumps({k: v for k, v in r.items() if k != "method"}, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
