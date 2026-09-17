

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
