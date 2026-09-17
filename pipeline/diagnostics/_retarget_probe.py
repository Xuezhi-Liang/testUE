#!/usr/bin/env python3
"""What retargeting API does this build expose to Python, and do the skeletons line up?

The crowd has no run animation and the project's only IK retargeters go between the UE4 and UE5
mannequins, so putting a run on a CitySampleCrowd citizen means building an IK Rig for SK_Base and
a retargeter into it. Before writing any of that, find out which classes and methods actually exist
here - guessing API shapes has already cost this session several editor restarts - and confirm the
two skeletons use the same bone names, because chain auto-mapping is by name.

Read-only. Nothing is created.
"""
import json
import sys
from pathlib import Path

P = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
sys.path.insert(0, str(P))
import engine  # noqa: E402

BODY = """
import unreal as U
out = {}

def has(name):
    return hasattr(U, name)

out["classes"] = {n: has(n) for n in (
    "IKRigDefinition", "IKRigDefinitionFactory", "IKRigController",
    "IKRetargeter", "IKRetargetFactory", "IKRetargeterController",
    "IKRetargetBatchOperation", "RetargetChainSettings", "AutoMapChainType")}

for cls in ("IKRigController", "IKRetargeterController", "IKRetargetBatchOperation"):
    if has(cls):
        out[cls + "_methods"] = sorted(m for m in dir(getattr(U, cls))
                                       if not m.startswith("_"))[:60]

# Do the bone names match? Chain auto-mapping is by name, so this decides whether a retarget is a
# configuration job or a rigging job.
def bones(path):
    m = U.load_asset(path)
    if m is None:
        return None
    sk = m.get_editor_property("skeleton")
    try:
        return [str(b) for b in sk.get_editor_property("bone_tree")]
    except Exception:
        return "bone_tree not readable"

src = U.load_asset("/Game/Characters/Mannequins/Meshes/SKM_Manny")
tgt = U.load_asset("/Game/CitySampleCrowd/Character/Male/NormalWeight/Meshes/m_tal_nrw_base")
out["source_mesh"] = None if src is None else str(src.get_name())
out["target_mesh"] = None if tgt is None else str(tgt.get_name())
for name, m in (("source", src), ("target", tgt)):
    if m is None:
        continue
    sk = m.get_editor_property("skeleton")
    out[name + "_skeleton"] = str(sk.get_name())
    try:
        n = sk.get_editor_property("bone_tree")
        out[name + "_bone_count"] = len(n)
    except Exception as e:
        out[name + "_bone_error"] = str(e)[:80]

# The existing mannequin IK rig - if it loads, the target rig can be modelled on it.
rig = U.load_asset("/Game/Characters/Mannequins/Rigs/IK_Mannequin")
out["IK_Mannequin_loads"] = rig is not None
if rig is not None:
    out["IK_Mannequin_type"] = type(rig).__name__

RESULT.update(out)
""".strip()

if __name__ == "__main__":
    ucv = engine.connect(1280, 720, timeout=600)
    if ucv is None:
        print("UE never became reachable")
        sys.exit(2)
    print(json.dumps(engine.query(ucv.client.request, BODY, timeout=600), indent=1)[:4000])
