#!/usr/bin/env python3
"""Clear one incorrect flag on WinterTown/Particles/M_Fog so it compiles.

The editor states both the defect and the fix:

    M_Fog.uasset: Failed to compile Material for platform SF_VULKAN_SM6, Default Material will
    be used in game.
        Volume materials are not compatible with skinned meshes: they are voxelised as boxes
        anyway. Please disable UsedWithSkeletalMesh on the material.

This is a different class of problem from ModularNeighborhood's M_Dokyo_Foliage, which fails on a
Missing Material Function - an asset the pack never shipped. Inventing that dependency would be
re-authoring someone else's content and that map was dropped instead. Here the asset is complete
and one flag is wrong, so clearing it restores what the author intended rather than changing it.

The asset IS modified and saved. That is stated in the episode's source metadata, because "the
level as shipped" and "the level with a flag repaired" are different provenance.
"""
import json
import sys
from pathlib import Path

P = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
sys.path.insert(0, str(P))
import engine  # noqa: E402

ASSET = "/Game/WinterTown/Particles/M_Fog"

BODY = f'''
import unreal as u
m = u.EditorAssetLibrary.load_asset("{ASSET}")
if m is None:
    RESULT.update({{"ok": False, "error": "asset not found"}})
else:
    before = bool(m.get_editor_property("used_with_skeletal_mesh"))
    m.set_editor_property("used_with_skeletal_mesh", False)
    u.MaterialEditingLibrary.recompile_material(m)
    saved = u.EditorAssetLibrary.save_asset("{ASSET}", only_if_is_dirty=False)
    RESULT.update({{"ok": True, "used_with_skeletal_mesh_before": before,
                   "used_with_skeletal_mesh_after":
                       bool(m.get_editor_property("used_with_skeletal_mesh")),
                   "saved": bool(saved)}})
'''

if __name__ == "__main__":
    ucv = engine.connect(1280, 720, timeout=600)
    if ucv is None:
        print("UE never became reachable")
        sys.exit(2)
    r = engine.query(ucv.client.request, BODY, timeout=300)
    print(json.dumps(r, indent=2))
    sys.exit(0 if r.get("ok") else 1)
