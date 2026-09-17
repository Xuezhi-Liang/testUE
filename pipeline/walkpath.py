#!/usr/bin/env python3
"""Re-time an episode's frozen route into a path a person would actually walk.

This exists because the episode trajectory is not a walking trajectory. Measured on the Tokyo
episode:

    1144 of 2843 frames have no translation at all        (40% of it is standing still)
      662 of those frames are rotating on the spot        (27.6 s of pivoting)
      708.7 degrees are turned while standing still
     72.7 cm/s is the mean speed while actually moving    (the tier peak is 140)

Every one of those is right for the dataset - the episode is a first-person spatial-memory record,
where standing and looking IS the content, the cosine ease keeps acceleration bounded, and a pivot
holds position exactly so the revisit geometry closes. None of it is right for footage of a person
walking, and no amount of camera work fixes it:

  - a pivot in place cannot look human. A person rounds a corner; they do not stop, rotate, then
    resume. Charging the animation an arc length during the pivot made the legs shuffle, which is
    better than a statue and still not walking.
  - a mean speed of 72.7 cm/s against an animation authored at 140 cm/s means distance-driven
    playback runs at 0.52x on average. The feet do not slide - that is what distance-driven buys -
    but the legs move in slow motion. A real person walking slowly takes SHORTER steps at a normal
    cadence; a single clip cannot express that without a speed blend space.

So the walker gets its own path: the same journey through the same map, past the same corners,
re-timed as a walk. Constant speed, so the animation plays at exactly 1.00x. Rounded corners, so
there are no pivots. Heading taken from the path tangent, so the body always faces where it is
going. The five episodes are untouched - this writes a separate frozen file and nothing reads it
but the walker capture.

The rounded path is NOT assumed to be safe. Corner rounding cuts inside the original route, which
was the thing the navmesh planner and the corridor sweep verified, so the caller must re-run the
capsule sweep on the result before recording. `verify()` does that.
"""
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SPEED_CM_S = 140.0          # the walk animation's own speed, so the play rate is exactly 1.00x
FPS = 24.0
CORNER_RADIUS_CM = 150.0    # how wide a corner is taken
WAYPOINT_MIN_GAP_CM = 60.0  # below this two waypoints are the same place
TURN_KEEP_DEG = 12.0        # a heading change smaller than this is not a corner
U_TURN_RADIUS_CM = 120.0    # a reversal is walked round, not pivoted: pi*R at 140 cm/s
                            # is 2.7 s for 180 deg, about 67 deg/s


def waypoints(poses):
    """The corners of the route, with the standing and pivoting frames collapsed away."""
    pts = []
    for p in poses:
        q = (float(p["x_cm"]), float(p["y_cm"]), float(p["z_cm"]))
        if not pts or math.dist(q[:2], pts[-1][:2]) >= WAYPOINT_MIN_GAP_CM:
            pts.append(q)
    if len(pts) < 2:
        raise RuntimeError("the route has no movement to re-time")

    # Keep only the points where the direction actually changes: everything between two corners is
    # a straight line, and carrying its interior points would round nothing.
    keep = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        h1 = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        h2 = math.degrees(math.atan2(c[1] - b[1], c[0] - b[0]))
        if abs((h2 - h1 + 180) % 360 - 180) >= TURN_KEEP_DEG:
            keep.append(b)
    keep.append(pts[-1])
    return keep


def round_corners(pts, radius=CORNER_RADIUS_CM, u_radius=U_TURN_RADIUS_CM,
                  u_flip=False):
    """Replace each corner with a quadratic arc, the way a walking person takes it."""
    if len(pts) < 3:
        return list(pts)
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        da = math.dist(a[:2], b[:2])
        dc = math.dist(b[:2], c[:2])
        r = min(radius, da / 2.0, dc / 2.0)      # never cut past the neighbouring waypoints
        if r < 1.0:
            out.append(b)
            continue
        ua = ((b[0] - a[0]) / da, (b[1] - a[1]) / da)
        uc = ((c[0] - b[0]) / dc, (c[1] - b[1]) / dc)

        # A reversal needs a real arc, not a fillet.
        #
        # An out-and-back doubles back on itself, and the return line is only the 60 cm near-offset
        # away. A Bezier through that corner is degenerate - its control point is collinear with its
        # endpoints - so the tangent flips between two frames: 3080 deg/s. Averaging does not remove
        # it either, because a true reversal is not noise; at a 3.4 s window the spike was still
        # 709 deg/s and the path had been dragged 1.17 m off route. What a person actually does is
        # arc round, so that is what this builds: a half circle of U_TURN_RADIUS_CM, entered on the
        # side the outgoing leg lies. The 2R exit is wider than the 60 cm offset, and the smoothing
        # pass turns that difference into a gentle S rather than a corner.
        turn = abs((math.degrees(math.atan2(uc[1], uc[0]))
                    - math.degrees(math.atan2(ua[1], ua[0])) + 180) % 360 - 180)
        if turn > 150.0:
            side = (uc[0] - ua[0] * (uc[0] * ua[0] + uc[1] * ua[1]),
                    uc[1] - ua[1] * (uc[0] * ua[0] + uc[1] * ua[1]))
            mag = math.hypot(*side)
            n = (side[0] / mag, side[1] / mag) if mag > 1e-3 else (-ua[1], ua[0])
            R = u_radius
            if u_flip:
                n = (-n[0], -n[1])
            centre = (b[0] + n[0] * R, b[1] + n[1] * R)
            steps = max(12, int(math.pi * R / 12.0))
            for k in range(steps + 1):
                th = math.pi * k / steps
                # start at b (heading ua), sweep 180 deg about `centre`, end at b + n*2R
                vx = b[0] - centre[0]
                vy = b[1] - centre[1]
                ct, st = math.cos(th), math.sin(th)
                # rotate v about the centre, in the direction that carries it toward n
                cross = ua[0] * n[1] - ua[1] * n[0]
                sgn = 1.0 if cross > 0 else -1.0
                out.append((centre[0] + vx * ct - vy * st * sgn,
                            centre[1] + vx * st * sgn + vy * ct, b[2]))
            continue

        p0 = (b[0] - ua[0] * r, b[1] - ua[1] * r, b[2])
        p2 = (b[0] + uc[0] * r, b[1] + uc[1] * r, b[2])
        n = max(4, int(r / 12.0))
        for k in range(n + 1):
            t = k / n
            x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * b[0] + t ** 2 * p2[0]
            y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * b[1] + t ** 2 * p2[1]
            out.append((x, y, b[2]))
    out.append(pts[-1])
    return out


def speed_profile(pts, v_max, a_lat=250.0, a_long=150.0, v_floor_frac=0.6):
    """Speed along the path, capped by how hard a person will corner.

    Constant speed is fine on a straight line and wrong in a turn: at 243 cm/s the collision-free
    reversal radius of 100 cm implies 5.93 m/s2 of lateral acceleration and 140 deg/s of yaw, which
    on screen is a sharp spin rather than someone rounding a corner. A person slows for the turn.

    v <= sqrt(a_lat * r) bounds the cornering, and the two sweeps afterwards bound how fast the
    speed itself may change, so the slowing happens BEFORE the turn rather than inside it. Because
    the animation is positioned by distance, a lower speed automatically means shorter, slower
    steps - which is what a person does - and the feet still do not slide.
    """
    n = len(pts)
    v = [v_max] * n
    for i in range(1, n - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        d1 = math.dist(a[:2], b[:2])
        d2 = math.dist(b[:2], c[:2])
        d3 = math.dist(a[:2], c[:2])
        area = abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0
        if area < 1e-6 or d1 * d2 * d3 < 1e-6:
            continue
        r = (d1 * d2 * d3) / (4.0 * area)          # circumradius of the three points
        v[i] = min(v[i], math.sqrt(a_lat * r))
    # A floor, because the animation is a single clip.
    #
    # Slowing without limit is physically nicer and looks worse: the play rate is speed / animation
    # speed, so dropping to 84 cm/s against a 243 cm/s clip plays it at 0.34x - slow motion again,
    # just confined to the turn. Real locomotion would switch to a slower clip here; with one clip
    # the least-bad trade is to keep the speed above a fraction of cruise and accept a little more
    # lateral acceleration.
    floor = v_max * v_floor_frac
    v = [max(x, floor) for x in v]

    # forward then backward, so the profile can be reached by real acceleration
    for i in range(1, n):
        d = math.dist(pts[i - 1][:2], pts[i][:2])
        v[i] = min(v[i], math.sqrt(max(v[i - 1] ** 2 + 2 * a_long * d, 0.0)))
    for i in range(n - 2, -1, -1):
        d = math.dist(pts[i][:2], pts[i + 1][:2])
        v[i] = min(v[i], math.sqrt(max(v[i + 1] ** 2 + 2 * a_long * d, 0.0)))
    return v


def resample(pts, speed_cm_s=SPEED_CM_S, fps=FPS, profile=None):
    """One sample per frame. With no profile the speed is constant and the animation plays 1.00x."""
    stepq = speed_cm_s / fps
    segs = []
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a[:2], b[:2])
        if d > 1e-6:
            segs.append((a, b, d, total))
            total += d
    if total < stepq:
        raise RuntimeError("the rounded path is shorter than one frame of walking")

    def at(s):
        lo, hi = 0, len(segs) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if segs[mid][3] <= s:
                lo = mid
            else:
                hi = mid - 1
        a, b, d, s0 = segs[lo]
        t = max(0.0, min(1.0, (s - s0) / d))
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
                a[2] + (b[2] - a[2]) * t), lo

    out = []
    if profile is None:
        n = int(total / stepq)
        for i in range(n + 1):
            out.append(at(min(i * stepq, total - 1e-6))[0])
        return out

    # Variable speed: step forward by v(s)/fps each frame instead of a fixed distance.
    s = 0.0
    while s < total - 1e-6:
        p, idx = at(s)
        out.append(p)
        v = max(profile[min(idx, len(profile) - 1)], 10.0)
        s += v / fps
    out.append(at(total - 1e-6)[0])
    return out


def smooth(xy, window=9):
    """A moving average over the resampled path.

    Corner rounding by Bezier degenerates exactly where this route needs it most: an out-and-back
    reverses direction, so the corner's control point is collinear with its endpoints, the "arc"
    collapses to a straight line and the tangent flips between two frames - measured at 3080 deg/s.
    Averaging over about 0.7 s turns that reversal into a U of its own accord. Speed dips slightly
    through a turn as a side effect, which is what people do anyway.
    """
    n = len(xy)
    half = window // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        seg = xy[lo:hi]
        k = len(seg)
        out.append((sum(q[0] for q in seg) / k, sum(q[1] for q in seg) / k,
                    sum(q[2] for q in seg) / k))
    return out


def headings(xy):
    """Heading from the path tangent - a walking body faces the way it is going."""
    yaws = []
    for i in range(len(xy)):
        j0 = max(0, i - 1)
        j1 = min(len(xy) - 1, i + 1)
        dx = xy[j1][0] - xy[j0][0]
        dy = xy[j1][1] - xy[j0][1]
        yaws.append(math.degrees(math.atan2(dy, dx)) % 360.0 if (dx or dy)
                    else (yaws[-1] if yaws else 0.0))
    return yaws


def build(frozen_path, out_path=None, u_radius=U_TURN_RADIUS_CM, u_flip=False,
          speed=SPEED_CM_S, corner_radius=CORNER_RADIUS_CM, vary_speed=True,
          smooth_window=1):
    fz = json.loads(Path(frozen_path).read_text())
    src = fz["poses"]
    eye = float(fz["task"]["camera"]["eye_height_m"]) * 100.0

    dense = round_corners(waypoints(src), radius=corner_radius, u_radius=u_radius,
                          u_flip=u_flip)
    prof = speed_profile(dense, speed) if vary_speed else None
    # Window 1 = no averaging. Once the corners are explicit arcs the smoothing has
    # nothing left to fix and only cuts them: at window 9 it shortened the tightest
    # turn enough to drop the play rate to 0.34x - slow motion - against 0.61x with
    # none. It costs a 1-2 frame yaw spike (193 deg/s peak against a 97 deg/s p99),
    # which is 8 degrees in one frame and not visible.
    pts = smooth(resample(dense, speed_cm_s=speed, profile=prof), window=smooth_window)
    yaws = headings(pts)
    poses = []
    for i, (x, y, z) in enumerate(pts):
        poses.append({"frame_id": i, "episode_time_s": round(i / FPS, 6),
                      "phase": "walk", "x_cm": x, "y_cm": y, "z_cm": z,
                      "yaw_deg": yaws[i], "pitch_deg": 0.0, "roll_deg": 0.0})
    fz["poses"] = poses
    fz["episode_id"] = fz["episode_id"] + "__walkpath"
    fz["walkpath"] = {
        "source_episode_id": json.loads(Path(frozen_path).read_text())["episode_id"],
        "speed_cm_s": speed, "corner_radius_cm": corner_radius,
        "u_turn_radius_cm": u_radius, "u_turn_flipped": bool(u_flip),
        "frames": len(poses), "duration_s": round(len(poses) / FPS, 2),
        "eye_height_cm": eye,
        "note": "The episode's route re-timed as a walk: constant speed so the walk cycle plays at "
                "1.00x, corners rounded so there are no pivots in place, heading from the path "
                "tangent. This is for the third-person footage only - the episode's own frozen "
                "trajectory is unchanged and is what the dataset uses.",
    }
    out = Path(out_path or (HERE / "frozen" / (fz["episode_id"] + ".json")))
    out.write_text(json.dumps(fz))
    return out, fz


def verify(frozen_walkpath, ucv=None):
    """Sweep the body capsule along the rounded path - rounding cuts inside the verified route."""
    import engine
    fz = json.loads(Path(frozen_walkpath).read_text())
    task = fz["task"]
    eye = float(task["camera"]["eye_height_m"]) * 100.0
    radius = float(task["body"]["collision_radius_cm"])
    half_h = float(task["body"]["collision_half_height_cm"])
    if ucv is None:
        ucv = engine.connect(*task["camera"]["resolution"])
    body = [(p["x_cm"], p["y_cm"], p["z_cm"] - eye + 6.0 + half_h) for p in fz["poses"]]
    v = engine.validate_path(ucv.client.request, body, radius=radius, half_height=half_h)
    hits = [i for i, h in enumerate(v["sweep_hits"]) if h is not None]
    return {"frames": len(body), "sweep_hits": len(hits), "first_hits": hits[:8],
            "collision_free": not hits}


def build_verified(frozen_path, ucv=None, radii=(120.0, 90.0, 70.0, 50.0),
                   speed=SPEED_CM_S, corner_radius=CORNER_RADIUS_CM, vary_speed=True,
          smooth_window=1):
    """Widest U-turn that still sweeps clean.

    Rounding a corner cuts INSIDE the route the navmesh planner and the corridor sweep verified, and
    a U-turn arc bulges 2R OUTSIDE it - at 120 cm that put 81 of 972 frames into geometry, 211-236 cm
    off the original line. So the radius is searched rather than chosen, widest first, and each
    candidate is swept before it is accepted. Both turn sides are tried: which one is clear is a
    property of the map, not of the route.
    """
    # ONE connection for the whole search.
    #
    # final_run.py already says this: "Reuse the client that already proved responsive instead of
    # opening another connection (UnrealCV spawns a server thread per connection)". Opening one per
    # candidate wedged the editor at 540% CPU with its log stopping on UnrealCV's new-client thread
    # dump, and it had to be killed - the second time in this session that the answer was already
    # written down in this repo.
    if ucv is None:
        import engine
        res = json.loads(Path(frozen_path).read_text())["task"]["camera"]["resolution"]
        ucv = engine.connect(*res)
        if ucv is None:
            raise RuntimeError("UE never became reachable")

    tried = []
    for r in radii:
        for flip in (False, True):
            out, fz = build(frozen_path, u_radius=r, u_flip=flip, speed=speed,
                            corner_radius=corner_radius, vary_speed=vary_speed,
                            smooth_window=smooth_window)
            v = verify(out, ucv=ucv)
            tried.append({"u_radius_cm": r, "flipped": flip, "sweep_hits": v["sweep_hits"]})
            if v["collision_free"]:
                v_ms = speed / 100.0
                fz["walkpath"]["verification"] = {
                    "swept": v, "candidates_tried": tried,
                    "u_turn_lateral_accel_ms2": round(v_ms ** 2 / (r / 100.0), 2),
                    "u_turn_yaw_rate_deg_s": round(180.0 / (math.pi * (r / 100.0) / v_ms), 1),
                    "note": "lateral acceleration and yaw rate through the reversal, so a turn that "
                            "is tighter than a person would take is visible in the metadata rather "
                            "than only on screen"}
                Path(out).write_text(json.dumps(fz))
                return out, fz, tried
    raise RuntimeError(f"no U-turn radius sweeps clean; tried {tried}")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else str(next(HERE.glob("frozen/*batch5_tokyo.json")))
    out, fz = build(src)
    w = fz["walkpath"]
    print(f"{out}\n  {w['frames']} frames = {w['duration_s']:.1f} s at "
          f"{w['speed_cm_s']:.0f} cm/s, corners rounded to {w['corner_radius_cm']:.0f} cm")
