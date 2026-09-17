#!/usr/bin/env python3
"""Install SimWorldCapture into gym_citynavRuntime and add the modules it needs.

Run inside the enroot container, where /home/ue4 exists. Idempotent: re-running replaces the
sources and leaves an already-patched Build.cs alone.
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path("/home/ue4/simworld/Source/gym_citynavRuntime")

DEPS = '''        // SimWorldCapture needs these:
        //   RHI + RenderCore   FRHIGPUTextureReadback, the only float-safe depth readback on
        //                      Vulkan. ReadLinearColorPixels quantises to 8 bits and fires a
        //                      checkf on R32F, which is the SIGSEGV we spent two editor crashes
        //                      finding.
        //   ImageCore/Wrapper  EXR encode for depth
        //   NavigationSystem   ANavMeshBoundsVolume, ARecastNavMesh, UNavigationSystemV1
        //   UnrealEd           UCubeBuilder. A volume spawned from code has no brush and
        //                      scaling an absent brush is a no-op, so this is the only way to
        //                      give it real bounds. Editor-only, which is why it is guarded and
        //                      why this module being UncookedOnly matters.
        PrivateDependencyModuleNames.AddRange(new string[] {
            "RHI",
            "RenderCore",
            "ImageCore",
            "ImageWrapper",
            "NavigationSystem",
            "Json",             // parsing the frozen trajectory inside the engine
        });
        if (Target.bBuildEditor)
        {
            PrivateDependencyModuleNames.Add("UnrealEd");
        }'''


def main():
    if not SRC.is_dir():
        print(f"[install] {SRC} not found - run this inside the container")
        return 1

    for name, sub in (("SimWorldCapture.h", "Public"), ("SimWorldCapture.cpp", "Private"),
                      ("SimWorldCaptureActor.h", "Public"),
                      ("SimWorldCaptureActor.cpp", "Private")):
        dst = SRC / sub / name
        shutil.copy2(HERE / name, dst)
        # UnrealBuildTool decides from mtimes whether anything changed; a copy that lands older
        # than the existing object files makes UBT report "Succeeded" without compiling, so the
        # binary keeps the old behaviour while the source says otherwise.
        dst.touch()
        print(f"[install] {dst}")

    build = SRC / "gym_citynavRuntime.Build.cs"
    text = build.read_text()
    if "SimWorldCapture needs these" in text:
        print("[install] Build.cs already patched")
    else:
        anchor = "        PrivateDependencyModuleNames.AddRange(new string[] {});"
        if anchor not in text:
            print("[install] could not find the PrivateDependencyModuleNames anchor in Build.cs")
            return 2
        bak = SRC / "gym_citynavRuntime.Build.cs.bak"
        if not bak.exists():
            shutil.copy2(build, bak)
        build.write_text(text.replace(anchor, DEPS, 1))
        build.touch()
        print(f"[install] Build.cs patched (backup at {bak.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
