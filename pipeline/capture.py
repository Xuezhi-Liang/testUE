#!/usr/bin/env python3
"""Execute a frozen trajectory: render and record, nothing else.

This stage deliberately has no opinions. It does not choose poses, avoid obstacles, adapt speed
or retry a leg. Every pose comes from the frozen file; if a pose is wrong, the freeze stage is
wrong and should be fixed there, where it can be validated before two minutes of GPU time is
spent. That split is what makes a run reproducible.

What it records per frame (spec section 9):
    frame_id, episode_time_s = frame_id / fps
    RGB
    desired pose (from the frozen file)
    actual CameraComponent pose, as read back from the engine
    raw UE location in cm and the quaternion derived from the readback
    canonical camera-to-world in metres
    the final K
    the pose error between desired and actual

Metric depth is absent and the reason is recorded rather than glossed: this build's Vulkan RHI
cannot read back a render target at all.
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
import geom  # noqa: E402

OUT_ROOT = HERE / "episodes"

FIELDS = ["frame_id", "episode_time_s", "phase", "depth_path", "depth_bit_depth",
          "depth_valid_fraction", "depth_min_m", "depth_max_m", "depth_centre_m",
          "desired_x_cm", "desired_y_cm", "desired_z_cm",
          "desired_yaw_deg", "desired_pitch_deg", "desired_roll_deg",
          "actual_x_cm", "actual_y_cm", "actual_z_cm",
          "actual_yaw_deg", "actual_pitch_deg", "actual_roll_deg",
          "pos_error_cm", "yaw_error_deg",
          "step_cm", "speed_m_s", "yaw_rate_deg_s",
          "quat_x", "quat_y", "quat_z", "quat_w", "c2w"]


class H264Writer:
    """cv2's mp4v writes MPEG-4 Part 2 with the moov atom at the end, which browsers refuse to
    play. H.264 with +faststart is required for the review page to work at all."""

    def __init__(self, path, w, h, fps):
        import imageio_ffmpeg
        self.p = subprocess.Popen(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps}",
             "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def write(self, bgr):
        self.p.stdin.write(np.ascontiguousarray(bgr).tobytes())

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=3600)


def capture(frozen_path, out_root=OUT_ROOT, keyframe_windows=True,
            ucv=None, rig=None):
    fz = json.loads(Path(frozen_path).read_text())
    task = fz["task"]
    fps = float(fz["fps"])
    W, H = task["camera"]["resolution"]
    fov = float(fz["camera_fov_deg_actual"])
    poses = fz["poses"]
    ep = out_root / fz["episode_id"]
    ep.mkdir(parents=True, exist_ok=True)
    (ep / "rgb_keyframes").mkdir(exist_ok=True)
    (ep / "depth").mkdir(exist_ok=True)

    intr = geom.intrinsics(W, H, fov)
    K = geom.K_matrix(intr)

    owns_rig = rig is None
    if ucv is None:
        ucv = engine.connect(W, H)
        if ucv is None:
            raise RuntimeError("UE never became reachable")
    if rig is None:
        rig = engine.Rig(ucv, W, H, float(task["camera"]["fov_deg"]))
        rig.configure()

    # the head offset converts a desired camera pose into the pawn placement that yields it
    eye_cm = float(fz["eye_height_cm"])
    off = rig.calibrate_camera_offset((fz["anchor_xy_cm"][0], fz["anchor_xy_cm"][1]),
                                      float(fz["anchor_ground_z_cm"]))
    print(f"[capture] camera offset from pawn origin: "
          f"({off[0]:.1f}, {off[1]:.1f}, {off[2]:.1f}) cm")

    # Depth is written by the engine directly into the episode's depth/ directory. It costs a
    # measured 158 ms per frame at 1280x720, which is why full-rate depth is affordable at all;
    # spec section 9 lists it as P0 per frame, not per keyframe.
    # Straight into the episode directory: the container sees the repo, so the engine can write
    # there itself and there is no copy step to go wrong.
    depth_dir_ctr = str((ep / "depth").resolve())
    req = ucv.client.request

    vw = H264Writer(ep / "rgb.mp4", W, H, fps)
    rows = []
    aw, rw = fz["anchor_window"], fz["revisit_window"]
    # lossless keyframes for the anchor and EVERY revisit window (spec section 13). Saving only
    # the last window leaves an earlier, shorter-age revisit with no evidence at all.
    key_windows = [aw] + [e["window"] for e in (fz.get("revisit_events") or [rw])]
    blank = 0
    t0 = time.time()
    prev = None

    for i, p in enumerate(poses):
        want = (p["x_cm"], p["y_cm"], p["z_cm"])
        loc, rot = rig.place_camera(want, p["yaw_deg"], p["pitch_deg"], off)
        img = rig.grab()
        if img is None:
            raise RuntimeError(f"no frame returned at {i}")
        bgr = np.asarray(img)[:, :, :3].astype(np.uint8)
        if bgr.shape[0] != H or bgr.shape[1] != W:
            raise RuntimeError(f"frame {i} is {bgr.shape[1]}x{bgr.shape[0]}, expected {W}x{H}; "
                               f"the K on file would not match the image")
        if float(bgr.max()) - float(bgr.min()) < 2.0:
            blank += 1
        vw.write(bgr)

        # Depth at the ACTUAL camera pose, not the commanded one: it has to describe the same
        # view the RGB frame does.
        dj = engine.depth_capture(req, loc, yaw=rot[1], pitch=rot[0], fov=fov,
                                  width=W, height=H,
                                  out_path=f"{depth_dir_ctr}/{i:06d}.exr")
        if not dj.get("ok"):
            raise RuntimeError(
                f"depth capture failed at frame {i}: {dj.get('error')}. Refusing to continue - a "
                f"P0 channel with holes in it is worse than a run that stopped.")

        pitch, yaw, roll = rot[0], rot[1], rot[2]
        c2w = geom.canonical_c2w(loc, pitch, yaw, roll)
        q = geom.quat_xyzw_from_euler(pitch, yaw, roll)
        step = 0.0 if prev is None else math.dist(loc, prev)
        dyaw = 0.0 if prev is None else abs((yaw - rows[-1]["actual_yaw_deg"] + 540) % 360 - 180)
        rows.append({
            "frame_id": i, "episode_time_s": i / fps, "phase": p["phase"],
            "depth_path": f"depth/{i:06d}.exr",
            "depth_bit_depth": dj.get("bit_depth"),
            "depth_valid_fraction": 1.0 - float(dj["invalid_fraction"]),
            "depth_min_m": dj["min_m"], "depth_max_m": dj["max_m"],
            "depth_centre_m": dj["centre_m"],
            "desired_x_cm": want[0], "desired_y_cm": want[1], "desired_z_cm": want[2],
            "desired_yaw_deg": p["yaw_deg"], "desired_pitch_deg": p["pitch_deg"],
            "desired_roll_deg": p["roll_deg"],
            "actual_x_cm": loc[0], "actual_y_cm": loc[1], "actual_z_cm": loc[2],
            "actual_yaw_deg": yaw, "actual_pitch_deg": pitch, "actual_roll_deg": roll,
            "pos_error_cm": math.dist(loc, want),
            "yaw_error_deg": abs((yaw - p["yaw_deg"] + 540) % 360 - 180),
            "step_cm": step, "speed_m_s": step / 100.0 * fps,
            "yaw_rate_deg_s": dyaw * fps,
            "quat_x": q[0], "quat_y": q[1], "quat_z": q[2], "quat_w": q[3],
            "c2w": " ".join(f"{v:.6f}" for v in c2w.flatten()),
        })
        prev = loc

        if keyframe_windows and any(a <= i <= b for a, b in key_windows):
            cv2.imwrite(str(ep / "rgb_keyframes" / f"{i:06d}.png"), bgr)
        if i % 200 == 0:
            el = time.time() - t0
            print(f"[capture] {i}/{len(poses)}  {el:.0f}s  "
                  f"{(i+1)/max(el,1e-6):.2f} fps  pos err "
                  f"{rows[-1]['pos_error_cm']*10:.2f} mm", flush=True)

    vw.close()
    if owns_rig:
        rig.destroy()

    with open(ep / "frames.csv", "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=FIELDS)
        wr.writeheader()
        wr.writerows(rows)

    perr = [r["pos_error_cm"] for r in rows]
    yerr = [r["yaw_error_deg"] for r in rows]
    smoke = geom.axis_smoke(intr)
    summary = {
        "episode_id": fz["episode_id"],
        "frozen_sha256": fz.get("sha256"),
        "map_id": fz["map_id"],
        "spawn_id": fz["spawn_id"],
        "seed": task["seed"],
        "split": task.get("split"),
        "trajectory_family": task["trajectory_family"],
        "trajectory_generator_version": fz["generator_version"],
        "recorder_version": "pipeline-capture-1",
        "control_model": "frozen trajectory, camera placed per frame; the pawn exists only to "
                         "own a real CameraComponent",
        "fps": fps,
        "frames": len(rows),
        "duration_s": len(rows) / fps,
        "resolution": f"{W}x{H}",
        "intrinsics": intr,
        "speed_tier": task["speed_tier"], "yaw_tier": task["yaw_tier"],
        "planned_speed_m_s": fz["speed_m_per_s"], "planned_yaw_deg_s": fz["yaw_deg_per_s"],
        "eye_height_cm": eye_cm,
        "camera_offset_from_pawn_cm": off,
        "pose_tracking": {
            "pos_error_cm_mean": float(np.mean(perr)), "pos_error_cm_max": float(np.max(perr)),
            "yaw_error_deg_mean": float(np.mean(yerr)),
            "yaw_error_deg_max": float(np.max(yerr))},
        "measured_speed_m_s_max": max(r["speed_m_s"] for r in rows),
        "measured_yaw_rate_deg_s_max": max(r["yaw_rate_deg_s"] for r in rows),
        "blank_frames": blank,
        "axis_smoke": smoke,
        "record_seconds": round(time.time() - t0, 1),
        "render": task.get("render"),
        "p0_gaps": ["metric depth: the Vulkan RHI in this build cannot read back a render "
                    "target (VulkanRenderTarget.cpp:171 asserts and kills the editor), so no "
                    "depth source exists. UnrealCV's constant 65504 is the same failure."],
    }
    (ep / "capture_summary.json").write_text(json.dumps(summary, indent=1))
    (ep / "trajectory.json").write_text(json.dumps(
        {k: v for k, v in fz.items() if k != "poses"}, indent=1))
    print(f"[capture] {len(rows)} frames in {summary['record_seconds']}s; "
          f"pos err mean {summary['pose_tracking']['pos_error_cm_mean']*10:.3f} mm "
          f"max {summary['pose_tracking']['pos_error_cm_max']*10:.3f} mm; "
          f"yaw err max {summary['pose_tracking']['yaw_error_deg_max']:.4f} deg; "
          f"blank {blank}")
    print(f"[capture] wrote {ep}")
    return ep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frozen", help="path to a frozen trajectory JSON")
    a = ap.parse_args()
    capture(a.frozen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
