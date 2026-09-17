#!/usr/bin/env python3
"""Camera geometry: UE conventions in, canonical conventions out.

Spec section 7 fixes both ends of the contract:

    UE          left-handed, X forward, Y right, Z up, centimetres
    canonical   x right, y down, z forward, metres, camera-to-world, column vectors

and requires a quaternion or rotation matrix as the rotation truth - Euler angles may be
stored for human reading but must not be the only record. This build's pose readback is Euler
(`vget /camera/N/rotation`), so the quaternion here is derived from it and both are written,
with the derivation recorded in `camera.json` rather than left implicit.
"""
import math

import numpy as np


def intrinsics(width, height, fov_deg):
    """UE's `fov_angle` is the horizontal field of view, so fx comes from the width and fy is
    equal to it - square pixels. cx/cy are the exact image centre."""
    fx = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    return {"fx": fx, "fy": fx, "cx": width / 2.0, "cy": height / 2.0,
            "width": width, "height": height, "hfov_deg": fov_deg,
            "vfov_deg": math.degrees(2 * math.atan((height / 2.0) / fx))}


def K_matrix(intr):
    return np.array([[intr["fx"], 0.0, intr["cx"]],
                     [0.0, intr["fy"], intr["cy"]],
                     [0.0, 0.0, 1.0]])


def scale_intrinsics(intr, out_w, out_h):
    """Spec section 8: after a resize the training K must be the resized one."""
    sx, sy = out_w / intr["width"], out_h / intr["height"]
    return {"fx": intr["fx"] * sx, "fy": intr["fy"] * sy,
            "cx": intr["cx"] * sx, "cy": intr["cy"] * sy,
            "width": out_w, "height": out_h,
            "hfov_deg": intr["hfov_deg"], "vfov_deg": intr["vfov_deg"],
            "resize_scale_x": sx, "resize_scale_y": sy}


def ue_basis(pitch, yaw, roll):
    """Columns are UE's forward, right, up world vectors, matching FRotationMatrix."""
    p, y, r = (math.radians(v) for v in (pitch, yaw, roll))
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    cr, sr = math.cos(r), math.sin(r)
    fwd = np.array([cp * cy, cp * sy, sp])
    right = np.array([cy * sp * sr - sy * cr, sy * sp * sr + cy * cr, -cp * sr])
    up = np.array([-cy * sp * cr - sy * sr, -sy * sp * cr + cy * sr, cp * cr])
    return fwd, right, up


def canonical_c2w(loc_cm, pitch, yaw, roll):
    """Camera-to-world in canonical axes and metres.

    The canonical basis columns are (right, -up, forward) of the UE camera basis: canonical x is
    right, y is down (hence -up), z is forward.
    """
    fwd, right, up = ue_basis(pitch, yaw, roll)
    R = np.column_stack([right, -up, fwd])
    t = np.asarray(loc_cm, dtype=float) / 100.0
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = t
    return M


def quat_xyzw_from_euler(pitch, yaw, roll):
    """UE rotator to quaternion, in UE's own left-handed world axes.

    Derived from the same basis used for c2w so the two can never disagree; verified by
    round-tripping back to a matrix in the self-test below.
    """
    fwd, right, up = ue_basis(pitch, yaw, roll)
    m = np.column_stack([fwd, right, up])       # UE rotation matrix, columns X/Y/Z
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    return [x, y, z, w]


def matrix_from_quat_xyzw(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def project(K, c2w, world_pt_cm):
    """Project a world point (cm) through a canonical c2w into pixels."""
    w2c = np.linalg.inv(c2w)
    p = w2c @ np.array([world_pt_cm[0] / 100.0, world_pt_cm[1] / 100.0,
                        world_pt_cm[2] / 100.0, 1.0])
    if p[2] <= 1e-9:
        return None
    uv = K @ (p[:3] / p[2])
    return [float(uv[0]), float(uv[1])]


def axis_smoke(intr, loc_cm=(0.0, 0.0, 0.0), pitch=0.0, yaw=0.0, roll=0.0):
    """Spec section 14 step 6/7: prove forward/right/up land where the convention says.

    forward -> exact image centre, right -> +u, up -> -v. Returned as data so the caller can
    gate on it instead of eyeballing a log line.
    """
    K = K_matrix(intr)
    c2w = canonical_c2w(loc_cm, pitch, yaw, roll)
    fwd, right, up = ue_basis(pitch, yaw, roll)
    o = np.asarray(loc_cm, dtype=float)
    D = 1000.0
    res = {
        "forward": project(K, c2w, o + fwd * D),
        "forward_right": project(K, c2w, o + fwd * D + right * D),
        "forward_up": project(K, c2w, o + fwd * D + up * D),
    }
    cx, cy = intr["cx"], intr["cy"]
    res["pass"] = bool(
        res["forward"] and abs(res["forward"][0] - cx) < 0.01
        and abs(res["forward"][1] - cy) < 0.01
        and res["forward_right"][0] > cx + 1.0
        and res["forward_up"][1] < cy - 1.0)
    return res


if __name__ == "__main__":
    intr = intrinsics(1280, 720, 90.0)
    print("intrinsics", {k: round(v, 4) if isinstance(v, float) else v
                         for k, v in intr.items()})
    s = axis_smoke(intr)
    print("axis smoke", s)
    # quaternion must reproduce the Euler basis exactly
    worst = 0.0
    for p in (-15, 0, 12):
        for y in (0, 58.4, 178.4, 300):
            for r in (-5, 0, 3):
                f, ri, u = ue_basis(p, y, r)
                m = np.column_stack([f, ri, u])
                m2 = matrix_from_quat_xyzw(quat_xyzw_from_euler(p, y, r))
                worst = max(worst, float(np.abs(m - m2).max()))
    print(f"quaternion vs Euler basis, worst element error: {worst:.2e}")
