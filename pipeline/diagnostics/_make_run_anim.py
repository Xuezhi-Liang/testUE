#!/usr/bin/env python3
"""Retarget a run animation onto the CitySampleCrowd skeleton, so the SAME citizen can run.

The crowd pack ships walks and idles only - City Sample citizens never run - and the project's
existing IK retargeters go between the UE4 and UE5 mannequins, not into SK_Base. So a running
citizen needs three new assets: an IK Rig for SK_Base, a retargeter from the mannequin rig into it,
and the retargeted AnimSequence itself.

They are written to /Game/SimWorldRetarget/ - a folder that exists only for this - so they are
obviously ours and can be deleted without touching any shipped content.

Every step reports what it did or the exact exception, because the Python retargeting API varies
across engine versions and guessing method names has already cost this session several restarts.
"""
import json
import sys
from pathlib import Path

P = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
sys.path.insert(0, str(P))
import engine  # noqa: E402

DEST = "/Game/SimWorldRetarget"
SRC_RIG = "/Game/Characters/Mannequins/Rigs/IK_Mannequin"
SRC_MESH = "/Game/Characters/Mannequins/Meshes/SKM_Manny"
TGT_MESH = "/Game/CitySampleCrowd/Character/Male/NormalWeight/Meshes/m_tal_nrw_base"
RUN_ANIM = "/Game/Characters/Mannequins/Animations/Manny/MM_Run_Fwd"

BODY = """
import unreal as U
steps = []

def step(name, fn):
    try:
        v = fn()
        steps.append({"step": name, "ok": True, "value": None if v is None else str(v)[:160]})
        return v
    except Exception as e:
        steps.append({"step": name, "ok": False,
                      "error": type(e).__name__ + ": " + str(e)[:220]})
        return None

tools = U.AssetToolsHelpers.get_asset_tools()
src_rig = step("load source rig", lambda: U.load_asset("@@src_rig@@"))
tgt_mesh = step("load crowd mesh", lambda: U.load_asset("@@tgt_mesh@@"))
run = step("load run animation", lambda: U.load_asset("@@run@@"))

# Idempotent: create_asset returns None when the name already exists, and a previous run of this
# script left both assets behind. Loading first makes a re-run continue instead of cascading
# AttributeErrors off a None.
def get_or_create(name, cls, factory):
    a = U.load_asset("@@dest@@/" + name)
    return a if a is not None else tools.create_asset(name, "@@dest@@", cls, factory)

rig = step("get or create IK rig",
           lambda: get_or_create("IK_CrowdBase", U.IKRigDefinition, U.IKRigDefinitionFactory()))
rc = step("get rig controller", lambda: U.IKRigController.get_controller(rig))
# set_skeletal_mesh, not set_preview_mesh - the latter is on the RETARGETER
# controller. Without the mesh the auto characterisation has nothing to read and
# returned False with an empty chain list.
step("set skeletal mesh", lambda: rc.set_skeletal_mesh(tgt_mesh))
# UE 5.4+ characterises the skeleton itself: it finds the retarget root and builds the standard
# spine/arm/leg chains by bone name, which is exactly what a hand-written definition would do for a
# MetaHuman-derived skeleton.
step("auto retarget definition", lambda: rc.apply_auto_generated_retarget_definition())
chains = step("read chains", lambda: [str(c.chain_name) for c in rc.get_retarget_chains()])
step("retarget root", lambda: str(rc.get_retarget_root()))

rtg = step("get or create retargeter",
           lambda: get_or_create("RTG_Manny_to_CrowdBase", U.IKRetargeter, U.IKRetargetFactory()))
tc = step("get retargeter controller", lambda: U.IKRetargeterController.get_controller(rtg))
step("set source rig", lambda: tc.set_ik_rig(U.RetargetSourceOrTarget.SOURCE, src_rig))
step("set target rig", lambda: tc.set_ik_rig(U.RetargetSourceOrTarget.TARGET, rig))
step("auto map chains",
     lambda: tc.auto_map_chains(U.AutoMapChainType.EXACT, True))

# Then map explicitly, because auto_map_chains is not reliable here: one run came back with
# ('Spine','Spine') and the next with every target chain mapped to None, same code. The two rigs
# name their chains identically - both skeletons are MetaHuman-derived - so same-name mapping is
# exact, not a guess, and doing it explicitly makes the result the same every time.
def force_map():
    fixed = []
    src_chains = {str(c.chain_name) for c in U.IKRigController.get_controller(src_rig)
                  .get_retarget_chains()}
    for c in rc.get_retarget_chains():
        name = str(c.chain_name)
        if name in src_chains and str(tc.get_source_chain(name)) in ("None", ""):
            tc.set_source_chain(name, name)
            fixed.append(name)
    return fixed

step("force same-name mapping", force_map)
mapped = step("read mapped chains",
              lambda: [(str(c.chain_name), str(tc.get_source_chain(c.chain_name)))
                       for c in rc.get_retarget_chains()][:12])

step("save assets", lambda: (U.EditorAssetLibrary.save_loaded_asset(rig, False),
                             U.EditorAssetLibrary.save_loaded_asset(rtg, False)))

# The retarget itself. duplicate_and_retarget writes a NEW AnimSequence on the target skeleton -
# this is the asset the crowd citizen can actually play, and the only reason any of the above
# exists.
# RunBatchRetarget, not DuplicateAndRetarget.
#
# The engine header says DuplicateAndRetarget is deprecated in favour of RunBatchRetarget, which
# takes an FIKRetargetBatchOperationInputs struct - and, decisively, its AssetsToRetarget field is
# a TArray<FAssetData>, not an array of animation objects. That is why every array element type
# tried against the old call failed to convert, and why the attempt then wedged the editor for 15
# minutes at 550% CPU: reading the header first would have skipped all of it.
src_mesh = U.load_asset("@@src_mesh@@")
reg = U.AssetRegistryHelpers.get_asset_registry()

def do_retarget():
    inputs = U.IKRetargetBatchOperationInputs()
    inputs.set_editor_property("assets_to_retarget",
                               [reg.get_asset_by_object_path(run.get_path_name())])
    inputs.set_editor_property("source_mesh", src_mesh)
    inputs.set_editor_property("target_mesh", tgt_mesh)
    inputs.set_editor_property("ik_retarget_asset", rtg)
    inputs.set_editor_property("search", "MM_")
    inputs.set_editor_property("replace", "CROWD_")
    inputs.set_editor_property("target_path", "@@dest@@")
    res = U.IKRetargetBatchOperation.run_batch_retarget(inputs)
    return [str(a.get_editor_property("package_name")) for a in res] if res else []

out_anim = step("retarget the run", do_retarget)

RESULT.update({"steps": steps, "chains": chains, "mapped": mapped,
               "out_anim": None if not out_anim else [str(a) for a in out_anim]})
""".strip()


if __name__ == "__main__":
    ucv = engine.connect(1280, 720, timeout=600)
    if ucv is None:
        print("UE never became reachable")
        sys.exit(2)
    # The same two cvars capture_walker.py sets, for the same reason: loading IK_Mannequin pulls in
    # SKM_Manny's skeletal mesh, and with async skinned asset compilation left at UnrealCV's default
    # of 2 the first such load blocks the game thread on "Waiting for skinned assets to be ready"
    # and never returns. The first attempt at this script omitted them and wedged the editor for 19
    # minutes without creating a single asset.
    for cvar in ("Editor.AsyncSkinnedAssetCompilation 0", "Editor.AsyncAssetCompilation 0"):
        ucv.client.request(f"vrun {cvar}")
        print(f"  cvar {cvar}")

    # str.replace, not %-formatting. The body is Python source that legitimately contains percent
    # signs - a comment mentioning "550% CPU" was enough to raise "not enough arguments for format
    # string" and cost a whole editor round trip, twice.
    body = BODY
    for key, val in (("src_rig", SRC_RIG), ("tgt_mesh", TGT_MESH), ("run", RUN_ANIM),
                     ("dest", DEST), ("src_mesh", SRC_MESH)):
        body = body.replace("@@" + key + "@@", val)
    r = engine.query(ucv.client.request, body, timeout=900)
    for s in r.get("steps", []):
        mark = "ok " if s.get("ok") else "FAIL"
        print(f"  {mark} {s['step']}: {s.get('value') or s.get('error') or ''}")
    print("chains:", (r.get("chains") or [])[:14])
    print("mapped:", (r.get("mapped") or [])[:14])
    print("retargeted animation:", r.get("out_anim"))
    if not r.get("steps"):
        print(json.dumps(r, indent=1)[:1500])
