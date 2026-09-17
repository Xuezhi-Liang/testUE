#!/usr/bin/env python3
"""Everything that talks to UE, in one place.

Two channels, because neither alone is sufficient on this build:

  UnrealCV  RGB frames, actor/camera transforms, spawning, console commands.
  in-editor Python (`vrun py <file>`)  collision traces and ground heights, which UnrealCV
            does not expose. Console commands only ever return "ok", so a query writes its
            answer to a file that is read back.

  SimWorldCapture (C++, gym_citynavRuntime)  depth and navmesh - see cpp/SimWorldCapture.cpp

Known limits of this build, all measured rather than assumed:
  - `vget /camera/N/depth npy` returns a constant 65504 (fp16 max) and reading a render target
    from editor Python asserts in VulkanRenderTarget.cpp. The cause is `ReadLinearColorPixels`,
    which is not a float path on Vulkan - NOT the RHI. Depth therefore comes from
    `SimWorldCapture::CaptureDepthEXR`, which reads back through FRHIGPUTextureReadback and was
    verified against line traces to 0.0-0.4 cm at close range.
  - the map ships no navmesh, so `SimWorldCapture::EnsureNavMesh` synthesises a bounds volume and
    builds one. Capsule sweeps here trace TRACE_TYPE_QUERY1 (the Visibility channel), which is
    NOT what blocks a Pawn, so they must not be the last word on whether a route is walkable.
  - `vget /object/<n>/vertex_locations` crashes the editor. Never call it.
"""
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))          # for simworld.communicator.unrealcv
PORT = int(os.environ.get("PORT", "9208"))
CAM_HOST = os.environ.get(
    "CAM_HOST",
    "/Game/TrafficSystem/Pedestrian/Base_User_Agent_Camera_Control."
    "Base_User_Agent_Camera_Control_C")
CTR = os.environ.get("CTR_PID", "61703")
IN_CTR = "/home/ue4/pipe_query.py"
OUT_CTR = "/home/ue4/pipe_query.json"
_INSIDE = Path("/home/ue4").is_dir()


def connect(width, height, timeout=900.0):
    from simworld.communicator.unrealcv import UnrealCV
    t0 = time.time()
    c = None
    while time.time() - t0 < timeout:
        if c is None:
            try:
                c = UnrealCV(port=PORT, ip="127.0.0.1", resolution=(width, height),
                             rpc_timeout=60.0)
            except Exception:
                c = None
                time.sleep(8)
                continue
        try:
            if str(c.client.request("vget /unrealcv/status")):
                return c
        except Exception:
            time.sleep(5)
    return None


# --------------------------------------------------------------------------- in-editor Python
_PREAMBLE = '''
import json, math, unreal
w = unreal.EditorLevelLibrary.get_game_world()
V = unreal.Vector
CH = unreal.TraceTypeQuery.TRACE_TYPE_QUERY1
DBG = unreal.DrawDebugTrace.NONE

def brk(r):
    """HitResult properties are protected and GameplayStatics has no break_hit_result on this
    build, so to_dict() is the only working accessor."""
    if r is None:
        return None
    d = r.to_dict()
    ip = d["impact_point"]
    a = d.get("hit_actor")
    return {"distance": float(d["distance"]), "impact": [ip.x, ip.y, ip.z],
            "actor": a.get_name() if a else None}

def seg(ax, ay, az, bx, by, bz):
    return brk(unreal.SystemLibrary.line_trace_single(
        w, V(ax, ay, az), V(bx, by, bz), CH, True, [], DBG, True))

def sweep_seg(ax, ay, az, bx, by, bz, radius, half_height):
    return brk(unreal.SystemLibrary.capsule_trace_single(
        w, V(ax, ay, az), V(bx, by, bz), radius, half_height, CH, False, [], DBG, True))

def ground(x, y, z_from, drop=4000.0):
    h = seg(x, y, z_from, x, y, z_from - drop)
    return None if h is None else h["impact"][2]

RESULT = {}
'''


def _write(path, text):
    if _INSIDE:
        Path(path).write_text(text)
    else:
        tmp = HERE / "_query.py"
        tmp.write_text(text)
        subprocess.run(["enroot", "exec", CTR, "bash", "-lc", f"cp {tmp} {path}"],
                       capture_output=True, text=True, timeout=120)


def _read(path):
    if _INSIDE:
        p = Path(path)
        return p.read_text() if p.exists() else ""
    r = subprocess.run(["enroot", "exec", CTR, "bash", "-lc", f"cat {path} 2>/dev/null"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout


def _rm(path):
    if _INSIDE:
        Path(path).unlink(missing_ok=True)
    else:
        subprocess.run(["enroot", "exec", CTR, "bash", "-lc", f"rm -f {path}"],
                       capture_output=True, text=True, timeout=120)


def query(req, body, timeout=300, poll=0.4):
    """Run in-editor Python that fills RESULT, and return it.

    The answer file is deleted first: a stale file read as a fresh answer is silent and yields
    confidently wrong geometry.
    """
    _rm(OUT_CTR)
    _write(IN_CTR, _PREAMBLE + body + f'\nopen("{OUT_CTR}","w").write(json.dumps(RESULT))\n')
    req(f"vrun py {IN_CTR}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = _read(OUT_CTR).strip()
        if s:
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
        time.sleep(poll)
    raise TimeoutError(f"no answer from the editor within {timeout}s (did it crash?)")


def sweep_rays(req, origin, legs, radius=40.0, half_height=88.0):
    """legs: [(yaw_deg, dist_cm)] from one origin. One round trip for the whole list."""
    x, y, z = origin
    body = f"L = {json.dumps([[float(a), float(b)] for a, b in legs])}\n"
    body += f"""
RESULT["legs"] = []
for yaw, dist in L:
    ax, ay, az = {x}, {y}, {z} + {half_height}
    bx = ax + math.cos(math.radians(yaw)) * dist
    by = ay + math.sin(math.radians(yaw)) * dist
    h = sweep_seg(ax, ay, az, bx, by, az, {radius}, {half_height})
    RESULT["legs"].append({{"clear": h is None,
                            "blocked_at_cm": None if h is None else h["distance"],
                            "actor": None if h is None else h["actor"]}})
"""
    return query(req, body)["legs"]


def ground_z(req, pts, z_from):
    body = f"P = {json.dumps([[float(a), float(b)] for a, b in pts])}\n"
    body += f'RESULT["z"] = [ground(px, py, {z_from}) for px, py in P]\n'
    return query(req, body)["z"]


def validate_path(req, pts_xyz, radius=40.0, half_height=88.0, near_cm=10.0,
                  clearance_yaws=(0.0,), chunk=400):
    """Per-frame collision evidence for spec section 11.

    For each consecutive pair: a capsule sweep (body did not pass through geometry) and the
    forward clearance from that frame. Checking only the camera centre is not enough - the near
    plane can cut into a wall while the centre is still outside it - so the four near-plane
    corners are traced too.
    """
    out = {"sweep_hits": [], "clearance_cm": [], "near_plane_hits": []}
    for s in range(0, len(pts_xyz), chunk):
        part = pts_xyz[s:s + chunk + 1]
        if len(part) < 2:
            break
        body = f"P = {json.dumps([[float(v) for v in p] for p in part])}\n"
        body += f"""
RESULT["sweep"] = []
RESULT["clear"] = []
RESULT["near"] = []
for i in range(len(P) - 1):
    a, b = P[i], P[i + 1]
    h = sweep_seg(a[0], a[1], a[2], b[0], b[1], b[2], {radius}, {half_height})
    RESULT["sweep"].append(None if h is None else
                           {{"at_cm": h["distance"], "actor": h["actor"]}})
for i in range(len(P) - 1):
    a = P[i]
    yaw = math.degrees(math.atan2(P[i+1][1]-a[1], P[i+1][0]-a[0]))
    bx = a[0] + math.cos(math.radians(yaw)) * 1500.0
    by = a[1] + math.sin(math.radians(yaw)) * 1500.0
    h = seg(a[0], a[1], a[2], bx, by, a[2])
    RESULT["clear"].append(None if h is None else h["distance"])
    n = 0
    for dx, dy in ((1,1),(1,-1),(-1,1),(-1,-1)):
        c = seg(a[0], a[1], a[2],
                a[0] + dx * {near_cm}, a[1] + dy * {near_cm}, a[2])
        if c is not None and c["distance"] <= {near_cm}:
            n += 1
    RESULT["near"].append(n)
"""
        r = query(req, body)
        # Say what came back instead of raising KeyError on it. The in-editor script sets these
        # three keys as its first statements, so a response without them is not a missing value -
        # it is a different answer than the one that was asked for, and the only thing that can
        # tell us which is its content. A bare `KeyError: 'sweep'` swallowed it on WinterTown and
        # cost a diagnosis.
        missing = [k for k in ("sweep", "clear", "near") if k not in r]
        if missing:
            raise RuntimeError(
                f"validate_path chunk at frame {s} (of {len(pts_xyz)}) came back without "
                f"{missing}; the editor answered with keys {sorted(r)}: "
                f"{json.dumps(r)[:400]}")
        if not (len(r["sweep"]) == len(r["clear"]) == len(r["near"]) == len(part) - 1):
            raise RuntimeError(
                f"validate_path chunk at frame {s}: asked about {len(part) - 1} frame pairs and "
                f"got {len(r['sweep'])} sweeps, {len(r['clear'])} clearances, "
                f"{len(r['near'])} near-plane counts")
        out["sweep_hits"].extend(r["sweep"])
        out["clearance_cm"].extend(r["clear"])
        out["near_plane_hits"].extend(r["near"])
    return out


# --------------------------------------------------------------------------------------- rig
class Rig:
    """A spawned pawn whose head-mounted camera is what we record.

    The pawn exists only to own a real CameraComponent - the frozen trajectory is executed by
    placing it, not by walking it, because a walked character is at the mercy of geometry it
    can wedge against and that destroys determinism.
    """

    def __init__(self, ucv, width, height, fov):
        self.ucv = ucv
        self.req = ucv.client.request
        self.width, self.height, self.fov = width, height, fov
        for cvar in ("Editor.AsyncSkinnedAssetCompilation 0",
                     "Editor.AsyncAssetCompilation 0"):
            try:
                self.req(f"vrun {cvar}")
            except Exception:
                pass
        for stale in [o for o in str(self.req("vget /objects")).split()
                      if o.startswith("PIPE_")]:
            self.req(f"vset /object/{stale}/destroy")
        self.name = f"PIPE_{int(time.time())}"
        self.req(f"vset /objects/spawn_bp_asset {CAM_HOST} {self.name}")
        time.sleep(4.0)
        self.req(f"vbp {self.name} SetInterpSpeed 0.0")
        self.cam = None

    # ------------------------------------------------------------------ readback
    def pawn_loc(self):
        """Where the camera host actually is.

        `vget /object/<name>/location` resolves the name through UnrealCV's own object registry, and
        that registry is built once, during "Annotate mesh of the scene" at startup. Our pawn is
        spawned after that, so on some maps the lookup returns the literal string "error" even
        though the spawn succeeded and `vbp <name> ...` works on it - MiddleEast and WildWest both
        failed here with `could not convert string to float: 'error'` a second into the run.

        The fallback asks the editor directly, which needs no registry. It is a fallback rather than
        the default because it costs a `vrun py` round trip instead of a plain request.
        """
        raw = self.req(f"vget /object/{self.name}/location")
        parts = raw.split()[:3]
        try:
            return [float(v) for v in parts]
        except ValueError:
            pass
        body = (
            "import unreal as U\n"
            "w = U.EditorLevelLibrary.get_game_world()\n"
            f"m = [a for a in U.GameplayStatics.get_all_actors_of_class(w, U.Actor)\n"
            f"     if '{self.name}' in str(a.get_name())]\n"
            "if not m:\n"
            "    RESULT.update({'ok': False, 'error': 'actor not found in the world either'})\n"
            "else:\n"
            "    l = m[0].get_actor_location()\n"
            "    RESULT.update({'ok': True, 'loc': [float(l.x), float(l.y), float(l.z)]})\n"
        )
        r = query(self.req, body, timeout=120)
        if not r.get("ok"):
            raise RuntimeError(
                f"could not read the pawn location: UnrealCV returned {raw!r} and the editor "
                f"query said {r.get('error')}")
        return r["loc"]

    def cam_pose(self):
        """Actual CameraComponent world transform - spec section 7 requires this, not the
        pawn's or the spring arm's.

        The camera index can stop resolving mid-run (the request answers "error"), so it is
        re-found once before giving up rather than aborting a capture over a transient.
        """
        for attempt in (0, 1):
            try:
                loc = [float(v) for v in
                       self.req(f"vget /camera/{self.cam}/location").split()[:3]]
                rot = [float(v) for v in
                       self.req(f"vget /camera/{self.cam}/rotation").split()[:3]]
                return loc, rot              # rot is (pitch, yaw, roll)
            except (ValueError, IndexError):
                if attempt:
                    raise
                self.cam = self.find_camera()

    def find_camera(self):
        pl = self.pawn_loc()
        cams = str(self.ucv.get_cameras() or "").split()
        best, bd = None, 1e18
        for i in range(len(cams) + 2):
            try:
                cl = [float(v) for v in
                      self.req(f"vget /camera/{i}/location").split()[:3]]
            except Exception:
                continue
            d = math.dist(cl, pl)
            if d < bd:
                best, bd = i, d
        if best is None or bd > 700:
            raise RuntimeError(f"no camera near {self.name}; closest {best} at {bd:.0f} cm")
        return best

    def configure(self):
        self.cam = self.find_camera()
        self.req(f"vset /camera/{self.cam}/size {self.width} {self.height}")
        self.req(f"vset /camera/{self.cam}/fov {self.fov}")
        time.sleep(0.6)
        return float(str(self.req(f"vget /camera/{self.cam}/fov")))

    def calibrate_camera_offset(self, probe_xy, floor_z, yaw=0.0):
        """Measure where the camera sits relative to the commanded pawn location.

        Measured rather than taken from the blueprint: the head offset is what converts a
        desired camera pose into the pawn placement that produces it.
        """
        self.place_pawn(probe_xy[0], probe_xy[1], floor_z + 88.0, yaw, 0.0)
        time.sleep(0.8)
        pl = self.pawn_loc()
        cl, _ = self.cam_pose()
        return [cl[i] - pl[i] for i in range(3)]

    def measure_eye_height(self, probe_xy, floor_z, yaw=0.0, settle_s=1.5):
        """Eye height above the floor that this rig actually produces.

        This is not a free parameter. The pawn is a character, so its movement component snaps
        the capsule to the floor: commanding a lower z does not lower the camera, it just leaves
        the pose 9 cm off and the readback disagreeing with the request. The achievable height is
        the capsule's standing height plus the head offset, so it is measured here and the frozen
        trajectory is built at that height - making desired and actual agree by construction.
        """
        self.place_pawn(probe_xy[0], probe_xy[1], floor_z + 88.0, yaw, 0.0)
        time.sleep(settle_s)
        last = None
        for _ in range(20):                  # wait for the capsule to come to rest
            cl, _ = self.cam_pose()
            if last is not None and abs(cl[2] - last) < 0.05:
                break
            last = cl[2]
            time.sleep(0.25)
        cl, _ = self.cam_pose()
        pl = self.pawn_loc()
        return cl[2] - floor_z, [cl[i] - pl[i] for i in range(3)]

    # ------------------------------------------------------------------- actions
    def place_pawn(self, x, y, z, yaw, pitch):
        self.req(f"vset /object/{self.name}/location {x:.3f} {y:.3f} {z:.3f}")
        self.req(f"vset /object/{self.name}/rotation 0 {yaw % 360:.3f} 0")
        self.req(f"vbp {self.name} RotateCameraArm {pitch:.3f} {yaw % 360:.3f}")

    def place_camera(self, target_xyz, yaw, pitch, cam_offset,
                     corrections=int(os.environ.get("PLACE_CORRECTIONS", "2"))):
        """Put the CameraComponent on `target_xyz`, correcting only in x and y.

        z is deliberately left to the character's movement component. It snaps the capsule to the
        floor, so correcting z means fighting it: the correction is undone every frame and the
        pose error never converges. The frozen trajectory is built at the rig's measured eye
        height instead, which makes the z agree without any correction at all.

        Two correction passes rather than one: with a single pass the residual stayed under a
        millimetre on most headings but grew to ~12 mm on others, over the 1 cm gate limit, because
        the character can be nudged in x/y between the placement and the readback. Each extra pass
        costs one readback out of a ~285 ms frame.
        """
        gx = target_xyz[0] - cam_offset[0]
        gy = target_xyz[1] - cam_offset[1]
        gz = target_xyz[2] - cam_offset[2]
        self.place_pawn(gx, gy, gz, yaw, pitch)
        best = self.cam_pose()
        best_err = math.dist(best[0], target_xyz)
        best_g = (gx, gy)
        for _ in range(corrections):
            if best_err < 0.05:
                break
            gx += target_xyz[0] - best[0][0]
            gy += target_xyz[1] - best[0][1]
            self.place_pawn(gx, gy, gz, yaw, pitch)
            cur = self.cam_pose()
            err = math.dist(cur[0], target_xyz)
            if err < best_err:
                best, best_err, best_g = cur, err, (gx, gy)
            else:
                # The body is blocked: it did not move, so the next correction would push the
                # commanded position further away and report a larger error than one pass did -
                # that is how two passes turned a 36 mm residual into 208 mm. Keep the best
                # placement and stop rather than chasing a target the body cannot reach.
                self.place_pawn(best_g[0], best_g[1], gz, yaw, pitch)
                break
        return self.cam_pose()

    def grab(self):
        img = self.ucv.get_image(self.cam, "lit", mode="fast")
        if img is None:
            return None
        a = np.asarray(img)
        return None if a.size == 0 else a

    def destroy(self):
        try:
            self.req(f"vset /object/{self.name}/destroy")
        except Exception:
            pass


# ------------------------------------------------------------------ SimWorldCapture (C++)
# These wrap the UFUNCTIONs added in cpp/SimWorldCapture.{h,cpp}. Everything they need that
# UnrealCV and editor Python cannot do lives there: a float-safe depth readback, and building a
# navmesh on a level that ships none.


def simworld(req, fn, args_py, timeout=300):
    """Call a USimWorldCapture function and return its parsed JSON.

    Every entry point returns a JSON string rather than out-params. UE's Python binding packs a
    bool return plus out-params inconsistently - EnsureNavMesh came back as 2 values for 3
    outputs and ProjectToNav dropped its FVector entirely - and guessing the shape cost a run.
    A JSON string has exactly one shape.
    """
    body = (
        "import json as _json\n"
        "try:\n"
        f"    RESULT.update(_json.loads(str(unreal.SimWorldCapture.{fn}(w, {args_py}))))\n"
        "except Exception as e:\n"
        '    RESULT.update({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})\n'
    )
    return query(req, body, timeout=timeout)


def V(x, y, z):
    return f"unreal.Vector({float(x):.4f}, {float(y):.4f}, {float(z):.4f})"


def R(yaw, pitch=0.0, roll=0.0):
    """Build an unreal.Rotator by keyword, always.

    PITFALL - the positional order is (roll, pitch, yaw), NOT the (pitch, yaw, roll) that
    FRotator prints and that `vget /camera/N/rotation` returns. Passing yaw positionally sets
    PITCH: it aimed a depth capture straight down and produced a frame of perfectly plausible
    1.690 m readings, which happened to equal the camera's own eye height. Nothing errored.
    """
    return (f"unreal.Rotator(roll={float(roll):.4f}, pitch={float(pitch):.4f}, "
            f"yaw={float(yaw):.4f})")


def nav_ensure(req, centre_xy, floor_z, extent=6000.0, padding=200.0, timeout_s=240.0,
               settle_polls=5, poll_s=2.0):
    """Build navigation data covering the region, synthesising a bounds volume if the level has none.

    Dispatches the build and then POLLS. The engine must tick for the build to progress, so waiting
    for it inside the call is not just slow, it is wrong: ticking the navigation system from a
    console command that runs inside the engine tick re-enters it, and that hung one map for 42
    minutes at 584% CPU with a silent log and no timeout.

    "Not in progress" alone is not "finished" - an unstarted build also reports idle - so idle has
    to hold for several consecutive polls with a navmesh present.
    """
    import time as _t
    r = simworld(req, "ensure_nav_mesh",
                 f"{V(centre_xy[0], centre_xy[1], floor_z)}, "
                 f"{V(extent, extent, 2000.0)}, {padding}, {timeout_s}", timeout=300)
    if not r.get("ok"):
        return r
    idle, t0 = 0, _t.time()
    last = {}
    while _t.time() - t0 < timeout_s:
        last = simworld(req, "nav_build_status", "", timeout=120)
        if not last.get("ok"):
            return last
        idle = idle + 1 if (not last["in_progress"] and last["has_navmesh"]) else 0
        if idle >= settle_polls:
            break
        _t.sleep(poll_s)
    r.update({"in_progress": last.get("in_progress"), "has_navmesh": last.get("has_navmesh"),
              "agent_radius_cm": last.get("agent_radius_cm"),
              "agent_height_cm": last.get("agent_height_cm"),
              "settled": idle >= settle_polls, "build_wait_s": round(_t.time() - t0, 1)})
    if not last.get("has_navmesh"):
        r["ok"] = False
        r["error"] = (f"no navmesh after {r['build_wait_s']}s of building - the level may have no "
                      f"walkable surface in the region")
    return r


def nav_project(req, xyz, extent=(300.0, 300.0, 500.0)):
    return simworld(req, "project_to_nav", f"{V(*xyz)}, {V(*extent)}")


def nav_path(req, start_xyz, goal_xyz):
    """A path the agent's own radius and step height can actually walk.

    `partial` true means it stops at the closest reachable point - which is precisely where a
    route would silently stop short - so a caller must reject it rather than use the points.
    """
    return simworld(req, "find_nav_path", f"{V(*start_xyz)}, {V(*goal_xyz)}")


def nav_export(req, bin_path, json_path):
    return simworld(req, "export_nav_mesh", f'"{bin_path}", "{json_path}"')


def rgb_capture(req, xyz, yaw, pitch, fov, width, height, out_path, exposure_bias_ev=0.0,
                manual_exposure=False, le_shadow=0.0, le_highlight=0.0, le_detail=0.0):
    """One tonemapped RGB frame as PNG at a stated exposure, with its histogram summary.

    `manual_exposure=False` is what the level's own auto-exposure gives a cold view - the
    reference for "what we have been recording". The returned mean / p10 / p50 / p99 / blown /
    near-black are computed in the engine, so a bias sweep is ranked without decoding PNGs.
    """
    return simworld(req, "capture_rgb_png",
                    f"{V(*xyz)}, {R(yaw, pitch)}, {float(fov)}, {int(width)}, {int(height)}, "
                    f'"{out_path}", {float(exposure_bias_ev)}, {str(bool(manual_exposure))}, '
                    f'{float(le_shadow)}, {float(le_highlight)}, {float(le_detail)}')


def depth_capture(req, xyz, yaw, pitch, fov, width, height, out_path,
                  max_range_m=1000.0, float16=True):
    """Scene depth as EXR, linear metres in R, -1 for sky and beyond range.

    The returned JSON carries valid fraction, min, max and the centre pixel, measured inside the
    engine, so depth can be verified without depending on the caller's EXR reader - ours was an
    opencv build with `OpenEXR: NO`, which made a perfectly good file read as None.
    """
    return simworld(req, "capture_depth_exr",
                    f"{V(*xyz)}, {R(yaw, pitch)}, {float(fov)}, {int(width)}, {int(height)}, "
                    f'"{out_path}", {float(max_range_m)}, {str(bool(float16))}')
