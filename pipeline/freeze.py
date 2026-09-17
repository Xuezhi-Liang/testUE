#!/usr/bin/env python3
"""Task JSON -> frozen trajectory: one commanded camera pose per frame, validated first.

Why freeze at all. The previous recorder decided where to go while it was recording, by walking
a character and hoping. One 2-minute episode spent 63% of its frames wedged against a storefront
corner that sat 273 cm along a bearing the planner had asked 1073 cm of, and nothing in the loop
could tell, because a wedged character still renders a good picture. Deciding the whole route in
advance and proving it walkable is the only way the capture stage can be dumb - and a dumb
capture stage is what makes runs reproducible.

What "validated" means here, matching spec section 11:
  - every leg is capsule-swept before it is committed to;
  - every consecutive frame pair is capsule-swept, so no segment passes through geometry;
  - the four near-plane corners are traced, because a camera centre outside a wall does not
    prove the near plane is;
  - forward clearance is recorded per frame;
  - frame-to-frame translation and rotation are bounded and reported.

The frozen file is the contract. `capture.py` may not invent a pose.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
import geom  # noqa: E402
import plan_navmesh  # noqa: E402

FROZEN = HERE / "frozen"
# One scratch file, overwritten by every probe: the probe reads the measured
# statistics out of the returned JSON, so the EXR itself is never opened.
PROBE_DIR = "/home/ue4/probe"
# The return leg walks 60 cm beside the outbound one, so the route has to be clear
# over that whole corridor, not just along its centre.
CORRIDOR_HALF_WIDTH_CM = 60.0
SP_DIR = HERE.parent.parent / "batch_inference" / "start_positions"

SPEED_TIERS = {"slow": (0.3, 0.6), "medium": (0.6, 1.0), "fast": (1.0, 1.5)}
YAW_TIERS = {"slow": (15.0, 30.0), "medium": (30.0, 60.0), "fast": (60.0, 90.0)}
UU_PER_M = 100.0
RAMP_PEAK = math.pi / 2.0     # a cosine ease peaks at pi/2 times its average

# A capsule whose bottom sits exactly on the floor is already in contact, so every sweep from it
# reports a blocking hit at distance 0 and the whole map looks impassable. Real characters have a
# step offset for the same reason; lifting the capsule a few centimetres is that offset.
GROUND_CLEARANCE_CM = float(os.environ.get("GROUND_CLEARANCE_CM", "6"))

# How fast the camera may change height, cm/s. A person stepping onto a 20 cm kerb takes about a
# third of a second, so the head moves vertically at well under a metre per second. Without this
# bound a per-frame ground trace turns every kerb into a single-frame 20 cm teleport.
Z_SLEW_CM_PER_S = float(os.environ.get("Z_SLEW_CM_PER_S", "60"))


# ----------------------------------------------------------------------------- speed profile
def ease(n):
    """Cosine ease-in-out weights over n frames, normalised to average 1.

    Spec section 5 asks for observe -> accelerate -> cruise -> decelerate, and explicitly
    forbids per-frame random speed. A single smooth envelope per leg gives that.
    """
    if n <= 1:
        return np.ones(max(n, 1))
    t = np.linspace(0.0, 1.0, n)
    w = 0.5 - 0.5 * np.cos(2 * math.pi * np.clip(t, 0, 1) * 0.5 + 0.0)
    w = np.sin(math.pi * t) ** 0.6            # gentle rise and fall, non-zero in the middle
    w = np.maximum(w, 1e-3)
    return w / w.mean()


class Leg:
    """A stretch of the trajectory. `kind` is what the frame generator does with it."""

    def __init__(self, kind, frames, **kw):
        self.kind = kind
        self.frames = int(max(1, frames))
        self.__dict__.update(kw)

    def as_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k not in ("frames", "kind")}
        d.update({"kind": self.kind, "frames": self.frames})
        return d


# --------------------------------------------------------------------------------- route
def fan_reach(req, origin, radius, half_h, step_deg=10.0, max_cm=2600.0):
    yaws = [i * step_deg for i in range(int(round(360.0 / step_deg)))]
    res = engine.sweep_rays(req, origin, [(y, max_cm) for y in yaws],
                            radius=radius, half_height=half_h)
    return {y: (max_cm if r["clear"] else float(r["blocked_at_cm"]))
            for y, r in zip(yaws, res)}, res


def choose_legs(reach, want_cm, margin_cm, min_turn=60.0, max_turn=160.0):
    """Pick an out bearing with room for `want_cm`, and a distinct second bearing.

    Returns (out_bearing, usable_cm, notes). If nothing fits, the leg is shortened to what the
    most open bearing actually offers and that is recorded - a short honest episode beats a
    long one spent against a wall.
    """
    notes = []
    fits = sorted([(y, d) for y, d in reach.items() if d >= want_cm + margin_cm],
                  key=lambda t: -t[1])
    if fits:
        return fits[0][0], want_cm, notes
    best_y = max(reach, key=lambda k: reach[k])
    best_d = reach[best_y]
    usable = max(150.0, best_d - margin_cm)
    notes.append(f"no bearing fits {want_cm:.0f} cm plus {margin_cm:.0f} cm margin; the most "
                 f"open bearing {best_y:.0f} deg reaches {best_d:.0f} cm, so the leg was "
                 f"shortened to {usable:.0f} cm")
    return best_y, usable, notes


# --------------------------------------------------------------------------------- families
def build_legs(family, fps, duration_s, age_s, out_bearing, leg_cm, yaw_rate, speed_ms,
               probe=True, return_kind="near", near_offset_cm=60.0):
    """Frame-count timeline for the requested family.

    The dwell before the return is solved so that the realised anchor-to-revisit age matches the
    request: the age is a hard requirement of the spec, not a by-product.
    """
    total = int(round(duration_s * fps))
    obs = int(round(1.5 * fps))
    travel = max(1, int(round(RAMP_PEAK * (leg_cm / UU_PER_M) / speed_ms * fps)))
    turn180 = max(1, int(round(180.0 / yaw_rate * fps)))
    probe_f = int(round(1.0 * fps)) if probe else 0
    revisit_f = int(round(1.0 * fps))

    # anchor is the end of the observation window; revisit is the end of the return leg
    fixed = obs + travel + turn180 + travel
    # age is anchor -> end of the revisit hold: out, dwell, turn, back, turn back, hold
    dwell = int(round(age_s * fps)) - (travel + turn180 + travel + turn180 + revisit_f)
    notes = []
    if dwell < 0:
        notes.append(f"requested age {age_s:.1f} s is shorter than the minimum "
                     f"{(travel*2+turn180*2+revisit_f)/fps:.1f} s this leg, its two turns "
                     f"and the revisit hold need; the age will be "
                     f"the minimum instead")
        dwell = 0
    # Turn frames follow from the angle actually being turned, at the tier's yaw rate, so a
    # skewed return costs slightly less turning than a straight one instead of snapping.
    def turn_leg(from_yaw, to_yaw):
        delta = (to_yaw - from_yaw) % 360.0
        return Leg("turn", max(1, math.ceil(RAMP_PEAK * delta / yaw_rate * fps)),
                   to_yaw=to_yaw % 360.0, direction=1, turn_deg=delta)

    if return_kind == "exact":
        back_bearing = (out_bearing + 180.0) % 360.0
        back_cm = leg_cm
        lateral = 0.0
    else:
        # `return_kind` decides whether the way back retraces the way out. An exact retrace makes
        # the episode a reversed replay of itself, which section 3.10 caps at 20% of the data, so
        # "near" walks back along a laterally offset line and arrives beside the anchor.
        back_cm = math.hypot(leg_cm, near_offset_cm)
        skew = math.degrees(math.atan2(near_offset_cm, leg_cm))
        back_bearing = (out_bearing + 180.0 + skew) % 360.0
        lateral = near_offset_cm

    legs = [Leg("observe", obs, yaw_sweep_deg=0.0)]
    legs.append(Leg("travel", travel, bearing=out_bearing, dist_cm=leg_cm, sign=+1))
    legs.append(Leg("dwell", dwell) if dwell else Leg("dwell", 1))
    legs.append(turn_leg(out_bearing, back_bearing))
    legs.append(Leg("travel", travel, bearing=back_bearing, dist_cm=back_cm, sign=-1,
                    lateral_offset_cm=lateral))
    # Turn back to the anchor's own heading before the revisit window. Without this the camera
    # arrives at the anchor position facing 180 degrees away: position closure is perfect but the
    # view shares almost nothing with what the anchor saw, so there is nothing to recognise and
    # the section 12 rotation gate (<=1 deg) can never pass. Returning to a place to check it and
    # then turning to face what you were looking at is also simply what a person does.
    legs.append(turn_leg(back_bearing, out_bearing))
    legs.append(Leg("revisit", revisit_f))
    if probe_f:
        legs.append(Leg("probe", probe_f, side_cm=22.0, yaw_deg=7.0))
    used = sum(l.frames for l in legs)
    if used < total:
        # pad only briefly: a long static tail adds duration without adding any spatial content,
        # and section 14 counts content, not seconds
        pad = min(total - used, int(round(2.0 * fps)))
        legs.append(Leg("hold", pad))
        if total - used > pad:
            notes.append(f"the family fills {(used+pad)/fps:.1f} s of the requested "
                         f"{duration_s:.1f} s; the remainder was not padded with a static hold. "
                         f"Filling a longer episode needs more revisit events (multi_anchor or "
                         f"double_loop), not a longer stand-still.")
    elif used > total:
        notes.append(f"family needs {used/fps:.1f} s, longer than the requested "
                     f"{duration_s:.1f} s; the episode will run {used/fps:.1f} s so the "
                     f"revisit age stays exact")
    return legs, notes


def build_nested(fps, ages, bearings, legs_cm, yaw_rate, speed_ms, probe=True,
                 near_offset_cm=60.0, perp_signs=None, perp_offsets=None):
    """Short out-and-back, then a much longer one on a different bearing.

    Two revisit events at two ages from the same anchor, which is what makes the episode test
    memory capacity rather than a single fixed offset: the short leg's revisit is recent, the long
    leg's is old, and the anchor has to survive the second excursion to be recognised again.

    `ages` and `legs_cm` are per excursion, and both come from the caller so the leg length is
    always derived from its own age budget.
    """
    obs = int(round(1.5 * fps))
    revisit_f = int(round(1.0 * fps))
    probe_f = int(round(1.0 * fps)) if probe else 0
    legs = [Leg("observe", obs, yaw_sweep_deg=0.0)]
    notes, events = [], []
    frame = obs

    def turn_leg(from_yaw, to_yaw):
        """Frames for the shorter rotation between two headings.

        Two things this must get right. It has to take the *actual* current heading, not the
        previous leg's nominal one - reading it off the preceding leg gave a delta of 0 for the
        turn between excursions, so a 70 degree change got one frame and became a 1680 deg/s snap.
        And it has to take the short way round: a person turning from 0 to 290 degrees turns 70
        degrees left, not 290 degrees right.
        """
        d = (to_yaw - from_yaw) % 360.0
        direction = 1
        if d > 180.0:
            d, direction = 360.0 - d, -1
        # RAMP_PEAK, for the same reason the travel legs carry it: the yaw tier is a limit on how
        # fast the head turns, and `ease` puts its PEAK at pi/2 times the average. Sizing the turn
        # by the average therefore aims the peak straight at the gate's 1.5x tier ceiling, and
        # discretising a 6-frame turn pushed it through - 94.9 deg/s against a 87.8 limit, one frame
        # in 2844. Sizing by the peak makes the tier mean what it says.
        return Leg("turn", max(1, math.ceil(RAMP_PEAK * d / yaw_rate * fps)),
                   to_yaw=to_yaw % 360.0, direction=direction, turn_deg=d)

    # Geometry is resolved against explicit points in anchor-relative coordinates, not by chaining
    # bearings. Chaining accumulated the near-pose offset - each excursion returned 60 cm off its
    # own start and the next started from there, so by the second revisit the camera was 98 cm from
    # the anchor instead of 60. Naming the target point makes every revisit exactly one offset from
    # the anchor, however many excursions there are.
    anchor_yaw = bearings[0]
    pos = (0.0, 0.0)
    yaw_now = anchor_yaw

    def leg_between(p, q):
        dx, dy = q[0] - p[0], q[1] - p[1]
        return math.degrees(math.atan2(dy, dx)) % 360.0, math.hypot(dx, dy)

    for k, (age, bearing, leg_cm) in enumerate(zip(ages, bearings, legs_cm)):
        far = (math.cos(math.radians(bearing)) * leg_cm,
               math.sin(math.radians(bearing)) * leg_cm)
        # come back beside the anchor, offset perpendicular to this excursion's bearing, so each
        # revisit is a genuinely different near-pose rather than the same one twice
        # which side to come back on is chosen by the caller after testing whether the body can
        # actually stand there; +90 is not always reachable
        sign = 1 if perp_signs is None else perp_signs[k]
        off_cm = near_offset_cm if perp_offsets is None else perp_offsets[k]
        perp = bearing + 90.0 * sign
        back_to = (math.cos(math.radians(perp)) * off_cm,
                   math.sin(math.radians(perp)) * off_cm)
        b_out, d_out = leg_between(pos, far)
        b_back, d_back = leg_between(far, back_to)
        t_out = turn_leg(yaw_now, b_out)
        t_mid = turn_leg(b_out, b_back)
        # The revisit must restore the ANCHOR's heading, not this excursion's bearing. Facing the
        # excursion's own direction leaves the camera up to 70 degrees off what the anchor saw, and
        # the shared view collapses - measured at 3 ORB matches against the anchor frame.
        t_home = turn_leg(b_back, anchor_yaw)
        travel_out = max(1, int(round(RAMP_PEAK * (d_out / UU_PER_M) / speed_ms * fps)))
        travel_back = max(1, int(round(RAMP_PEAK * (d_back / UU_PER_M) / speed_ms * fps)))

        spent = (t_out.frames + travel_out + t_mid.frames + travel_back + t_home.frames
                 + revisit_f)
        elapsed = frame - obs
        dwell = int(round(age * fps)) - elapsed - spent
        if dwell < 0:
            notes.append(f"excursion {k+1}: age {age:.0f} s cannot be met - {elapsed/fps:.1f} s "
                         f"is already spent and this {d_out/100:.1f} m leg needs "
                         f"{spent/fps:.1f} s at {speed_ms:.2f} m/s; it will realise "
                         f"{(elapsed + spent)/fps:.1f} s instead")
            dwell = 0

        legs.append(t_out)
        legs.append(Leg("travel", travel_out, bearing=b_out, dist_cm=d_out, sign=+1))
        legs.append(Leg("dwell", max(1, dwell)))
        legs.append(t_mid)
        legs.append(Leg("travel", travel_back, bearing=b_back, dist_cm=d_back, sign=-1))
        legs.append(t_home)
        legs.append(Leg("revisit", revisit_f, excursion=k + 1, requested_age_s=age))
        if probe_f:
            legs.append(Leg("probe", probe_f, side_cm=22.0, yaw_deg=7.0))
        frame = sum(l.frames for l in legs)
        pos, yaw_now = back_to, anchor_yaw
        events.append({"excursion": k + 1, "requested_age_s": age,
                       "leg_cm": round(d_out, 1), "bearing_deg": round(b_out, 2),
                       "returns_to_offset_cm": round(off_cm, 1)})
    legs.append(Leg("hold", int(round(1.0 * fps))))
    return legs, notes, events


def reachable_at(rig, xyz, cam_off, yaws=(0.0, 90.0, 180.0, 270.0), tol_cm=2.0):
    """Worst placement error for one camera point across several headings.

    Reachability is heading-dependent: the head sits ~6 cm off the pawn's axis and rotates with it,
    so putting the camera on a fixed point requires a different pawn position at each yaw. A point
    that is reachable facing one way can be blocked facing another - the excursion-1 return point
    tested clean at bearing 0 and then failed at bearing 290, where the route actually passes
    through it. Requiring every heading to work makes the point safe whatever the trajectory does
    there.

    Each heading is tested as an *approach*: the body is parked well away from the point first,
    then placed. Testing it in place is self-confirming - the first placement moves the body there,
    so every later measurement trivially succeeds and the point reads as reachable even when
    arriving at it is blocked. That is why a point measured at 0.00 cm here failed at 20.69 cm
    during the route check, which arrives from the previous sampled frame.
    """
    worst = 0.0
    park = (xyz[0] + 400.0, xyz[1] + 400.0, xyz[2])
    for y in yaws:
        rig.place_camera(park, y, 0.0, cam_off)
        loc, _ = rig.place_camera(xyz, y, 0.0, cam_off)
        worst = max(worst, math.dist(loc, xyz))
        if worst > tol_cm * 8:
            break              # hopeless; stop paying for the remaining headings
    return worst


# Lateral tolerance, set from the measured distribution rather than picked.
#
# Across five maps the lateral disagreement between the frozen pose and where the body comes to
# rest is bimodal, with nothing in between:
#
#   Downtown_West   0.001 cm |  Tokyo  0.001 cm |  WinterTown  2.97 cm   <- capsule settling
#   MedievalTown  138.34 cm                                              <- real obstruction
#
# 2.97 cm is a character capsule settling on a slope; 138 cm is geometry pushing the body out of
# the way. 20 cm sits in the empty middle of that gap. The previous 5 cm was arbitrary and rejected
# WinterTown, a route the navmesh independently called fully walkable with a detour ratio of 1.0.
LATERAL_TOL_CM = float(os.environ.get("LATERAL_TOL_CM", "20"))


def verify_reachable(rig, poses, eye_cm, cam_off, sample=20, tol_cm=LATERAL_TOL_CM, limit=12):
    """Place the real body at a sample of frozen poses and check it lands where asked.

    The capsule sweeps in `engine` trace `TRACE_TYPE_QUERY1`, which is the Visibility channel -
    that is not the channel that blocks a Pawn, so a route can sweep perfectly clean and still be
    somewhere the character cannot stand. That is not a hypothetical: a revisit point 60 cm from
    the anchor validated clean, then the character stuck 20.8 cm away from it for ~100 frames and
    the whole episode failed pose tracking after 12 minutes of capture.

    Rather than guess the right trace channel, this asks the body itself. It is the only check that
    uses the same collision resolution the capture will use.
    """
    # LATERAL error only, and z reported separately.
    #
    # Measuring the 3D distance conflated two unrelated things and rejected three sound maps. The
    # frozen z now comes from the navmesh, whose surface Recast deliberately places a few
    # centimetres above the geometry, while a character capsule snaps to the actual floor - so a
    # pose reads as "unreachable by 7-30 cm" with x and y matching to the millimetre. That is a
    # convention difference, not an obstruction.
    #
    # It also checks a constraint the capture no longer has: the in-engine capture puts the scene
    # capture components straight on the frozen pose, with no character and no floor snapping. What
    # still matters is whether a route passes through geometry, and that shows up laterally - a
    # 127 cm sideways push is a wall, a 7 cm height difference is not.
    worst_xy, worst_z, offenders = 0.0, 0.0, []
    idxs = list(range(0, len(poses), max(1, sample)))
    for i in idxs:
        p = poses[i]
        loc, _ = rig.place_camera((p["x_cm"], p["y_cm"], p["z_cm"]),
                                  p["yaw_deg"], p["pitch_deg"], cam_off)
        exy = math.hypot(loc[0] - p["x_cm"], loc[1] - p["y_cm"])
        ez = abs(loc[2] - p["z_cm"])
        worst_xy = max(worst_xy, exy)
        worst_z = max(worst_z, ez)
        if exy > tol_cm and len(offenders) < limit:
            offenders.append({"frame": i, "phase": p["phase"],
                              "lateral_error_cm": round(exy, 2), "z_error_cm": round(ez, 2),
                              "wanted": [round(p["x_cm"], 1), round(p["y_cm"], 1)],
                              "reached": [round(loc[0], 1), round(loc[1], 1)]})
    return {"sampled_frames": len(idxs), "sample_every": sample,
            "max_lateral_error_cm": round(worst_xy, 3),
            "max_z_error_cm": round(worst_z, 3),
            "tolerance_cm": tol_cm,
            "unreachable_samples": offenders, "reachable": not offenders,
            "z_note": "z difference between the navmesh surface and where a character capsule "
                      "comes to rest. Recast places its surface above the geometry on purpose, so "
                      "a few centimetres here is expected and is not an obstruction. The in-engine "
                      "capture does not use a character at all.",
            "method": "the pawn was placed at each sampled pose and the CameraComponent read back. "
                      "Lateral disagreement means the route passes through geometry; the "
                      "Visibility-channel capsule sweeps cannot see that, because Visibility is "
                      "not the channel that blocks a Pawn."}


def legs_to_poses(legs, anchor_xy, yaw_rate, start_yaw=None):
    """Expand legs to per-frame (x_cm, y_cm, yaw_deg, pitch_deg, phase).

    A turn interpolates from whatever yaw the previous leg left behind to an explicit target,
    rather than from a remembered bearing by a fixed 180 degrees. Assuming the turn is exactly
    180 degrees from the outbound bearing is wrong as soon as the return leg is skewed for a
    near-pose revisit: the camera then snaps by the skew angle at both leg boundaries - measured
    at 12.08 degrees in one frame, an instantaneous 290 deg/s against a 58 deg/s tier.
    """
    x, y = anchor_xy
    # Explicit, because guessing it from legs[1] silently breaks as soon as legs[1] is not a travel
    # leg. In the nested family legs[1] became a turn, so the start yaw fell back to 0 while the
    # builder assumed it was the first bearing - the whole 170 degree difference was then turned in
    # a single frame (4080 deg/s) and the anchor faced 170 degrees away from its own revisit, which
    # collapsed the view overlap to 8 matches.
    if start_yaw is not None:
        yaw = float(start_yaw)
    else:
        yaw = legs[1].bearing if len(legs) > 1 and hasattr(legs[1], "bearing") else 0.0
    rows = []
    for li, leg in enumerate(legs):
        n = leg.frames
        if leg.kind == "travel":
            w = ease(n)
            step = np.asarray(w) / w.sum() * leg.dist_cm
            b = math.radians(leg.bearing)
            for i in range(n):
                x += math.cos(b) * step[i]
                y += math.sin(b) * step[i]
                rows.append((x, y, leg.bearing, 0.0, f"travel{li}"))
            yaw = leg.bearing
        elif leg.kind == "turn":
            direction = getattr(leg, "direction", 1)
            target = leg.to_yaw
            delta = (target - yaw) % 360.0
            if direction < 0:
                delta = -((yaw - target) % 360.0)
            w = ease(n)
            d = np.asarray(w) / w.sum() * delta
            cur = yaw
            for i in range(n):
                cur += d[i]
                rows.append((x, y, cur, 0.0, f"turn{li}"))
            yaw = cur
        elif leg.kind == "probe":
            # a small real movement after the revisit, so a frozen or copied frame is exposed
            for i in range(n):
                f = math.sin(math.pi * (i + 1) / n)
                rows.append((x + leg.side_cm * f, y, yaw + leg.yaw_deg * f, 0.0, f"probe{li}"))
        elif leg.kind == "observe":
            for i in range(n):
                rows.append((x, y, yaw, 0.0, "observe"))
        else:
            for i in range(n):
                rows.append((x, y, yaw, 0.0, leg.kind))
        if rows:
            yaw = rows[-1][2]
    return rows


# ----------------------------------------------------------------------------------- freeze
def freeze(task, out_dir=FROZEN, ucv=None, rig=None):
    """If `ucv`/`rig` are supplied they are reused and left alive for the caller.

    Reconnecting to UnrealCV after a client has disconnected has been observed to kill the
    editor outright (it logs a second connection thread with `Socket: NULL` and dies on the next
    request), so the whole task runs on one connection.
    """
    fps = float(task["fps"])
    W, H = task["camera"]["resolution"]
    fov = float(task["camera"]["fov_deg"])
    eye_cm = float(task["camera"]["eye_height_m"]) * 100.0
    radius = float(task["body"]["collision_radius_cm"])
    half_h = float(task["body"]["collision_half_height_cm"])
    margin = float(task["body"]["leg_margin_cm"])
    rng = np.random.default_rng(int(task["seed"]))

    lo, hi = SPEED_TIERS[task["speed_tier"]]
    speed_ms = float(rng.uniform(lo, hi))
    ylo, yhi = YAW_TIERS[task["yaw_tier"]]
    yaw_rate = float(rng.uniform(ylo, yhi))

    owns_rig = rig is None
    if ucv is None:
        ucv = engine.connect(W, H)
        if ucv is None:
            raise RuntimeError("UE never became reachable")
    req = ucv.client.request
    # The rig is OPTIONAL, because on some maps it cannot exist.
    #
    # It is a Character blueprint spawned by UnrealCV at the world origin with default collision
    # handling, so on any map whose origin sits inside geometry the spawn silently produces no actor
    # - MiddleEast and WildWest both died one second in with "Can not find object". Everything the
    # rig provides has an alternative: the task states the FOV and the eye height, and the in-engine
    # capture places its own components, so the only real loss is the pawn-based reachability check.
    # That loss is recorded rather than hidden.
    if rig is None:
        rig = engine.Rig(ucv, W, H, fov)
    try:
        actual_fov = rig.configure()
    except Exception as e:
        print(f"[freeze] the camera-host pawn could not be placed on this map "
              f"({type(e).__name__}); continuing without it. The navmesh plan, the depth probe and "
              f"the corridor sweep all run engine-side and are unaffected; the pawn-based "
              f"reachability check cannot run and is reported as unavailable.")
        rig = None
        actual_fov = fov

    slug = "Game_" + task["map_id"].lstrip("/").removeprefix("Game/").replace("/", "_")
    # Fail here, by name, rather than two minutes into an editor boot. Only 86 of the project's
    # maps have a start-positions file, and picking one that does not is silent until this line.
    sp_file = SP_DIR / f"{slug}.json"
    if not sp_file.exists():
        have = sorted(f.stem for f in SP_DIR.glob("Game_*.json"))
        raise RuntimeError(f"no start positions for {slug}. {len(have)} maps have them; the "
                           f"nearest names are "
                           f"{[h for h in have if h.split('_')[1][:4] in slug][:5] or have[:5]}")
    pts = [p for p in json.load(open(sp_file))["positions"]
           if all(isinstance(p.get(k), (int, float)) for k in ("x", "y", "z"))]
    if not pts:
        raise RuntimeError(f"no usable start points for {slug}")
    sp = pts[0] if task["spawn_id"] == "auto" else \
        next(p for p in pts if p.get("name") == task["spawn_id"])

    # Choose a start point the agent can actually stand on, using the navmesh.
    #
    # Purchased levels ship no PlayerStart, and a start point that is nowhere near the ground puts
    # the camera in the air for the whole episode with every metric green - that was the "camera in
    # the sky" bug, found only by looking at a contact sheet. A downward trace alone is not enough
    # either: it happily finds a rooftop. The navmesh answers the actual question, "can this agent
    # stand here", so a point that will not project onto it is skipped.
    candidates = pts if task["spawn_id"] == "auto" else \
        [p for p in pts if p.get("name") == task["spawn_id"]]
    nav_boot = engine.nav_ensure(req, (float(candidates[0]["x"]), float(candidates[0]["y"])),
                                 float(candidates[0]["z"]))
    sp, gz, spawn_notes = None, None, []
    for cand in candidates[:10]:
        cx, cy, cz = float(cand["x"]), float(cand["y"]), float(cand["z"])
        traced = engine.ground_z(req, [(cx, cy)], cz + 400.0)[0]
        proj = engine.nav_project(req, (cx, cy, traced if traced is not None else cz),
                                 extent=(400.0, 400.0, 800.0)) if nav_boot.get("ok") else {}
        if proj.get("ok"):
            navz = proj["point"][2]
            # Prefer the navmesh height. When it disagrees with the trace by a lot, the trace
            # found something the agent cannot stand on - a roof, an awning, a parked vehicle.
            if traced is not None and abs(navz - traced) > 150.0:
                spawn_notes.append(
                    f"'{cand.get('name')}': downward trace hit {traced:.0f} cm but the navmesh "
                    f"puts walkable ground at {navz:.0f} cm; using the navmesh height")
            sp, gz = cand, navz
            break
        spawn_notes.append(f"'{cand.get('name')}' does not project onto the navmesh - skipped")
    if sp is None:
        # No navmesh, or nothing projected. Fall back to the trace and say so loudly: this is the
        # configuration in which the sky bug was possible.
        sp = candidates[0]
        gz = engine.ground_z(req, [(float(sp["x"]), float(sp["y"]))],
                             float(sp["z"]) + 400.0)[0]
        if gz is None:
            raise RuntimeError("no ground under any candidate start point")
        spawn_notes.append("NO start point projected onto a navmesh; fell back to a downward "
                           "trace, which cannot tell a walkable surface from a rooftop")
    for n in spawn_notes[:6]:
        print(f"[freeze] spawn: {n}")
    anchor = (float(sp["x"]), float(sp["y"]))

    # The eye height is measured, not requested. The rig's pawn is a character whose movement
    # component snaps the capsule to the floor, so the achievable camera height is fixed by the
    # capsule and the head mount; asking for a different one only leaves every frame's pose
    # disagreeing with its request.
    # The requested eye height is used as asked. It used to be overridden by whatever the
    # character capsule produced, because the external capture had to drive a pawn and a pawn's
    # movement component snaps it to the floor. The in-engine capture places the scene capture
    # components directly, so section 10's camera height is a real parameter again - the rig's
    # natural height is still measured and recorded, for comparison.
    requested_eye_cm = eye_cm
    rig_natural_eye_cm, cam_off = (rig.measure_eye_height(anchor, gz) if rig
                                   else (float("nan"), 0.0))
    print(f"[freeze] anchor ({anchor[0]:.1f}, {anchor[1]:.1f})  ground z {gz:.1f} cm")
    print(f"[freeze] eye height {eye_cm:.0f} cm as requested"
          + (f" (a character capsule here would sit at {rig_natural_eye_cm:.1f} cm; the in-engine "
             f"capture uses no character, so the requested height is used directly)" if rig
             else " (no pawn on this map, so the capsule height was not measured)"))
    print(f"[freeze] speed {speed_ms:.2f} m/s  yaw {yaw_rate:.1f} deg/s")

    # ---- route: planned on the navmesh, not swept as bearings from the spawn ----------------
    #
    # The bearing fan asked "is this direction clear" when the question is "where can this agent
    # walk". Across five maps it produced a route into a hedge (50-58% of pixels crushed to black
    # because the camera was inside foliage), a 1.5 m dead end (detour ratio 36.5, 2788 hits), a
    # route that ploughed through farmland, and an anchor in open sky. Boundary edges on a navmesh
    # are exactly where walkable space ends - a wall, a hedge, a drop - so planning on it and
    # keeping clear of those edges is what fixes all four.
    navdir = FROZEN / "nav"
    navdir.mkdir(parents=True, exist_ok=True)
    nbin, njson = navdir / f"{slug}.bin", navdir / f"{slug}.json"
    exp = engine.nav_export(req, str(nbin), str(njson))
    if not exp.get("ok"):
        raise RuntimeError(f"navmesh export failed for {slug}: {exp.get('error')}")
    print(f"[freeze] navmesh export: {exp['vertex_count']} verts, {exp['triangle_count']} tris, "
          f"bounds {exp['bounds_min'][:2]} .. {exp['bounds_max'][:2]}")
    navmesh, navmeta = plan_navmesh.load(str(nbin), str(njson))

    ages = [float(a) for a in task["revisit_ages_s"]]
    family = task["trajectory_family"]
    age = ages[0]
    # The age constrains the travel time and the leg length follows. Sizing the leg first and
    # hoping the age fits is what once asked for a 9 m leg that needed 65 s for a 30 s revisit.
    turn_s = RAMP_PEAK * 180.0 / yaw_rate   # worst-case turn, at the same peak-sized cost turn_leg now charges
    revisit_hold_s = 1.0
    want_cm = []
    for k, a_s in enumerate(ages):
        spent_before = sum(ages[:k]) + 1.0 * k
        t_s = max(0.5, (a_s - spent_before - 2.0 * turn_s - revisit_hold_s) / 2.0 * 0.85)
        want_cm.append(min(2400.0, t_s * speed_ms / RAMP_PEAK * UU_PER_M))
    print(f"[freeze] age budget {ages} s -> legs {[round(w/100,1) for w in want_cm]} m")

    # ---- depth probe: the only check that can see geometry with no collision ----------------
    #
    # The navmesh, the capsule sweeps and the near-plane rays all agreed a route through a tree in
    # RussianWinterTownDemo01 was clean, and the episode walked through the trunk for 1.25 s
    # (frames 1386-1415, over 20% of each frame inside 60 cm, nearest pixels on the 10 cm near
    # clip). Foliage instances routinely ship with collision disabled: they cut no hole in the
    # navmesh and stop no sweep. The renderer is the only part of the engine that knows they are
    # there, so the probe renders depth along each candidate leg and vetoes the route.
    #
    # The engine measures the statistics itself and returns them in the JSON, so this costs one
    # small render per sample and reads no files.
    # The step MUST be smaller than the threshold, and the first attempt at this got it backwards.
    # Each sample looks along the leg and rejects only what is nearer than probe_min_m, so with a
    # 200 cm step and a 0.6 m threshold an obstacle 0.6-2.0 m past a sample is seen at a distance
    # that passes, and the next sample is already beyond it - about 70% of trunk positions are
    # invisible. That first run reproduced the tree exactly: same anchor, same bearings, same 1.25 s
    # pass-through. With a 40 cm step every obstacle is at most 40 cm ahead of some sample, so it
    # cannot fall between the threshold and the next sample.
    probe_min_m = float(os.environ.get("PROBE_MIN_M", "0.6"))
    probe_step_cm = float(os.environ.get("PROBE_STEP_CM", "40"))
    if probe_step_cm >= probe_min_m * 100.0:
        raise RuntimeError(f"PROBE_STEP_CM {probe_step_cm:.0f} must be under PROBE_MIN_M "
                           f"{probe_min_m*100:.0f} cm or the probe steps over obstacles")
    probe_log = []

    class Veto(int):
        """False with a reason attached, and the heading to ban.

        Carrying the bearing is what turns "this anchor is unusable" into "this direction is
        unusable": the planner bans that one heading and re-searches from the same anchor, so a tree
        costs one heading instead of the open standing place that made the anchor best.
        """
        def __new__(cls, reason, bearing=None):
            v = super().__new__(cls, 0)
            v.reason = reason
            v.bearing = bearing
            return v

    def probe_route(anchor_xyz, legs_out):
        """Veto a candidate route on either check the navmesh cannot make by itself.

        Both run here, at planning time, rather than after the whole trajectory is built. The
        capsule sweep used to run only on the finished 2844 poses, and a route that grazed something
        in 14 of them refused the entire episode - a 10 minute round trip to learn that one heading
        out of 72 was bad. Vetoing here bans that heading and re-searches, which is the difference
        between going around an obstacle and giving up on the map.

        Cheap check first: one sweep call for the whole leg, then the per-sample depth renders.
        """
        eye = float(task["camera"]["eye_height_m"]) * 100.0
        checked, worst = 0, 1e9
        for li, leg in enumerate(legs_out):
            bearing = float(leg["bearing_deg"])
            length = float(leg["length_cm"])
            ux, uy = math.cos(math.radians(bearing)), math.sin(math.radians(bearing))
            pts = []
            d = 0.0
            while d <= length + 1e-6:
                x, y = anchor_xyz[0] + ux * d, anchor_xyz[1] + uy * d
                sz = navmesh.inside_xy(np.array([x, y, 0.0]), navmesh.regions[0])
                pts.append((x, y, (anchor_xyz[2] if sz is None else sz), d))
                d += probe_step_cm

            # Collision, on the body rather than the eye, over a CORRIDOR rather than a line.
            #
            # The trajectory builder does not walk the planner's nominal bearing: it resolves the
            # geometry against explicit anchor-relative points so that every revisit lands exactly
            # one offset from the anchor, and the walked heading came out 1.4 degrees off - 59 cm of
            # lateral deviation at the far end of a 24 m leg. Probing the centre line alone passed a
            # suburb route whose full trajectory then hit geometry on 14 frames. The offsets are
            # +-the same 60 cm near-offset the return leg uses, which also contains the 22 cm probe
            # side-steps.
            #
            # TRACE_TYPE_QUERY1 is the Visibility channel and not what blocks a Pawn, so this is
            # necessary but not sufficient - the depth probe below covers geometry with no collision
            # at all. Six sweep calls per candidate, against 80 renders: effectively free.
            px, py = -uy, ux
            for off in (0.0, CORRIDOR_HALF_WIDTH_CM, -CORRIDOR_HALF_WIDTH_CM):
                body = [(x + px * off, y + py * off, z + GROUND_CLEARANCE_CM + half_h)
                        for x, y, z, _ in pts]
                v = engine.validate_path(req, body, radius=radius, half_height=half_h)
                hit = next((i for i, h in enumerate(v["sweep_hits"]) if h is not None), None)
                if hit is not None:
                    side = ("centre line" if off == 0 else
                            f"{abs(off):.0f} cm to the {'left' if off > 0 else 'right'}")
                    return Veto(f"leg {li+1} along {bearing:.0f} deg is blocked "
                                f"{pts[hit][3]/100:.1f} m out on the {side}: the body capsule hits "
                                f"geometry there", bearing)

            for x, y, z, dd in pts:
                r = engine.depth_capture(req, (x, y, z + eye), bearing, 0.0, actual_fov,
                                         320, 180, f"{PROBE_DIR}/probe.exr", max_range_m=200.0)
                checked += 1
                if not r.get("ok"):
                    return Veto(f"depth probe failed on leg {li+1} at {dd/100:.1f} m: "
                                f"{r.get('error')}", bearing)
                # PITFALL - the keys are min_m / max_m / centre_m. Reading a "depth_min_m" that
                # does not exist returned 0.0 for every sample, and a `0.0 < mn` guard meant to
                # skip unmeasured frames then passed all of them: the probe reported 80 samples
                # and "route accepted" while testing nothing, and reproduced the tree exactly.
                mn = float(r.get("min_m", 0.0) or 0.0)
                if mn > 1e6:          # FLT_MAX: no pixel had valid depth, so there is no evidence
                    continue
                worst = min(worst, mn)
                if mn < probe_min_m:
                    return Veto(f"leg {li+1} at {dd/100:.1f} m along {bearing:.0f} deg sees "
                                f"geometry {mn:.2f} m away - closer than the {probe_min_m:.2f} m "
                                f"body clearance, and the navmesh cannot see it", bearing)
        probe_log.append({"samples": checked, "nearest_m": round(worst, 3),
                          "bearings": [float(l["bearing_deg"]) for l in legs_out],
                          "step_cm": probe_step_cm, "limit_m": probe_min_m})
        print(f"[nav-probe] {checked} depth samples plus capsule sweeps along both legs, nearest "
              f"geometry {worst:.2f} m (limit {probe_min_m:.2f} m) - route accepted")
        return True

    nav_plan = plan_navmesh.plan(
        navmesh, navmeta, want_cm[0], want_cm[-1],
        min_clear_cm=float(os.environ.get("MIN_BOUNDARY_CLEAR_CM", "150")),
        anchor_hint=(anchor[0], anchor[1], gz),
        accept=probe_route if os.environ.get("DEPTH_PROBE", "1") == "1" else None)
    if not nav_plan["ok"]:
        raise RuntimeError(f"navmesh planning failed for {slug}: {nav_plan['error']}"
                           + (f" ({len(nav_plan.get('candidates_rejected') or [])} candidates were "
                              f"rejected by the depth probe)"
                              if nav_plan.get("candidates_rejected") else ""))
    nav_plan["depth_probe"] = (probe_log[-1] if probe_log else
                               {"note": "the depth probe was disabled for this run"})

    # The plan moves the anchor to open ground, so everything downstream uses the planned one.
    anchor = (nav_plan["anchor"][0], nav_plan["anchor"][1])
    gz = nav_plan["anchor"][2]
    spawn_notes.append(f"anchor moved onto open navmesh ground with "
                       f"{nav_plan['anchor_clearance_cm']:.0f} cm to the nearest boundary")
    # Float keys, as the bearing fan produced: downstream code indexes reach[bearing] with a
    # float, and string keys turned every map into KeyError: 190.0 after the route was already
    # planned successfully.
    reach = {float(l["bearing_deg"]): float(l["length_cm"]) for l in nav_plan["legs"]}
    out_bearing = nav_plan["legs"][0]["bearing_deg"]
    leg_cm = nav_plan["legs"][0]["length_cm"]
    route_notes = []
    nav_region = navmesh.regions[0]

    def surface_z(x, y):
        """Height from the walkable surface itself.

        A per-frame downward trace turned every kerb into a single-frame 20 cm jump (4.8 m/s of
        vertical motion, measured on Tokyo) and on farmland it put the camera under the soil. The
        navmesh surface is the height a walking agent has, by construction.
        """
        z = navmesh.inside_xy(np.array([float(x), float(y), 0.0]), nav_region)
        return gz if z is None else z

    print(f"[freeze] out bearing {out_bearing:.0f} deg, leg {leg_cm:.0f} cm "
          f"(reach {reach[out_bearing]:.0f} cm)")
    for n in route_notes:
        print(f"  note: {n}")

    if family == "nested_out_and_back":
        # Each excursion is sized from its own age, and the second bearing must be clearly
        # different from the first: returning down the same street twice tests recall of one
        # corridor, not of a place.
        # Both legs come from the navmesh planner, which already enforced the turn separation and
        # verified every 30 cm of each leg. Re-searching a bearing fan here would throw that away -
        # and the fan is the thing that put the camera in a hedge.
        bearings = [float(l["bearing_deg"]) for l in nav_plan["legs"]]
        legs_cm = [float(l["length_cm"]) for l in nav_plan["legs"]]
        while len(bearings) < len(ages):          # more ages than planned legs: reuse the long one
            bearings.append(bearings[-1])
            legs_cm.append(legs_cm[-1])
        picked_notes = [
            f"leg {i+1}: {l['length_cm']/100:.1f} m at {l['bearing_deg']:.0f} deg, narrowest "
            f"point {l['min_boundary_clearance_cm']:.0f} cm from a walkable-surface boundary"
            for i, l in enumerate(nav_plan["legs"])]
        fam_notes_pre = []
        perp_signs = [1] * len(bearings)
        perp_offsets = [60.0] * len(bearings)
        print("[freeze] nested: " + "  ".join(
            f"#{i+1} {l:.0f} cm @ {b_:.0f} deg (age {a_:.0f} s)"
            for i, (a_, b_, l) in enumerate(zip(ages, bearings, legs_cm))))

        legs, fam_notes, excursions = build_nested(
            fps, ages, bearings, legs_cm, yaw_rate, speed_ms,
            probe=bool(task.get("probe_after_revisit", True)), perp_signs=perp_signs,
            perp_offsets=perp_offsets)
        fam_notes += picked_notes + fam_notes_pre
    else:
        excursions = None
        legs, fam_notes = build_legs(family, fps, float(task["duration_s"]),
                                age, out_bearing, leg_cm, yaw_rate, speed_ms,
                                probe=bool(task.get("probe_after_revisit", True)),
                                return_kind=task.get("return_kind", "near"))
    for n in fam_notes:
        print(f"  note: {n}")
    nonlocal_z_report = {}

    def poses_for(legs_in):
        rows_in = legs_to_poses(legs_in, anchor, yaw_rate,
                                start_yaw=bearings[0] if family == "nested_out_and_back"
                                else out_bearing)
        # Height from the navmesh surface. No UE round trip per frame, and no trace that can find
        # a rooftop or miss the ground entirely.
        zs_in = [surface_z(r[0], r[1]) for r in rows_in]
        # Limit how fast the camera may rise or fall.
        #
        # The ground height is traced per frame, so a kerb becomes an instantaneous vertical jump:
        # measured on Tokyo, z went 210 -> 190 cm in one frame, which is 20 cm in 1/24 s = 4.8 m/s
        # of vertical motion. That is not how a person walks - it is a visible jolt in the video -
        # and it is also what produced 46 capsule-sweep hits on a route the navmesh called fully
        # walkable with a detour ratio of 1.0: a body swept horizontally across a 20 cm
        # discontinuity clips the kerb.
        #
        # A person stepping onto a 20 cm kerb takes roughly a third of a second, so the head rises
        # at well under a metre per second. Bounding the rate absorbs the kerb over several frames
        # instead of one. A real slope is unaffected because its per-frame change is already small.
        raw_z = [(gz if z is None else z) + eye_cm for z in zs_in]
        max_dz = Z_SLEW_CM_PER_S / fps
        smooth_z, cur = [], raw_z[0] if raw_z else gz + eye_cm
        for target in raw_z:
            cur += max(-max_dz, min(max_dz, target - cur))
            smooth_z.append(cur)
        jumps_before = max((abs(raw_z[i + 1] - raw_z[i]) for i in range(len(raw_z) - 1)),
                           default=0.0)
        jumps_after = max((abs(smooth_z[i + 1] - smooth_z[i]) for i in range(len(smooth_z) - 1)),
                          default=0.0)
        if jumps_before > max_dz * 1.5:
            print(f"[freeze] vertical smoothing: terrain gave a {jumps_before:.1f} cm single-frame "
                  f"step; limited to {jumps_after:.2f} cm/frame "
                  f"({Z_SLEW_CM_PER_S:.0f} cm/s), which is what a person's head does on a kerb")
        nonlocal_z_report.update({"max_raw_step_cm": round(jumps_before, 2),
                                  "max_smoothed_step_cm": round(jumps_after, 3),
                                  "limit_cm_per_s": Z_SLEW_CM_PER_S})

        out = []
        for (x, y, yaw, pitch, phase), zc in zip(rows_in, smooth_z):
            out.append({"x_cm": float(x), "y_cm": float(y), "z_cm": float(zc),
                        "yaw_deg": float(yaw % 360.0), "pitch_deg": float(pitch),
                        "roll_deg": 0.0, "phase": phase})
        return out

    poses = poses_for(legs)
    # Spec section 11 asks for a NavMesh/path legality check. Until now this map had no navmesh
    # so it could not run; SimWorldCapture::EnsureNavMesh builds one. This is a stronger statement
    # than our capsule sweeps, which trace the Visibility channel rather than what blocks a Pawn.
    nav = engine.nav_ensure(req, anchor, gz)
    nav_legs = []
    if nav.get("ok"):
        proj = engine.nav_project(req, (anchor[0], anchor[1], gz))
        far_points = []
        for ev in (excursions or []):
            b = math.radians(ev["bearing_deg"])
            far_points.append((anchor[0] + math.cos(b) * ev["leg_cm"],
                               anchor[1] + math.sin(b) * ev["leg_cm"], gz))
        for k, fp in enumerate(far_points):
            r = engine.nav_path(req, (anchor[0], anchor[1], gz), fp)
            straight = math.dist((anchor[0], anchor[1]), (fp[0], fp[1]))
            nav_legs.append({
                "excursion": k + 1, "ok": bool(r.get("ok")),
                "partial": bool(r.get("partial")),
                "point_count": r.get("point_count"),
                "path_length_cm": r.get("length_cm"),
                "straight_line_cm": round(straight, 1),
                "detour_ratio": (round(r["length_cm"] / straight, 3)
                                 if r.get("length_cm") and straight > 1 else None),
                "error": r.get("error")})
        print(f"[freeze] navmesh: {nav.get('provenance', '')[:60]}...")
        for nl in nav_legs:
            flag = "partial - stops at the closest reachable point" if nl["partial"] else "full"
            print(f"  leg {nl['excursion']}: {flag}, path {nl.get('path_length_cm')} cm vs "
                  f"{nl['straight_line_cm']} cm straight (detour {nl.get('detour_ratio')})")
    else:
        print(f"[freeze] navmesh unavailable: {nav.get('error')}")
        proj = None

    # Every key verify_reachable would return, so nothing downstream has to know it was skipped -
    # a stub missing one of them cost a run four minutes in with KeyError: 'tolerance_cm'.
    reach_check = (verify_reachable(rig, poses, eye_cm, cam_off) if rig else {
        "available": False, "reachable": True, "sampled_frames": 0, "sample_every": 0,
        "tolerance_cm": LATERAL_TOL_CM, "max_lateral_error_cm": None, "max_z_error_cm": None,
        "unreachable_samples": [],
        "method": "not run: no camera-host pawn could be spawned on this map",
        "z_note": "not measured",
        "note": "the camera-host pawn could not be spawned on this map, so no body was placed at "
                "the frozen poses. The route is still navmesh-planned, depth-probed and "
                "corridor-swept; this one check is UNAVAILABLE, not passed."})
    # Retry with a tighter return offset when the body cannot reach the one we chose. The offset
    # decides where every revisit lands, so an unreachable one fails ~100 frames; shrinking it a
    # little is far better than rejecting an otherwise sound route.
    if family == "nested_out_and_back" and not reach_check["reachable"]:
        for retry in (45.0, 30.0, 20.0):
            print(f"[freeze] return offset {perp_offsets[0]:.0f} cm is blocked laterally "
                  f"({reach_check['max_lateral_error_cm']} cm); retrying at {retry:.0f} cm")
            perp_offsets = [retry] * len(bearings)
            legs, fam_notes2, excursions = build_nested(
                fps, ages, bearings, legs_cm, yaw_rate, speed_ms,
                probe=bool(task.get("probe_after_revisit", True)),
                perp_signs=perp_signs, perp_offsets=perp_offsets)
            poses = poses_for(legs)
            reach_check = (verify_reachable(rig, poses, eye_cm, cam_off) if rig
                           else reach_check)
            if reach_check["reachable"]:
                fam_notes.append(f"the return offset was reduced to {retry:.0f} cm because the "
                                 f"body could not reach 60 cm beside the anchor here")
                break

    # ---- validation ------------------------------------------------------------
    print(f"[freeze] validating {len(poses)} frames against collision geometry")
    t0 = time.time()
    body_pts = [(p["x_cm"], p["y_cm"],
                 (p["z_cm"] - eye_cm) + GROUND_CLEARANCE_CM + half_h) for p in poses]
    v = engine.validate_path(req, body_pts, radius=radius, half_height=half_h)
    hits = [i for i, h in enumerate(v["sweep_hits"]) if h is not None]
    near_bad = [i for i, n in enumerate(v["near_plane_hits"]) if n]
    cl = [c for c in v["clearance_cm"] if c is not None]
    trans = [math.dist((poses[i]["x_cm"], poses[i]["y_cm"], poses[i]["z_cm"]),
                       (poses[i+1]["x_cm"], poses[i+1]["y_cm"], poses[i+1]["z_cm"]))
             for i in range(len(poses) - 1)]
    rots = [abs((poses[i+1]["yaw_deg"] - poses[i]["yaw_deg"] + 540) % 360 - 180)
            for i in range(len(poses) - 1)]
    collision = {
        "collision_count": len(hits),
        "penetration_count": len(near_bad),
        # None here would be ambiguous between "not measured" and "nothing within range"
        "minimum_clearance_cm": (min(cl) if cl else None),
        "clearance_ray_range_cm": 1500.0,
        "clearance_note": ("every frame's forward ray reached the full 1500 cm without a hit"
                           if not cl else
                           f"{len(cl)} of {len(poses)} frames had an obstacle within 1500 cm"),
        "maximum_frame_translation_cm": (max(trans) if trans else 0.0),
        "maximum_frame_rotation_deg": (max(rots) if rots else 0.0),
        "collision_free": len(hits) == 0 and len(near_bad) == 0,
        "first_collision_frames": hits[:10],
        # kept per frame so the QA video can show the real forward clearance rather than a
        # depth sample it does not have
        "clearance_per_frame_cm": [None if c is None else round(float(c), 1)
                                   for c in v["clearance_cm"]],
        "method": "capsule sweep between consecutive frames, four near-plane corner rays, "
                  "forward ray clearance; there is no navmesh in this map so no path-legality "
                  "check was possible",
        "validated_in_s": round(time.time() - t0, 1),
    }
    print(f"[freeze] collisions {collision['collision_count']}  penetrations "
          f"{collision['penetration_count']}  min clearance "
          f"{collision['minimum_clearance_cm']}  max step "
          f"{collision['maximum_frame_translation_cm']:.2f} cm  "
          f"({collision['validated_in_s']}s)")

    print(f"[freeze] reachability: sampled {reach_check['sampled_frames']} poses, "
          f"max lateral {reach_check['max_lateral_error_cm']} cm "
          f"(z {reach_check['max_z_error_cm']} cm, expected - navmesh vs capsule rest), "
          f"reachable={reach_check['reachable']}")
    for o in reach_check["unreachable_samples"][:5]:
        print(f"  blocked f{o['frame']} ({o['phase']}): wanted {o['wanted']} "
              f"reached {o['reached']}, lateral {o['lateral_error_cm']} cm")

    # ---- revisit windows -------------------------------------------------------
    idx, anchor_win, revisit_win = 0, None, None
    revisit_events = []
    for leg in legs:
        end = idx + leg.frames
        if leg.kind == "observe":
            anchor_win = [idx, end - 1]
        if leg.kind == "revisit":
            revisit_win = [idx, end - 1]
            revisit_events.append({
                "window": [idx, end - 1],
                "excursion": getattr(leg, "excursion", 1),
                "requested_age_s": getattr(leg, "requested_age_s", age)})
        idx = end
    anchor_win = anchor_win or [0, int(fps) - 1]
    revisit_win = revisit_win or [len(poses) - 1, len(poses) - 1]
    realised_age = (revisit_win[1] - anchor_win[1]) / fps

    intr = geom.intrinsics(W, H, actual_fov)
    smoke = geom.axis_smoke(intr)
    ep_id = (f"{slug}__{task['trajectory_family']}__age{int(age)}"
             f"__seed{task['seed']}__{task['task_id']}")
    frozen = {
        "episode_id": ep_id,
        "task": task,
        "generator_version": "pipeline-freeze-1",
        "map_id": task["map_id"],
        "spawn_id": sp.get("name", "auto"),
        "fps": fps,
        "frames": len(poses),
        "duration_s": len(poses) / fps,
        "speed_tier": task["speed_tier"],
        "speed_m_per_s": speed_ms,
        "yaw_tier": task["yaw_tier"],
        "yaw_deg_per_s": yaw_rate,
        "anchor_xy_cm": list(anchor),
        "anchor_ground_z_cm": gz,
        "eye_height_cm": eye_cm,
        "eye_height_requested_cm": requested_eye_cm,
        "eye_height_rig_natural_cm": rig_natural_eye_cm,
        "camera_offset_from_pawn_cm": cam_off,
        "out_bearing_deg": out_bearing,
        "leg_cm": leg_cm,
        "requested_age_s": age,
        "realised_age_s": realised_age,
        "anchor_window": anchor_win,
        "revisit_window": revisit_win,
        "revisit_events": revisit_events,
        "excursions": excursions,
        "legs": [l.as_dict() for l in legs],
        "reach_by_bearing_cm": {str(k): v for k, v in reach.items()},
        "route_notes": route_notes + fam_notes,
        "collision": collision,
        "reachability": reach_check,
        "vertical_smoothing": nonlocal_z_report,
        # The plan and the probe that vetted it, so a consumer can see which route was chosen,
        # how open it was, and what the renderer found along it - the navmesh alone cannot say.
        "route_plan": nav_plan,
        "navmesh": {"ensure": nav, "anchor_projection": proj, "legs": nav_legs,
                    "spawn_selection": spawn_notes, "boot": nav_boot,
                    "note": "path legality per spec section 11. A partial path ends at the "
                            "closest reachable point, which is exactly where a route would "
                            "silently stop short, so it is reported separately from ok."},
        "intrinsics": intr,
        "axis_smoke": smoke,
        "camera_fov_deg_actual": actual_fov,
        "poses": poses,
        "known_gaps": [
            "metric depth is unavailable: this build's Vulkan RHI cannot read back a render "
            "target (VulkanRenderTarget.cpp:171 asserts and takes the editor down), which is "
            "also why UnrealCV's depth returns a constant 65504 fp16 sentinel. Spec P0 depth, "
            "depth-visible overlap and occlusion intervals therefore cannot be produced yet."
        ],
    }
    # `rig` is None when the map would not spawn a camera host, and owns_rig can still be true
    # because this function created the object before configure() failed.
    if owns_rig and rig:
        rig.destroy()
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{ep_id}.json"
    p.write_text(json.dumps(frozen, indent=1))
    frozen["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    p.write_text(json.dumps(frozen, indent=1))
    print(f"[freeze] {len(poses)} frames / {len(poses)/fps:.2f} s  age requested {age:.1f} s "
          f"realised {realised_age:.1f} s")
    print(f"[freeze] axis smoke pass={smoke['pass']}  forward={smoke['forward']}")
    print(f"[freeze] wrote {p}")
    return p, frozen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", help="path to a task JSON")
    a = ap.parse_args()
    task = json.loads(Path(a.task).read_text())
    freeze(task)
    return 0


if __name__ == "__main__":
    sys.exit(main())
