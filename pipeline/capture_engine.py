#!/usr/bin/env python3
"""Run a frozen trajectory inside the engine tick, then assemble the episode from what it wrote.

This replaces the per-frame Python loop in `capture.py`. That loop was not slow because of
rendering: measured per call at 1280x720, a bare UnrealCV request costs ~18 ms whatever it does,
`place_camera` was 128 ms of pure protocol, and a frame totalled 531 ms of which roughly half was
waiting on a socket. Here Python sends one command, polls a status line, and reads the results.

What the engine writes (see cpp/SimWorldCaptureActor.cpp):
    depth/%06d.exr        linear metres in R, -1 for sky and beyond range
    rgb/%06d.jpg          per-frame JPEG; the mp4 is assembled here for review
    engine_states.jsonl   one line per frame: desired and actual pose, quaternion, depth stats

Per-frame images rather than only a video, deliberately: ordinary inter-frame coding reconstructs
frame t from frame t-1, which is the task a world model is being trained on, so the codec would be
doing a crude version of it inside the data.
"""
import argparse
import csv
import json
import os
import math
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
import geom  # noqa: E402

OUT_ROOT = HERE / "episodes"

FIELDS = ["frame_id", "episode_time_s", "phase",
          "desired_x_cm", "desired_y_cm", "desired_z_cm",
          "desired_yaw_deg", "desired_pitch_deg", "desired_roll_deg",
          "actual_x_cm", "actual_y_cm", "actual_z_cm",
          "actual_yaw_deg", "actual_pitch_deg", "actual_roll_deg",
          "pos_error_cm", "yaw_error_deg", "step_cm", "speed_m_s", "yaw_rate_deg_s",
          "quat_x", "quat_y", "quat_z", "quat_w", "c2w",
          "depth_path", "depth_bit_depth", "depth_valid_fraction",
          "depth_min_m", "depth_max_m", "depth_centre_m", "rgb_path"]


# Above this many frames the review video is a sampled proxy, not the episode at full rate.
# A 20 h episode is 1.73 M frames: a full-rate x264 encode of it is ~110 GB and hours of CPU,
# which blows the timeout below, and nobody reviews 20 hours of footage anyway. The mp4 is a
# review artifact - the uploader excludes it (`--exclude rgb.mp4`) and the acceptance gates read
# the delivered JPEGs - so sampling it costs the dataset nothing.
FULL_RATE_MAX_FRAMES = 120_000       # 83 min at 24 fps
PROXY_TARGET_FRAMES = 4_320          # 3 min at 24 fps


def assemble_mp4(ep, fps, width, height, total, full_rate_max=FULL_RATE_MAX_FRAMES):
    """Build the review video from the per-frame JPEGs, H.264 with the moov atom up front.

    cv2's mp4v writes MPEG-4 Part 2 with moov at the end, which browsers refuse to play.

    Returns (filename, stride). A stride above 1 means the file is `rgb_proxy.mp4`, a timelapse
    of every stride-th frame, and the name says so - a sampled video called rgb.mp4 would be a
    file that silently disagrees with frames.csv about what frame 100 is.
    """
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if total <= full_rate_max:
        out = ep / "rgb.mp4"
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
               "-framerate", str(fps), "-i", str(ep / "rgb" / "%06d.jpg"),
               "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
        return out.name, 1

    stride = max(2, -(-total // PROXY_TARGET_FRAMES))
    out = ep / "rgb_proxy.mp4"
    # A concat list, not a select filter: `-vf select` still decodes every input frame, which is
    # the cost we are avoiding. This decodes only the frames that end up in the proxy.
    lst = ep / "_proxy_frames.txt"
    with open(lst, "w") as fh:
        for i in range(0, total, stride):
            fh.write(f"file 'rgb/{i:06d}.jpg'\n")
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-r", str(fps), "-i", str(lst),
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=3600, cwd=str(ep))
    lst.unlink(missing_ok=True)
    return out.name, stride


DYNAMIC_SKY = """
import unreal as U
w = U.EditorLevelLibrary.get_game_world()
changed, seen, skipped = [], [], []
has_atmosphere = any(len(a.get_components_by_class(U.SkyAtmosphereComponent)) > 0
                     for a in U.GameplayStatics.get_all_actors_of_class(w, U.Actor))
for a in U.GameplayStatics.get_all_actors_of_class(w, U.Actor):
    if a.actor_has_tag("SimWorldFillSky") or a.actor_has_tag("SimWorldRig"):
        skipped.append(str(a.get_name()))     # ours; never re-tune what the rig/fill spawned
        continue
    if "SkyLight" not in str(a.get_class().get_name()):
        continue
    c = a.get_component_by_class(U.SkyLightComponent)
    if c is None:
        continue
    mob = str(c.get_editor_property("mobility"))
    rtc = bool(c.get_editor_property("real_time_capture"))
    src = str(c.get_editor_property("source_type"))
    seen.append({"actor": str(a.get_name()), "mobility": mob, "real_time_capture": rtc, "source_type": src})
    if "MOVABLE" in mob.upper() and (rtc == has_atmosphere):
        continue
    c.set_editor_property("mobility", U.ComponentMobility.MOVABLE)
    # Real-time capture renders only the sky atmosphere, clouds and height fog into the cubemap.
    # On a level whose sky is a painted sphere mesh (most purchased maps) it captures BLACK, and
    # the sky light contributes nothing whatever its intensity - measured on Downtown West, where
    # the x1/x3/x6 fill sweep rendered identical frames until the source became captured-scene.
    # So: real-time capture only where there is an atmosphere to capture; otherwise photograph the
    # scene (the painted sky included) once with RecaptureSky.
    c.set_editor_property("real_time_capture", bool(has_atmosphere))
    if not has_atmosphere:
        c.set_editor_property("source_type", U.SkyLightSourceType.SLS_CAPTURED_SCENE)
    try:
        c.recapture_sky()
    except Exception:
        pass
    changed.append(str(a.get_name()))
RESULT.update({"ok": True, "skylights": seen, "made_dynamic": changed, "skipped_ours": skipped,
               "has_atmosphere": has_atmosphere,
               "mode": "real_time_capture" if has_atmosphere else "captured_scene"})
""".strip()


def ensure_dynamic_sky(req):
    """Give the level a skylight that contributes without baked lighting.

    A Static or Stationary skylight bakes its indirect contribution into lightmaps. None of these
    maps ship built lighting for our session, so that contribution is simply absent: direct sunlight
    renders correctly and everything in shadow comes out at zero. Measured on MiddleEast, whose
    SkyLight_0 is Stationary with real_time_capture off - 15.4% of the median frame was pure black,
    166 of 237 sampled frames over the 10% gate, and every material in the level compiled fine, so
    it was not the Default-Material fallback that made ModularNeighborhood black. Switching the
    skylight to Movable with real-time capture took the same route to 2.4%, against a 1.6-1.7%
    baseline on maps that were already fine.

    This changes the LEVEL state for the session only - nothing is saved - and what it changed is
    recorded in capture_summary.json, because "the map as shipped" and "the map with a dynamic sky"
    are different provenance.
    """
    r = engine.query(req, DYNAMIC_SKY, timeout=300)
    if not r.get("ok"):
        return {"applied": False, "error": r.get("error")}
    made = r.get("made_dynamic") or []
    if made:
        print(f"[capture] skylight made dynamic ({', '.join(made)}, {r.get('mode')}): the level ships "
              f"Static/Stationary sky lighting and no built lighting data, which renders everything "
              f"outside direct sunlight at zero")
    return {"applied": bool(made), "made_dynamic": made, "mode": r.get("mode"),
            "has_atmosphere": r.get("has_atmosphere"), "skipped_ours": r.get("skipped_ours") or [],
            "skylights_before": r.get("skylights") or [],
            "note": "session-only change to the loaded level; nothing was saved to the map"}


REQUIRED_STATE = ("frame_id", "actual_location", "actual_rotation_pyr", "desired_location",
                  "desired_rotation_pyr", "quat_xyzw", "depth")
REQUIRED_DEPTH = ("bit_depth", "valid_fraction", "min_m", "max_m", "centre_m")


def parse_state(raw):
    """One engine_states.jsonl line as a dict, or None if it cannot become a frames.csv row.

    A capture killed mid-write leaves a torn tail: a half-written line, or complete JSON with a
    field the engine had not filled in yet. Neither may become a row - csv.DictWriter turns a
    missing key into a blank column, and the blank surfaces much later as a TypeError inside an
    acceptance gate that names neither the row nor the file. One recovered episode failed exactly
    that way.

    Shared with longvideo/recover.py so the frame count it decides on and the rows finalise can
    actually write are the same number, decided by the same code.
    """
    try:
        s = json.loads(raw)
        for k in REQUIRED_STATE:
            if s.get(k) is None:
                return None
        for k in REQUIRED_DEPTH:
            if s["depth"].get(k) is None:
                return None
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
        return None
    return s


def capture(frozen_path, out_root=OUT_ROOT, ucv=None, poll=2.0, on_poll=None):
    """Capture one frozen trajectory.

    on_poll, if given, is called once per status poll. The capture itself runs entirely inside the
    engine, so this is the only place anything Python can act DURING a clip - crowd.Crowd.tick()
    uses it to renew the duration-based walk commands that keep NPCs moving while filming.
    """
    fz = json.loads(Path(frozen_path).read_text())
    task = fz["task"]
    fps = float(fz["fps"])
    W, H = task["camera"]["resolution"]
    fov = float(fz["camera_fov_deg_actual"])
    ep = out_root / fz["episode_id"]
    ep.mkdir(parents=True, exist_ok=True)

    if ucv is None:
        ucv = engine.connect(W, H)
        if ucv is None:
            raise RuntimeError("UE never became reachable")
    req = ucv.client.request

    lighting = ensure_dynamic_sky(req)
    # render.lighting "rig" (or LIGHTING_RIG=1): hide the level's own global lighting and light it
    # with the fixed Dubai rig, auto-exposure off for the session - see lighting_rig.py.
    rend0 = (fz.get("task") or {}).get("render") or {}
    calibrated_manual_bias = None
    lmode = os.environ.get("LIGHTING_MODE") or ("rig" if os.environ.get("LIGHTING_RIG") == "1" else str(rend0.get("lighting", "level")))
    if lmode == "rig":
        import lighting_rig
        lighting = {"skylight_fix": lighting, "rig": lighting_rig.apply_rig(req)}
        if not lighting["rig"].get("applied"):
            raise RuntimeError(f"lighting rig failed to apply: {lighting['rig'].get('error')}")
    elif lmode == "fill":
        # keep the author's lighting and grading, multiply their sky light (lighting_rig.apply_fill)
        import lighting_rig
        factor_raw = os.environ.get("SKYLIGHT_FACTOR") or rend0.get("skylight_factor", "auto")
        calib = None
        if str(factor_raw).lower() == "auto":
            # per-map: the smallest boost that meets the quality bar, and its exposure bias, from
            # rendered histograms on route poses (lighting_calibrate). Fixed for the whole episode.
            import lighting_calibrate
            calib = lighting_calibrate.calibrate(req, fz, ep / "_lighting_calibration", lighting=lighting)
            if calib.get("chosen_factor") is None:
                raise RuntimeError("lighting calibration found no usable sky-light factor; refusing to record")
            factor = float(calib["chosen_factor"])
            # The sweep uses MANUAL exposure. Its bias is not an auto-exposure offset.
            # Keep it local so it cannot leak into auto_instant or a later capture.
            calibrated_manual_bias = float(calib["chosen_ev"])
            fill = {"applied": True, "factor": factor, "from_calibration": True}
        else:
            factor = float(factor_raw)
            fill = lighting_rig.apply_fill(req, factor)
            if not fill.get("applied"):
                raise RuntimeError(f"sky-light fill failed to apply: {fill.get('error')}")
        lighting = {"skylight_fix": lighting, "fill": fill, "calibration": calib}
    elif lmode != "level":
        raise RuntimeError(f"unknown render.lighting {lmode!r}; one of level, fill, rig")

    # `render.exposure: "fixed"` has been in every task since the first episode and was applied
    # by nothing - the actor now takes it. Manual exposure pins the RGB capture for the whole
    # episode; the bias is per map (render.exposure_bias_ev, default 0), chosen by exposure_probe.py
    # from the histogram the level produces. Anything other than "fixed" leaves the level's own
    # auto-exposure in charge, and the actor logs a warning saying so.
    rend = (fz.get("task") or {}).get("render") or {}
    # Session console variables select an AA method, but cannot enable the capture's TemporalAA
    # show flag. History mode 4 does that in C++; modes 0..3 retain their legacy behavior.
    cvars = list(rend.get("cvars") or [])
    if os.environ.get("CAPTURE_CVARS"):
        cvars += [c.strip() for c in os.environ["CAPTURE_CVARS"].split(";") if c.strip()]
    for cmd in cvars:
        r = engine.query(req, f'unreal.SystemLibrary.execute_console_command(w, {cmd!r})\nRESULT.update({{"ok": True}})')
        print(f"[capture] cvar: {cmd} -> {'ok' if r.get('ok') else r.get('error')}")
    # exposure modes: "fixed" = manual exposure on the capture at a per-map bias; "off" = the
    # level's auto-exposure disabled for the session (the rig does this) and nothing else pinned;
    # anything else = the level's own auto-exposure, unpinned (what every episode before 14 Sep got).
    mode = str(os.environ.get("EXPOSURE_MODE") or rend.get("exposure", "fixed")).lower()
    if os.environ.get("EXPOSURE_AUTO") == "1":
        mode = "auto"
    if mode == "off" and not (isinstance(lighting, dict) and lighting.get("rig", {}).get("applied")):
        for cmd in ("r.DefaultFeature.AutoExposure 0", "r.EyeAdaptationQuality 0"):
            engine.query(req, f'unreal.SystemLibrary.execute_console_command(w, "{cmd}")\nRESULT.update({{"ok": True}})')
    manual = mode == "fixed"
    # "auto_instant": the level's own metering with the adaptation lag removed (speed ~100 EV/s)
    # and the range clamped; the brightness is then a function of the current frame only.
    ae_speed = float(os.environ.get("AE_SPEED") or rend.get("auto_exposure_speed", 100.0)) if mode == "auto_instant" else 0.0
    ae_range = rend.get("auto_exposure_range") or [0.0, 0.0]
    if os.environ.get("AE_RANGE"):
        ae_range = [float(x) for x in os.environ["AE_RANGE"].split(",")]
    le = rend.get("local_exposure") or {}
    le_shadow = float(os.environ.get("LE_SHADOW") or le.get("shadow_scale", 0.0))
    le_highlight = float(os.environ.get("LE_HIGHLIGHT") or le.get("highlight_scale", 0.0))
    le_detail = float(os.environ.get("LE_DETAIL") or le.get("detail_strength", 0.0))
    exposure_env = os.environ.get("EXPOSURE_BIAS_EV")   # test hook: override the task
    bias_raw = exposure_env if exposure_env is not None else rend.get("exposure_bias_ev")
    if manual and exposure_env is None and calibrated_manual_bias is not None and bias_raw in (None, "auto"):
        bias_raw = calibrated_manual_bias
    sweep_report = None
    if manual and str(bias_raw).lower() == "auto":
        # Measure the bias on THIS map, in this session, with the lighting the recording will
        # have: exposure_probe.sweep renders a dozen route poses at a coarse then fine EV grid
        # and picks the bias losing the fewest pixels at both ends. 20-60 s, one connection.
        import exposure_probe
        table, ev = exposure_probe.sweep(req, fz, ep / "_exposure_sweep", n=12, lighting=lighting)
        if ev is None:
            raise RuntimeError("exposure sweep found no bias within limits for this map; refusing to record")
        bias_raw = ev
        sweep_report = {"chosen_ev": ev, "table": table, "poses": 12}
    if manual and bias_raw is None:
        # Manual exposure meters from the default camera (EV100 ~ 9.9). A purchased level is lit
        # in whatever units its author liked - Downtown West needed +11.5 EV and rendered BLACK
        # at 0. A default here would be a 20-hour black episode with every count right, so there
        # is none: the bias has to come from exposure_probe.py's sweep for this map.
        raise RuntimeError(
            "task declares render.exposure 'fixed' but no render.exposure_bias_ev is set for "
            f"{fz.get('map_id')}; run exposure_probe.py on this map and put its chosen bias in the "
            "task (or the shard plan). Refusing to record at an uncalibrated exposure.")
    bias = float(bias_raw) if (bias_raw is not None and str(bias_raw).lower() != "auto") else 0.0
    print(f"[capture] exposure: {'manual, bias %+.2f EV' % bias if manual else ('auto-exposure OFF for the session (fixed, no override)' if mode == 'off' else (f'level metering, INSTANT (speed {ae_speed:.0f}, range {ae_range}, bias {bias:+.2f})' if mode == 'auto_instant' else 'level auto-exposure (NOT pinned)'))}")
    if not isinstance(lighting, dict) or "skylight_fix" not in lighting:
        lighting = {"skylight_fix": lighting}
    lighting["cvars"] = cvars
    lighting["history_mode"] = int(os.environ.get("CAPTURE_HISTORY") or rend.get("history_mode", 0))
    lighting["supersample"] = int(os.environ.get("CAPTURE_SUPERSAMPLE") or rend.get("supersample", 1))
    lighting["exposure"] = {"mode": mode, "manual": manual, "bias_ev": bias if (manual or mode == "auto_instant") else None,
                            "auto_exposure_speed": ae_speed or None, "auto_exposure_range": ae_range if ae_speed else None,
                            "local_exposure": {"shadow_scale": le_shadow, "highlight_scale": le_highlight, "detail_strength": le_detail} if (le_shadow or le_highlight) else None,
                            "sweep": sweep_report}
    started = engine.simworld(
        req, "start_capture",
        f'"{Path(frozen_path).resolve()}", "{ep.resolve()}", {W}, {H}, {fov}, '
        f'1000.0, True, 92, True, {bias}, {str(manual)}, {ae_speed}, {float(ae_range[0])}, {float(ae_range[1])}, '
        f'{le_shadow}, {le_highlight}, {le_detail}, {int(os.environ.get("CAPTURE_HISTORY") or rend.get("history_mode", 0))}, '
        f'{int(os.environ.get("CAPTURE_SUPERSAMPLE") or rend.get("supersample", 1))}')
    if not started.get("ok"):
        raise RuntimeError(f"could not start the in-engine capture: {started.get('error')}")
    total = started.get("total", len(fz["poses"]))
    print(f"[capture] armed in-engine: {total} frames at {W}x{H}, fov {fov:.2f}")

    t0 = time.time()
    last = -1
    stalled_since = None
    last_beat = 0.0
    while True:
        st = engine.simworld(req, "get_capture_status", "")
        if not st.get("ok"):
            raise RuntimeError(f"status query failed: {st.get('error')}")
        # The heartbeat is NOT conditional on the frame counter moving. It used to be, and that
        # made a stall the one state which produced no output: the in-engine capture wedged, the
        # log went quiet, driver.sh's watchdog measured the log's mtime, and at 48 minutes of
        # silence it sent SIGKILL to a process holding hours of unpackaged frames. Thirteen
        # episodes were orphaned that way in one run.
        #
        # So a stall now announces itself, and says how long it has been stalled. What the
        # watchdog should measure is this frame counter, not whether anything was printed.
        moved = st["frame"] != last
        if moved:
            last = st["frame"]
            stalled_since = None
        elif stalled_since is None:
            stalled_since = time.time()
        now = time.time()
        if moved or now - last_beat >= 30.0:
            last_beat = now
            stall = "" if stalled_since is None else \
                f"  STALLED {now - stalled_since:.0f}s at this frame"
            print(f"[capture] {st['frame']}/{st['total']}  {st['elapsed_s']:.0f}s  "
                  f"{st['fps']:.2f} fps  render {st['render_ms']:.0f} ms  "
                  f"readback {st['readback_ms']:.0f} ms  write {st['write_ms']:.0f} ms{stall}",
                  flush=True)
        if st.get("finished"):
            break
        if on_poll is not None:
            try:
                on_poll()
            except Exception as e:
                # A crowd that stops walking is a cosmetic loss; the trajectory is the artefact.
                print(f"[capture] poll hook failed ({type(e).__name__}: {e}); continuing",
                      flush=True)
        time.sleep(poll)
    engine_s = time.time() - t0
    if lighting["history_mode"] == 4:
        if st.get("history_mode") != 4 or st.get("temporal_aa_enabled") is not True:
            raise RuntimeError("temporal capture requested but not enabled by the running C++ module; rebuild it")
        if st.get("warmup_frames", 0) < 32:
            raise RuntimeError("temporal capture did not complete its rendered warmup")
    planned_total, short_reason = total, None
    if st["frame"] != total:
        # A single 20 h episode has no checkpoint: an engine that stops at hour 15 has written
        # 15 h of frames, and refusing to package them throws all of it away. With
        # allow_short_episode the episode becomes what was actually recorded - a complete
        # shorter episode, with the planned length and the reason recorded next to it - rather
        # than a partial one claiming to be 20 h. Off by default: for a 60 s campaign episode
        # a short recording is a fault worth failing on, not a shorter dataset.
        if not bool(task.get("allow_short_episode", False)):
            raise RuntimeError(
                f"the engine stopped at frame {st['frame']} of {total}: {st.get('reason')}. "
                f"Refusing to package a partial trajectory.")
        total = int(st["frame"])
        short_reason = str(st.get("reason"))
        if total < int(task.get("min_frames", 0)):
            raise RuntimeError(
                f"the engine stopped at frame {total} of {planned_total} ({short_reason}), "
                f"below the task's min_frames of {task.get('min_frames')}")
        print(f"[capture] the engine stopped at frame {total} of {planned_total} "
              f"({short_reason}); packaging the {total} frames that were recorded, and "
              f"recording the planned length alongside", flush=True)

    return finalise(ep, fz, task, total, planned_total=planned_total,
                    short_reason=short_reason, engine_s=engine_s, st=st, lighting=lighting)


def finalise(ep, fz, task, total, *, planned_total=None, short_reason=None,
             engine_s=None, st=None, lighting=None, review_video=True):
    """Everything capture() does after the last frame is written: the per-frame table, the review
    video, the keyframes, capture_summary.json and trajectory.json.

    Split out so it can be run on an episode whose capture was interrupted. The engine writes
    rgb/%06d.jpg, depth/%06d.exr and engine_states.jsonl AS IT GOES, so a run killed mid-capture
    leaves hours of real frames on disk with none of the files that make them an episode - and
    that is exactly what happened to most of a 31-shard fleet run: the in-engine capture stalled,
    the progress print is gated on the frame counter advancing so the log went silent, and a
    45-minute watchdog killed the runner with SIGKILL before any of this ran.

    `st`, `engine_s` and `lighting` are unknown in that case. They are recorded as null with a
    note rather than invented, because capture_summary's timings are measurements.
    """
    fps = float(fz["fps"])
    W, H = task["camera"]["resolution"]
    fov = float(fz["camera_fov_deg_actual"])
    if planned_total is None:
        planned_total = int(fz.get("frames") or total)
    # ---- read what the engine wrote -------------------------------------------------
    #
    # Streamed, line by line, straight into frames.csv. Reading the whole file and building the
    # whole table first cost two copies of the episode in memory - engine_states.jsonl is 745 MB
    # at 20 h and the row dicts several GB on top - for a table nothing needs in one piece. What
    # is carried forward is the handful of aggregates capture_summary reports.
    intr = geom.intrinsics(W, H, fov)
    n = 0
    prev = None
    prev_yaw = None
    perr_sum = perr_max = yerr_sum = yerr_max = 0.0
    speed_max = yawrate_max = 0.0
    fh_csv = open(ep / "frames.csv", "w", newline="")
    wr = csv.DictWriter(fh_csv, fieldnames=FIELDS)
    wr.writeheader()
    # A capture killed mid-write leaves a torn tail: the last line can be half-written, or
    # complete JSON with a field the engine had not filled in yet. Either way it must not become
    # a row - csv.DictWriter turns a missing key into a blank column, and the blank surfaces much
    # later as a TypeError inside an acceptance gate that names neither the row nor the file.
    # Skipped lines are COUNTED and reported: a torn tail is expected after a SIGKILL, but how
    # much was thrown away is a fact about the episode, not an implementation detail.
    torn = 0
    for raw in open(ep / "engine_states.jsonl"):
        if not raw.strip():
            continue
        if n >= total:
            break            # a short episode stops here; the tail belongs to frames not kept
        s = parse_state(raw)
        if s is None:
            torn += 1
            print(f"[capture] engine_states.jsonl line {n + torn} cannot become a row; "
                  f"the episode ends at frame {n - 1}", flush=True)
            break
        i = s["frame_id"]
        al, ar = s["actual_location"], s["actual_rotation_pyr"]
        dl, dr = s["desired_location"], s["desired_rotation_pyr"]
        c2w = geom.canonical_c2w(al, ar[0], ar[1], ar[2])
        step = 0.0 if prev is None else math.dist(al, prev)
        dyaw = 0.0 if prev is None else abs((ar[1] - prev_yaw + 540) % 360 - 180)
        d = s["depth"]
        row = {
            "frame_id": i, "episode_time_s": i / fps, "phase": s.get("phase", ""),
            "desired_x_cm": dl[0], "desired_y_cm": dl[1], "desired_z_cm": dl[2],
            "desired_pitch_deg": dr[0], "desired_yaw_deg": dr[1], "desired_roll_deg": dr[2],
            "actual_x_cm": al[0], "actual_y_cm": al[1], "actual_z_cm": al[2],
            "actual_pitch_deg": ar[0], "actual_yaw_deg": ar[1], "actual_roll_deg": ar[2],
            "pos_error_cm": math.dist(al, dl),
            "yaw_error_deg": abs((ar[1] - dr[1] + 540) % 360 - 180),
            "step_cm": step, "speed_m_s": step / 100.0 * fps, "yaw_rate_deg_s": dyaw * fps,
            "quat_x": s["quat_xyzw"][0], "quat_y": s["quat_xyzw"][1],
            "quat_z": s["quat_xyzw"][2], "quat_w": s["quat_xyzw"][3],
            "c2w": " ".join(f"{v:.6f}" for v in c2w.flatten()),
            "depth_path": f"depth/{i:06d}.exr", "depth_bit_depth": d["bit_depth"],
            "depth_valid_fraction": d["valid_fraction"], "depth_min_m": d["min_m"],
            "depth_max_m": d["max_m"], "depth_centre_m": d["centre_m"],
            "rgb_path": f"rgb/{i:06d}.jpg",
        }
        wr.writerow(row)
        n += 1
        pe, ye = row["pos_error_cm"], row["yaw_error_deg"]
        perr_sum += pe; perr_max = max(perr_max, pe)
        yerr_sum += ye; yerr_max = max(yerr_max, ye)
        speed_max = max(speed_max, row["speed_m_s"])
        yawrate_max = max(yawrate_max, row["yaw_rate_deg_s"])
        prev, prev_yaw = al, ar[1]
    fh_csv.close()
    if n != total:
        raise RuntimeError(f"engine_states.jsonl yielded {n} rows for {total} frames"
                           + (f" ({torn} trailing line(s) were unusable)" if torn else ""))

    # The review mp4 and the lossless keyframes are BOTH excluded from the upload
    # (uploader.sh: --exclude rgb.mp4 --exclude rgb_proxy.mp4 --exclude "rgb_keyframes/*"), so on
    # a recovery, where the only goal is to get the delivered frames into S3, building them is
    # work on the critical path that nothing downstream will ever read. A 400k-frame episode
    # encodes 100k frames into the proxy alone.
    if review_video:
        review_file, review_stride = assemble_mp4(ep, fps, W, H, total)
        print(f"[capture] review video {review_file}"
              + (f" (every {review_stride}th frame; a full-rate encode of {total} frames is not a "
                 f"review artifact)" if review_stride > 1 else f" from {total} JPEGs"))

        # lossless keyframes for the anchor and every revisit window (spec section 13)
        import cv2
        (ep / "rgb_keyframes").mkdir(exist_ok=True)
        windows = [fz["anchor_window"]] + [e["window"] for e in (fz.get("revisit_events") or [])]
        for a, b in windows:
            for i in range(a, b + 1):
                jpg = ep / "rgb" / f"{i:06d}.jpg"
                if jpg.exists():
                    cv2.imwrite(str(ep / "rgb_keyframes" / f"{i:06d}.png"), cv2.imread(str(jpg)))
    else:
        review_file, review_stride = None, None
        print("[capture] review video and keyframes skipped: both are excluded from the upload, "
              "so on a recovery they are cost with no consumer", flush=True)

    summary = {
        "episode_id": fz["episode_id"], "frozen_sha256": fz.get("sha256"),
        "map_id": fz["map_id"], "spawn_id": fz["spawn_id"], "seed": task["seed"],
        "split": task.get("split"), "trajectory_family": task["trajectory_family"],
        "trajectory_generator_version": fz["generator_version"],
        "recorder_version": "pipeline-capture-engine-1",
        "control_model": "frozen trajectory executed inside the engine tick; the scene capture "
                         "components are placed on the frozen camera pose directly, so there is "
                         "no pawn and desired == actual by construction",
        "fps": fps, "frames": n, "duration_s": n / fps,
        "planned_frames": planned_total,
        "planned_duration_s": planned_total / fps,
        "ended_early_reason": short_reason,
        "review_video": ({"file": review_file, "frame_stride": review_stride,
                          "note": ("every frame" if review_stride == 1 else
                                   f"a timelapse of every {review_stride}th frame; frame numbers "
                                   f"in it do not correspond to frames.csv")}
                         if review_file else
                         {"file": None, "note": "not built; it is a review artifact excluded "
                                                "from the upload and this episode was packaged "
                                                "for delivery only"}),
        "resolution": f"{W}x{H}", "intrinsics": intr,
        "speed_tier": task["speed_tier"], "yaw_tier": task["yaw_tier"],
        "planned_speed_m_s": fz["speed_m_per_s"], "planned_yaw_deg_s": fz["yaw_deg_per_s"],
        "eye_height_cm": fz["eye_height_cm"],
        "pose_tracking": {"pos_error_cm_mean": perr_sum / max(n, 1),
                          "pos_error_cm_max": perr_max,
                          "yaw_error_deg_mean": yerr_sum / max(n, 1),
                          "yaw_error_deg_max": yerr_max},
        "measured_speed_m_s_max": speed_max,
        "measured_yaw_rate_deg_s_max": yawrate_max,
        "blank_frames": 0,
        "axis_smoke": geom.axis_smoke(intr),
        # These four are the capture's own MEASUREMENTS of the run that produced the frames, so
        # when finalise is called on an episode whose capture was interrupted they do not exist.
        # Null with a stated reason, never a plausible number: a recovered episode must not claim
        # an engine fps nobody measured. The gates read what is here, so a null reaches acceptance
        # as an unmade check rather than as a passing one.
        "record_seconds": round(engine_s, 1) if engine_s is not None else None,
        "engine_timing_ms": ({"render": st["render_ms"], "readback": st["readback_ms"],
                              "write": st["write_ms"]} if st else None),
        "engine_fps": (st["fps"] if st else None),
        "render": task.get("render"),
        "render_runtime": ({k: st.get(k) for k in (
            "history_mode", "temporal_aa_enabled", "warmup_frames",
            "rgb_render_width", "rgb_render_height")} if st else None),
        "p0_gaps": [],
    }
    summary["lighting"] = lighting
    if engine_s is None or st is None:
        summary["measurements_unavailable"] = (
            "record_seconds, engine_timing_ms, engine_fps and lighting are null because this "
            "episode was finalised after its capture was interrupted; they are measurements of "
            "the recording process and no process was running to measure. The per-frame data, "
            "which comes from engine_states.jsonl, is unaffected.")
    (ep / "capture_summary.json").write_text(json.dumps(summary, indent=1))
    (ep / "trajectory.json").write_text(json.dumps(
        {k: v for k, v in fz.items() if k != "poses"}, indent=1))
    fps_note = f"{st['fps']:.2f} fps in-engine" if st else "in-engine fps not measured"
    secs_note = f"{engine_s:.0f}s" if engine_s is not None else "an unmeasured time"
    print(f"[capture] {n} frames in {secs_note} ({fps_note}); "
          f"pos err max {perr_max*10:.3f} mm; yaw err max {yerr_max:.4f} deg")
    return ep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frozen")
    a = ap.parse_args()
    capture(a.frozen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
