#!/usr/bin/env python3
"""What does a CitySampleCrowd person actually consist of, and can we load it here?

The crowd is modular: body, top, bottom and shoes are separate skeletal meshes sharing one
skeleton, and the face is a separate mesh on the MetaHuman face skeleton. Before writing any
capture code we need to know which of those load in this headless session and which skeleton each
one is on - a face on a different skeleton cannot simply be added as another mesh component.

Note `unreal.load_asset`, not `EditorAssetLibrary.load_asset`: the latter returns None for every
asset in this session, including ones plainly present on disk.
"""
import json
import sys

sys.path.insert(0, "/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
import engine  # noqa: E402

C = "/Game/CitySampleCrowd/Character/"
WANT = {
    "body":  C + "Male/NormalWeight/Meshes/m_tal_nrw_body",
    "base":  C + "Male/NormalWeight/Meshes/m_tal_nrw_base",
    "top":   C + "Male/NormalWeight/Meshes/m_tal_nrw_buttonDown_tie_blazer",
    "legs":  C + "Male/NormalWeight/Meshes/m_tal_nrw_slacks_belt",
    "shoes": C + "Male/NormalWeight/Meshes/m_tal_nrw_oxfords",
    # The face mesh is deliberately absent. Loading Male/m_001/Face/m_001_nrw_FaceMesh from editor
    # Python HUNG the editor: 25 minutes at 170% CPU with a ShaderCompileWorker running, no log
    # output after the load began, and UnrealCV requests timing out - the game thread never came
    # back and the process had to be killed. MetaHuman skin and hair shaders compiling
    # synchronously on the game thread is the likely cause. Body and garment meshes load in under
    # a second, so what this has to establish instead is whether the body already carries a head.
    "walk":  C + "Anims/Loco/MTN_N_Walk_F",
    "animbp": C + "Male/NormalWeight/Rig/m_tal_nrw_animbp",
}

BODY = """
import unreal as U
want = %s
out = {}
for k, p in want.items():
    a = U.load_asset(p)
    if a is None:
        out[k] = None
        continue
    d = {"type": type(a).__name__}
    try:
        d["skeleton"] = str(a.get_editor_property("skeleton").get_name())
    except Exception:
        d["skeleton"] = "n/a"
    if k == "walk":
        try:
            d["length_s"] = round(float(a.get_editor_property("play_length")), 3)
        except Exception:
            pass
    if d["type"] == "SkeletalMesh":
        try:
            b = a.get_bounds()
            d["extent_cm"] = [round(float(v), 1) for v in
                              (b.box_extent.x, b.box_extent.y, b.box_extent.z)]
        except Exception:
            pass
        # Does this mesh reach head height, and does its skeleton have head bones? A City Sample
        # body is often headless by design - the face mesh supplies the head - and that decides
        # whether the MetaHuman face is optional or unavoidable.
        try:
            sk = a.get_editor_property("skeleton")
            bones = [str(x) for x in sk.get_editor_property("bone_tree")]
            d["bone_count"] = len(bones)
        except Exception:
            d["bone_count"] = None
        try:
            mats = a.get_editor_property("materials")
            d["material_slots"] = [str(m.get_editor_property("material_slot_name")) for m in mats]
        except Exception:
            pass
    out[k] = d
RESULT.update({"assets": out})
""" % json.dumps(WANT)

if __name__ == "__main__":
    ucv = engine.connect(1280, 720, timeout=600)
    if ucv is None:
        print("UE never became reachable")
        sys.exit(2)
    print(json.dumps(engine.query(ucv.client.request, BODY, timeout=900), indent=1))
