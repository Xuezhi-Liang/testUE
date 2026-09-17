#!/usr/bin/env python3
"""Record a CitySampleCrowd person walking a frozen route, filmed over their shoulder.

The route is not new. It is the same navmesh-planned, depth-probed, corridor-swept trajectory the
first-person episodes use - reinterpreted as where the WALKER's eyes are rather than where the
camera is. The camera is then derived from it: behind, to one side, raised, and turned to the
walker's OWN view direction rather than aimed at their back, so the person and what they are
walking towards are both in frame. Everything about the path's legality carries over unchanged.

The one number this has to measure rather than assume is the walk animation's own forward speed.
The animation is positioned by DISTANCE TRAVELLED, not by time:

    anim_time = fmod(travelled_cm / anim_speed_cm_s, anim_length_s)

so the stride advances exactly as far as the body does and the feet do not slide - including
through the cosine ease at the start and end of every leg, where a fixed play rate would slip.
Measuring it here rather than in C++ keeps it cheap to check: a wrong number is visible as sliding
feet, and correcting it does not mean rebuilding the engine module.
"""
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
import capture_engine as cape  # noqa: E402

OUT_ROOT = HERE / "episodes"

# The walker: the crowd's own blueprint, spawned by the engine module, not a mesh list assembled
# by hand.
#
# Hand assembly was tried and each attempt is worth recording because each looked plausible until
# rendered. A CitySampleCrowd citizen is five layers across three skeletons:
#
#   base + garments                  a suit with an empty collar and empty sleeves. `base` carries
#                                    material M_Hide - it is the body that hides UNDER clothes and
#                                    renders no skin at all.
#   + skin as a leader-pose follower  a stretched black spike above the shoulder: a 232-bone SK_Base
#                                    leader driving a 167-bone metahuman_base_skel follower maps
#                                    only the bones whose names match.
#   + face snapped to the head bone   an oversized head at hip height: a MetaHuman face's origin is
#                                    at the character's feet, not its head.
#
# BP_CrowdCharacter does all of it correctly on construction - the editor log shows it building
# base, body, garments and shoes for one identity - so the module spawns the blueprint and takes
# over the animation of whichever component drives the pose.
#
# MTN_N_Walk_F is the animation to use for more than availability: measured root motion puts it at
# 140.0 cm/s, which is the trajectory's own speed, so it plays at 1.00x. Manny's MM_Walk_Fwd is
# 240.0 cm/s and played at 0.58x, which is why that take looked like slow motion.
WALKER_BP = "/Game/CitySampleCrowd/Blueprints/BP_CrowdCharacter.BP_CrowdCharacter_C"
MESHES = []
import os as _os
WALK = _os.environ.get("WALK_ANIM",
                      "/Game/CitySampleCrowd/Character/Anims/Loco/MTN_N_Walk_F")
FACE = ""

# There is deliberately no in-editor animation measurement here any more.
#
# It used to call unreal.AnimationLibrary.get_bone_pose_for_time, which is deprecated since 5.2.
# Three attempts left the editor at 600% CPU with the game thread in R state, no log output and
# UnrealCV unresponsive for 25-35 minutes, and every one had to be killed.
#
# CORRECTION to an earlier reading of this: those runs were dismissed as "not compiling anything,
# so waiting would not help" on the evidence that /home/ue4/simworld/DerivedDataCache did not grow.
# That evidence was worthless - the work was skeletal mesh building and LOD reduction, which logs
# under LogSkeletalMesh / LogSkinnedAsset ("Waiting for skinned assets to be ready 0/1 (...)") and
# does not land in the cache that was being sampled. Editor.AsyncSkinnedAssetCompilation is set to 0
# at startup, so that build blocks the game thread. Those processes may well have finished if left
# alone. What is true is that it is slow and unnecessary: the module measures the root motion itself
# with ExtractRootMotionFromRange and reports walk_root_motion_cm and walk_anim_speed_cm_s in its
# status, so the number is still measured rather than assumed - just not from Python.


def capture(frozen_path, out_root=OUT_ROOT, ucv=None, poll=2.0, behind_cm=450.0,
            above_cm=40.0, look_at_z_cm=110.0, side_cm=80.0, pitch_deg=-10.0,
            follow_view=True):
    fz = json.loads(Path(frozen_path).read_text())
    task = fz["task"]
    fps = float(fz["fps"])
    W, H = task["camera"]["resolution"]
    fov = float(fz["camera_fov_deg_actual"])
    eye_cm = float(task["camera"]["eye_height_m"]) * 100.0
    ep = out_root / (fz["episode_id"] + "__walker")
    ep.mkdir(parents=True, exist_ok=True)

    if ucv is None:
        ucv = engine.connect(W, H)
        if ucv is None:
            raise RuntimeError("UE never became reachable")
    req = ucv.client.request

    # Wait until the editor actually ANSWERS, not just until its port is open.
    #
    # launch_ue.sh returns as soon as UnrealCV is listening, but the editor keeps working after that
    # - UnrealCV annotates every mesh in the level, the asset registry cache is written - and a
    # request sent during that just queues. One run in three sat at 600% CPU with the log stopped on
    # "Overwrite the world setting with some UnrealCV extensions" and no output of ours at all,
    # which looked exactly like the hang this file already documents and was not it. Bounded, so a
    # genuinely wedged editor fails here with a clear message instead of hanging forever.
    ready = False
    for attempt in range(12):
        try:
            if ucv.client.request("vget /unrealcv/status", timeout=20):
                ready = True
                if attempt:
                    print(f"[walker] editor answered on attempt {attempt + 1}")
                break
        except Exception:
            pass
    if not ready:
        raise RuntimeError("the editor accepted a connection but never answered a request in ~4 "
                           "minutes; it is wedged and needs restarting")

    # Force synchronous skinned asset compilation BEFORE anything spawns a character.
    #
    # This repo already paid for this once and wrote it down in final_run.py: ini_unrealcv() leaves
    # Editor.AsyncSkinnedAssetCompilation at 2, and with async compilation on, the first spawn of a
    # humanoid never returns - the editor goes quiet at ~188% CPU with the game thread blocked and
    # the log stopping on "Waiting for skinned assets to be ready 0/1 (...)". Forcing 0 makes the
    # spawn block and actually finish.
    #
    # Not setting these is what cost several editor kills here: every one of those hangs was a
    # first-time skinned asset build waiting on a compiler that never delivered, and the first-time
    # cost is real but finite - roughly a second per mesh once it can proceed.
    for cvar in ("Editor.AsyncSkinnedAssetCompilation 0", "Editor.AsyncAssetCompilation 0"):
        try:
            req(f"vrun {cvar}")
            print(f"[walker] cvar {cvar}")
        except Exception as e:
            print(f"[walker] cvar {cvar} failed ({type(e).__name__})")

    # The framing is computed, not guessed. A 90 degree horizontal FOV at 16:9 is only +-29.4 deg
    # vertically, and the walker has to fit inside that with margin at both ends.
    #
    #   260 cm behind, 260 above the feet: feet at atan(260/260) = 45 deg down, outside the frame at
    #       any tilt - head and shoulders only, no visible walking.
    #   340 cm behind, 230 above:          feet at 34 deg, head at 9 deg. The body fits but fills
    #       most of the vertical FOV, and the head clipped the top edge.
    #   450 cm behind, 210 above:          feet at atan(210/450) = 25 deg, head at 4 deg. With a
    #       10 deg tilt the frame covers -39 to +19 deg, so the whole body sits in about 35% of the
    #       frame height with clearance above the head and below the feet.
    args = (f'"{Path(frozen_path).resolve()}", "{ep.resolve()}", {W}, {H}, {fov}, '
            f'"{",".join(MESHES)}", "{WALK}", "{FACE}", "{WALKER_BP}", {eye_cm}, 0.0, '
            f'{behind_cm}, {above_cm}, {look_at_z_cm}, {side_cm}, {pitch_deg}, '
            f'{str(bool(follow_view))}, 1000.0, True, 92')
    started = engine.simworld(req, "start_walker_capture", args, timeout=900)
    if not started.get("ok"):
        raise RuntimeError(f"could not start the walker capture: {started.get('error')}")
    total = started.get("total", len(fz["poses"]))
    speed = float(started.get("walk_anim_speed_cm_s") or 0.0)
    aim = (f"turned to the walker's heading with {pitch_deg:.0f} deg of tilt" if follow_view
           else f"aimed at {look_at_z_cm:.0f} cm height")
    print(f"[walker] armed: {total} frames at {W}x{H}, fov {fov:.2f}, "
          f"{started.get('walker_parts')} meshes, camera {behind_cm:.0f} cm behind, "
          f"{side_cm:.0f} cm to the side, {above_cm:.0f} cm above the eye line, {aim}")
    print(f"[walker] walk cycle {started.get('walk_anim_length_s', 0):.3f} s, root travels "
          f"{started.get('walk_root_motion_cm', 0):.1f} cm -> {speed:.1f} cm/s")
    if speed < 1.0:
        print(f"[walker] WARNING: no root motion ({speed:.2f} cm/s), so the cycle plays at "
              f"wall-clock rate and the feet WILL slide. Stated rather than hidden: it needs a "
              f"root-motion walk cycle, not a different play rate.")
    anim = {"walk_anim_length_s": started.get("walk_anim_length_s"),
            "walk_root_motion_cm": started.get("walk_root_motion_cm"),
            "walk_anim_speed_cm_s": speed,
            "measured_by": "ASimWorldCaptureActor::AttachWalker via "
                           "UAnimSequence::ExtractRootMotionFromRange"}

    t0 = time.time()
    last = -1
    while True:
        st = engine.simworld(req, "get_capture_status", "")
        if not st.get("ok"):
            raise RuntimeError(f"status query failed: {st.get('error')}")
        if st["frame"] != last:
            last = st["frame"]
            print(f"[walker] {st['frame']}/{st['total']}  {st['elapsed_s']:.0f}s  "
                  f"{st['fps']:.2f} fps", flush=True)
        if st.get("finished"):
            break
        time.sleep(poll)
    if st["frame"] != total:
        raise RuntimeError(f"the engine stopped at frame {st['frame']} of {total}: "
                           f"{st.get('reason')}")
    print(f"[walker] {total} frames in {time.time() - t0:.0f}s")

    cape.assemble_mp4(ep, fps, W, H)
    (ep / "WALKER.json").write_text(json.dumps({
        "source_frozen": str(Path(frozen_path).resolve()),
        "source_episode_id": fz["episode_id"],
        "map_id": task["map_id"],
        "meshes": MESHES or None,
        "walker_blueprint": WALKER_BP or None,
        "walk_animation": WALK,
        "face_mesh": FACE or None,
        "walk_animation_measured": anim,
        "eye_height_cm": eye_cm,
        "camera": {"behind_cm": behind_cm, "above_eye_cm": above_cm, "side_cm": side_cm, "pitch_deg": pitch_deg,
                   "follows_walker_view": bool(follow_view),
                   "look_at_height_cm": None if follow_view else look_at_z_cm, "fov_deg": fov},
        "note": "Third-person chase footage of a CitySampleCrowd person walking the frozen route "
                "of the episode named above. The route, its navmesh legality, its depth probe and "
                "its corridor sweep are that episode's; only the interpretation changed - the "
                "trajectory is the walker's eye line and the camera trails it. No face mesh is "
                "attached; see the comment in capture_walker.py for why.",
    }, indent=2) + "\n")
    print(f"[walker] done -> {ep}")
    return ep


def main():
    frozen = sys.argv[1] if len(sys.argv) > 1 else str(
        next(HERE.glob("frozen/*batch5_tokyo.json")))
    capture(frozen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
