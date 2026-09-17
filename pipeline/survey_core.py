#!/usr/bin/env python3
"""Rank maps by the size of their built-up CORE, not by their walkable area.

A purchased demo level is content sitting on a large flat apron. The apron is walkable, it is
usually the biggest walkable region on the map, and planning a coverage walk on "the biggest
region" therefore spends the episode outside the content. On Tokyo the existing survey picked an
8174 m2 empty apron over the 2151 m2 street network that has every building in it, and the
finished episode - 145950 frames, 101 minutes - walks the map's outskirts.

Core = INTERIOR walkable surface with content standing on it. Two tests, and a cell has to pass
both:

  1. local obstacle density - the fraction of a 12 m disc around the cell that is obstacle. Content
     appears on the navmesh as the space the walkable surface does not cover inside the map
     footprint: buildings, walls, props.
  2. content on opposite sides - of 8 pairs of opposing directions, at least one pair has an
     obstacle within 25 m on BOTH sides.

Density alone measures "near content", which is not the same as "inside content", and the
difference is exactly the map's outskirts. A strip along a boundary wall, or a path round the rim
of an empty field, has content on one side and nothing on the other: it passes the density test and
fails the pair test. It removed 88% of ModularNeighborhood's core (a band beside one wall), 93% of
Grass_Hills' (a couple of props in a field) and 77% of AsianTemple's (a path along the edge), while
leaving 97% of Tokyo's street network and 80% of Downtown_West's - the 20% being its fringe where
the blocks open onto the apron.

Measured PER NAVMESH REGION, and the region with the largest core wins. Not a union of regions
within a height band: Downtown_West has a coarse 10784 m2 plane at z = -270 that runs underneath
the whole level, buildings included, and unioning it with the 8401 m2 street level at z = 0 fills
in every building footprint - obstacles vanish, and a city block map reports a 200 m2 core. A
region is one continuous surface an agent can walk without leaving it, which is exactly the thing
whose core we want to measure. Stacked surfaces cannot dilute each other if they are never mixed.
"""
import json
import sys
from pathlib import Path

import numpy as np
import cv2
from scipy import ndimage

# The deployment, not this file's directory. Both copies of this script - the one that runs and
# the one in the source-of-record repo - must read the same navmesh cache, and a __file__-relative
# path makes the repo copy quietly find no maps at all instead of saying so.
PIPE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
sys.path.insert(0, str(PIPE))
import plan_navmesh as pnm                              # noqa: E402

CELL = 25.0
MIN_CLEAR = 90.0      # body radius 40 cm + margin, as in survey_coverage
CLOSE_R_M = 15.0      # closes the walkable surface into a footprint, bridging streets
DENS_R_M = 12.0       # street-scale neighbourhood
DENS_MIN = 0.06       # obstacle fraction inside that disc
ENCL_R_M = 25.0       # how far to look for content on the other side of you
PAIRS_MIN = 1         # of 8 opposing pairs, how many must have content on both sides
TOP_REGIONS = 3       # how many regions to run the enclosure test on, best density core first
MIN_OBST_M2 = 2.0     # a lamp post is content; one stray cell is raster noise
MIN_REGION_M2 = 20.0


def _disc(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return ((x * x + y * y) <= r * r).astype(np.uint8)


_DENS_K = None
_RAY_K = {}


def ray_kernels(r_cells, n_dirs=16):
    """One kernel per direction: a single ray from the centre outwards. cv2.dilate reflects the
    kernel, so each direction comes back as its opposite - which does not matter here, because all
    n_dirs are used and they come in opposing pairs."""
    ks, size = [], 2 * r_cells + 1
    t = np.arange(1, r_cells + 1)
    for i in range(n_dirs):
        th = 2 * np.pi * i / n_dirs
        k = np.zeros((size, size), np.uint8)
        k[np.round(r_cells + t * np.sin(th)).astype(int),
          np.round(r_cells + t * np.cos(th)).astype(int)] = 1
        ks.append(k)
    return ks


def opposing_pairs(obst, n_dirs=16):
    """How many of the n_dirs/2 opposing direction pairs have an obstacle within ENCL_R_M on both
    sides. This is the test for being INSIDE content rather than beside it.

    Counting covered directions without pairing them does not work: a cell two metres from a long
    boundary wall has a wide fan of directions landing on that wall, so it scores as high as a
    street does. Nothing is on the other side, and the pair count says so.
    """
    r = int(round(ENCL_R_M * 100 / CELL))
    if r not in _RAY_K:
        _RAY_K[r] = ray_kernels(r, n_dirs)
    hits = [cv2.dilate(obst.astype(np.uint8), k) > 0 for k in _RAY_K[r]]
    half = n_dirs // 2
    pairs = np.zeros(obst.shape, np.uint8)
    for i in range(half):
        pairs += (hits[i] & hits[i + half])
    return pairs


def core_of(g):
    """One rasterised region -> (clearance, safe, obstacle, core)."""
    global _DENS_K
    clear = ndimage.distance_transform_edt(g) * CELL
    safe = clear >= MIN_CLEAR
    # Pad before closing, crop after. cv2's morphology treats out-of-image as filled during the
    # erosion half of a close, so a close done in place leaves the dilation's full 15 m band along
    # every edge of the raster - a phantom wall around the export box. It reads as content: 388 of
    # ModularNeighborhood's 435 m2 of "obstacle" was this frame, and the 503 m2 core it produced
    # was a strip pinned between a real wall and the phantom one.
    r = int(CLOSE_R_M * 100 / CELL)
    gp = np.pad(g, r)
    foot = cv2.morphologyEx(gp, cv2.MORPH_CLOSE, _disc(r))[r:-r, r:-r]
    obst = ndimage.binary_fill_holes(foot > 0) & (g == 0)
    if obst.any():
        lab, n = ndimage.label(obst)
        sz = ndimage.sum(obst, lab, range(1, n + 1)) * CELL ** 2 / 1e4
        k = np.zeros(n + 1, bool)
        k[1:] = sz >= MIN_OBST_M2
        obst = k[lab]
    if _DENS_K is None:
        d = _disc(int(round(DENS_R_M * 100 / CELL))).astype(np.float32)
        _DENS_K = d / d.sum()
    dens = cv2.filter2D(obst.astype(np.float32), -1, _DENS_K,
                        borderType=cv2.BORDER_CONSTANT)
    return clear, safe, obst, safe & (dens >= DENS_MIN)


def largest(m):
    if not m.any():
        return m
    lab, n = ndimage.label(m)
    sz = ndimage.sum(m, lab, range(1, n + 1))
    return lab == int(np.argmax(sz)) + 1


def grid_for(nav):
    """One raster for the whole export: origin, width, height.

    Every region is rasterised on THIS grid, not on its own extent, so a mask made here can be
    handed to another module and still line up cell for cell. coverage.py needs exactly that -
    the roads it walks have to lie inside the core this file measured, and two grids with two
    origins would shift one against the other by an arbitrary offset.
    """
    xy = nav.verts[:, :2]
    lo, hi = xy.min(0), xy.max(0)
    W = int(np.ceil((hi[0] - lo[0]) / CELL)) + 4
    H = int(np.ceil((hi[1] - lo[1]) / CELL)) + 4
    if W * H > 40_000_000:
        raise RuntimeError(f"grid {W}x{H} too large")
    return lo, H, W


def region_raster(nav, reg, lo, H, W):
    g = np.zeros((H, W), np.uint8)
    poly = np.round((nav.verts[nav.wtris[reg]][:, :, :2] - lo) / CELL).astype(np.int32) + 2
    cv2.fillPoly(g, [p for p in poly], 1)
    return g


def best_core_region(nav, verbose=False):
    """The region with the largest built-up interior, and that interior as a mask.

    Every region gets the density test, which is cheap. Only the most promising few get the
    enclosure test, which is 16 dilations with a 50 m kernel: on a map with 149 regions that
    would otherwise be most of the runtime, and a region whose density core is negligible cannot
    win on its interior.
    """
    lo, H, W = grid_for(nav)
    a = lambda m: float(np.asarray(m).sum()) * CELL ** 2 / 1e4
    cand, per_region = [], []
    for reg in sorted(nav.regions, key=lambda r: -float(nav.tri_areas(r).sum())):
        area = float(nav.tri_areas(reg).sum()) / 1e4
        if area < MIN_REGION_M2:
            continue
        g = region_raster(nav, reg, lo, H, W)
        clear, safe, obst, dcore = core_of(g)
        rec = dict(region_area_m2=round(area, 1), safe_m2=round(a(safe), 1),
                   obstacle_m2=round(a(obst), 1), density_core_m2=round(a(largest(dcore)), 1),
                   median_z_cm=round(float(np.median(nav.verts[nav.wtris[reg]][:, :, 2])), 1))
        per_region.append(rec)
        cand.append((rec, reg, g, clear, safe, obst, dcore))
    if not cand:
        raise RuntimeError("no region above the area floor")

    best = None
    for rec, reg, g, clear, safe, obst, dcore in sorted(
            cand, key=lambda c: -c[0]["density_core_m2"])[:TOP_REGIONS]:
        pairs = opposing_pairs(obst)
        core = largest(dcore & (pairs >= PAIRS_MIN))
        strict = largest(dcore & (pairs >= PAIRS_MIN + 1))
        rec["core_m2"] = round(a(core), 1)
        rec["core_strict_m2"] = round(a(strict), 1)
        if best is None or rec["core_m2"] > best[0]["core_m2"]:
            best = (rec, reg, g, clear, safe, obst, core, strict)
    rec, reg, g, clear, safe, obst, core, strict = best
    if verbose:
        print(f"[core] chose a {rec['region_area_m2']:.0f} m2 region at z "
              f"{rec['median_z_cm']:.0f} cm; core {rec['core_m2']:.0f} m2 of its "
              f"{rec['safe_m2']:.0f} m2 walkable", flush=True)
    return {"region": reg, "core": core, "strict": strict, "walkable": g, "safe": safe,
            "obstacle": obst, "clearance_cm": clear, "lo": lo, "shape": (H, W),
            "cell_cm": CELL, "stats": rec, "per_region": per_region}


def measure(slug, navdir, png=None):
    nav, meta = pnm.load(str(navdir / f"{slug}.bin"), str(navdir / f"{slug}.json"))
    a = lambda m: float(np.asarray(m).sum()) * CELL ** 2 / 1e4
    sel = best_core_region(nav)
    rec, per_region = sel["stats"], sel["per_region"]
    g, clear, safe, obst = sel["walkable"], sel["clearance_cm"], sel["safe"], sel["obstacle"]
    core, strict = sel["core"], sel["strict"]
    H, W = sel["shape"]

    # How close the core comes to the edge of what was exported. The nav box is 120 m of half
    # extent around one start point, so a core that runs up to the box edge is a lower bound on
    # that map's core rather than a measurement of it.
    if core.any():
        rs, cs = np.where(core)
        margin = min(rs.min(), H - 1 - rs.max(), cs.min(), W - 1 - cs.max()) * CELL / 100
    else:
        margin = -1.0

    # What kind of core it is, from the clearance already to hand: a street network reads a few
    # metres, a plaza reads fifteen. This replaced a skeleton and a covering tour. The tour does
    # not belong here - a core is a blob, not a corridor network, and ChemicalPlant's core skeleton
    # is 9336 nodes with 5864 of odd degree, on which route inspection's all-pairs shortest paths
    # and maximum weight matching do not finish - and the skeleton was the only reason this file
    # needed anything from survey_coverage.
    cc = clear[core] if core.any() else np.zeros(1, np.float32)

    out = {
        "map": slug,
        "core_m2": rec["core_m2"],
        "core_strict_m2": rec["core_strict_m2"],
        "core_density_only_m2": rec["density_core_m2"],
        "region_walkable_m2": rec["region_area_m2"],
        "region_safe_m2": rec["safe_m2"],
        "obstacle_m2": rec["obstacle_m2"],
        "core_frac_of_region_safe": round(rec["core_m2"] / max(rec["safe_m2"], 1e-9), 3),
        "core_clearance_median_m": round(float(np.median(cc)) / 100, 2),
        "core_clearance_p90_m": round(float(np.percentile(cc, 90)) / 100, 2),
        "core_margin_to_export_bounds_m": round(float(margin), 2),
        "export_extent_m": [round(W * CELL / 100, 1), round(H * CELL / 100, 1)],
        "chosen_region_median_z_cm": rec["median_z_cm"],
        "regions_measured": len(per_region),
        "nav_regions": len(nav.regions),
        "per_region": per_region[:8],
    }
    if png:
        img = np.zeros((H, W, 3), np.uint8)
        img[g > 0] = (45, 45, 45)
        img[safe] = (115, 115, 115)
        img[core] = (90, 230, 90)
        img[obst] = (0, 110, 220)
        cv2.imwrite(str(png), cv2.flip(img, 0))
    return out


def main():
    navdir = PIPE / "frozen" / "nav"
    if not navdir.is_dir():
        raise SystemExit(f"no navmesh cache at {navdir}. Exports are written there by freeze.py; "
                         f"this survey reads them and never launches the editor itself.")
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else PIPE / "_core_survey")
    (outdir / "png").mkdir(parents=True, exist_ok=True)
    rows = []
    for nb in sorted(navdir.glob("*.bin")):
        if not nb.with_suffix(".json").exists():
            continue
        try:
            r = measure(nb.stem, navdir, outdir / "png" / f"{nb.stem}.png")
        except Exception as e:
            r = {"map": nb.stem, "error": f"{type(e).__name__}: {e}"}
        rows.append(r)
        print(json.dumps({k: v for k, v in r.items() if k != "per_region"}), flush=True)
    (outdir / "core_survey.json").write_text(json.dumps(rows, indent=1))
    print("wrote", outdir / "core_survey.json")


if __name__ == "__main__":
    main()
