#!/usr/bin/env python3
"""Does a CitySampleCrowd body have a head, and does the SK_Base walk animation play?

Both are questions a picture answers and reasoning does not. `m_tal_nrw_base` has a single material
slot called M_Hide and its bounds are only 160 cm tall, which suggests the head comes from the
MetaHuman face mesh - and loading that face mesh from editor Python hung the editor for 25 minutes
and had to be killed. If the base turns out to carry a head, the whole walker can be built on
SK_Base and the MetaHuman path is avoidable.

Spawns the body, drops it on the Tokyo anchor from the frozen episode we already captured, plays
the walk animation, and photographs it from the front with the ordinary UnrealCV camera.
"""
import json
import sys
from pathlib import Path

P = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
sys.path.insert(0, str(P))
import engine  # noqa: E402

FROZEN = next(P.glob("frozen/*batch5_tokyo.json"))
C = "/Game/CitySampleCrowd/Character/"
BASE = C + "Male/NormalWeight/Meshes/m_tal_nrw_base"
WALK = C + "Anims/Loco/MTN_N_Walk_F"
OUT = P / "_qa"

SETUP = """
import unreal as U
world = U.EditorLevelLibrary.get_game_world()
mesh = U.load_asset("%(base)s")
anim = U.load_asset("%(walk)s")
all_sk = list(U.GameplayStatics.get_all_actors_of_class(world, U.SkeletalMeshActor))
# get_name(), not get_actor_label(): labels are an editor-only concept and come back empty under
# PIE, so a label filter finds nothing however well the spawn worked.
found = [a for a in all_sk if "sw_walker" in str(a.get_name())]
if not found:
    RESULT.update({"ok": False, "error": "no sw_walker actor found",
                   "skeletal_mesh_actors": [str(a.get_name()) for a in all_sk][:20],
                   "count": len(all_sk)})
else:
    a = found[0]
    comp = a.skeletal_mesh_component
    comp.set_skeletal_mesh_asset(mesh)
    a.set_actor_location_and_rotation(
        U.Vector(%(x).3f, %(y).3f, %(z).3f), U.Rotator(roll=0.0, pitch=0.0, yaw=%(yaw).3f),
        False, False)
    # Single-node mode, positioned explicitly: our capture advances one frame per tick on a fixed
    # 1/24 s clock, so an animation driven by real delta time would not be reproducible.
    comp.set_animation_mode(U.AnimationMode.ANIMATION_SINGLE_NODE)
    comp.set_animation(anim)
    comp.set_position(%(t).3f, False)
    comp.set_editor_property("visibility_based_anim_tick_option",
                             U.VisibilityBasedAnimTickOption.ALWAYS_TICK_POSE_AND_REFRESH_BONES)
    comp.set_update_animation_in_editor(True)
    comp.tick_animation(0.0, False)
    comp.refresh_bone_transforms()
    b = a.get_actor_bounds(False)
    RESULT.update({"ok": True,
                   "mesh": str(mesh.get_name()),
                   "anim": str(anim.get_name()),
                   "anim_length_s": round(float(anim.get_editor_property("play_length")), 4),
                   "actor_origin": [round(float(v), 1) for v in (b[0].x, b[0].y, b[0].z)],
                   "actor_extent_cm": [round(float(v), 1) for v in (b[1].x, b[1].y, b[1].z)],
                   "top_of_mesh_z": round(float(b[0].z + b[1].z), 1),
                   "bones": int(comp.get_num_bones())})
""".strip()


def main():
    fz = json.loads(FROZEN.read_text())
    p0 = fz["poses"][0]
    eye = 170.0
    x, y, z, yaw = p0["x_cm"], p0["y_cm"], p0["z_cm"] - eye, p0["yaw_deg"]

    ucv = engine.connect(1280, 720, timeout=600)
    if ucv is None:
        print("UE never became reachable")
        return 2
    req = ucv.client.request

    print(req("vset /objects/spawn SkeletalMeshActor sw_walker"))
    r = engine.query(req, SETUP % {"base": BASE, "walk": WALK, "x": x, "y": y, "z": z,
                                   "yaw": yaw, "t": 0.4}, timeout=600)
    print(json.dumps(r, indent=1))
    if not r.get("ok"):
        return 1

    # Photograph from the front: 3 m along the walker's facing, at chest height, looking back.
    import math
    rad = math.radians(yaw)
    cx, cy = x + math.cos(rad) * 300.0, y + math.sin(rad) * 300.0
    rig = engine.Rig(ucv, 1280, 720, 90.0)
    rig.configure()
    rig.place_pawn(cx, cy, z + 110.0, (yaw + 180.0) % 360.0, 0.0)
    img = rig.grab()
    if img is None:
        print("no image")
        return 1
    import cv2
    OUT.mkdir(exist_ok=True)
    cv2.imwrite(str(OUT / "walker_spike.png"), img[..., :3][..., ::-1])
    print(f"wrote {OUT / 'walker_spike.png'}  shape {img.shape}")
    rig.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
