#!/usr/bin/env python3
"""Smoke-test SimWorldCapture: depth EXR and navmesh, verified against independent measurements.

Nothing here trusts a return code. Depth is checked by sweeping several headings and comparing
the centre pixel against a line trace along the same axis - two independent measurements of the
same distance. That comparison is what catches the failure mode that matters: depth which is
present, correctly formatted, plausible, and from the wrong pose.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import engine  # noqa: E402

# Downtown_West anchor used by every episode so far. The capsule fan measured a storefront
# corner 273 cm along 58.4 deg and clear space along 0 deg, so the two headings should give
# very different centre depths - if they give the same, the rotation is not being applied.
X, Y, FLOOR, EYE = -2420.4, 1148.3, 2.8, 169.0
YAWS = [0.0, 58.4, 90.0, 180.0, 270.0]
W, H, FOV = 640, 360, 90.0

BODY = f"""
import unreal, json as _json
res = {{"depth": [], }}
loc = unreal.Vector({X}, {Y}, {FLOOR + EYE})

for yaw in {YAWS!r}:
    # PITFALL - unreal.Rotator's positional order is (roll, pitch, yaw), NOT the
    # (pitch, yaw, roll) that FRotator prints and that vget returns. Passing yaw
    # positionally in slot 2 sets PITCH: at 'yaw 270' the camera looked straight down and
    # measured the floor at 1.690 m, which is exactly the 169 cm eye height. Depth that is
    # correct for the wrong orientation is the hardest kind of wrong to notice.
    rot = unreal.Rotator(roll=0.0, pitch=0.0, yaw=yaw)
    out = "/home/ue4/smoke_depth_%03d.exr" % int(yaw)
    try:
        j = _json.loads(str(unreal.SimWorldCapture.capture_depth_exr(
            w, loc, rot, {FOV}, {W}, {H}, out, 1000.0, True)))
    except Exception as e:
        j = {{"ok": False, "error": "%s: %s" % (type(e).__name__, e)}}
    # independent ground truth for the centre pixel
    h = seg(loc.x, loc.y, loc.z,
            loc.x + math.cos(math.radians(yaw)) * 100000.0,
            loc.y + math.sin(math.radians(yaw)) * 100000.0, loc.z)
    j["trace_m"] = None if h is None else round(h["distance"] / 100.0, 4)
    j["yaw"] = yaw
    res["depth"].append(j)

def call(fn, *a):
    try:
        return _json.loads(str(fn(w, *a)))
    except Exception as e:
        return {{"ok": False, "error": "%s: %s" % (type(e).__name__, e)}}

res["nav"] = call(unreal.SimWorldCapture.ensure_nav_mesh,
                  unreal.Vector({X}, {Y}, {FLOOR}),
                  unreal.Vector(6000.0, 6000.0, 2000.0), 200.0, 240.0)
if res["nav"].get("ok"):
    res["project"] = call(unreal.SimWorldCapture.project_to_nav,
                          unreal.Vector({X}, {Y}, {FLOOR}),
                          unreal.Vector(300.0, 300.0, 500.0))
    res["path"] = call(unreal.SimWorldCapture.find_nav_path,
                       unreal.Vector({X}, {Y}, {FLOOR}),
                       unreal.Vector({X} + 900.0, {Y} + 900.0, {FLOOR}))
    res["export"] = call(unreal.SimWorldCapture.export_nav_mesh,
                         "/home/ue4/smoke_navmesh.bin", "/home/ue4/smoke_navmesh.json")

RESULT.update(res)
"""


def main():
    ucv = engine.connect(1280, 720, timeout=900)
    if ucv is None:
        print("[smoke] UE not reachable")
        return 2
    t0 = time.time()
    r = engine.query(ucv.client.request, BODY, timeout=900, poll=1.0)
    print(f"[smoke] returned after {time.time()-t0:.0f}s\n")

    print("DEPTH  centre pixel vs an independent line trace along the same axis")
    print(f"  {'yaw':>6} {'centre_m':>9} {'trace_m':>9} {'diff_cm':>8} {'valid%':>7} "
          f"{'min_m':>7} {'max_m':>8}  actual_yaw")
    agree = 0
    for d in r.get("depth", []):
        if not d.get("ok"):
            print(f"  {d.get('yaw'):>6} FAILED: {d.get('error')}")
            continue
        c, t = d["centre_m"], d.get("trace_m")
        diff = None if (t is None or c <= 0) else abs(c - t) * 100.0
        agree += 1 if (diff is not None and diff < 15.0) else 0
        print(f"  {d['yaw']:>6.1f} {c:>9.3f} "
              f"{('none' if t is None else f'{t:9.3f}')} "
              f"{('   n/a' if diff is None else f'{diff:8.1f}')} "
              f"{d['invalid_fraction']*-100+100:>7.1f} {d['min_m']:>7.3f} {d['max_m']:>8.3f}"
              f"  {d['actual_rotation_pyr'][1]:.2f}")
    print(f"\n  headings where centre and trace agree within 15 cm: {agree}/"
          f"{len(r.get('depth', []))}")

    for k in ("nav", "project", "path", "export"):
        if k in r:
            v = dict(r[k])
            v.pop("points", None)
            print(f"\n{k.upper()}: {json.dumps(v)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
