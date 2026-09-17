#!/usr/bin/env python3
"""Run one task end to end on a single UnrealCV connection.

    freeze -> capture -> package -> QA video

Everything shares one connection and one spawned rig deliberately. Reconnecting after a client
disconnect has been observed to kill this editor build outright: UnrealCV logs a second
connection thread with `Socket: NULL` and the process dies on the next request, with nothing in
the log. Connecting once per editor lifetime avoids that entirely, and is faster anyway.

    MAP=... PORT=9208 python3 run_task.py tasks/downtown_pilot.json [--skip-qa]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture as cap  # noqa: E402
import capture_engine as cape  # noqa: E402
import engine  # noqa: E402
import freeze as fr  # noqa: E402


def run(task_path, skip_qa=False):
    task = json.loads(Path(task_path).read_text())
    W, H = task["camera"]["resolution"]
    t0 = time.time()

    print(f"[run] task {task['task_id']}  map {task['map_id']}")
    ucv = engine.connect(W, H)
    if ucv is None:
        print("[run] UE never became reachable")
        return 2
    # The rig is a Character blueprint UnrealCV spawns at the world origin. On maps whose origin is
    # inside geometry the spawn produces no actor at all and every query against it returns "error";
    # MiddleEast and WildWest both failed here a second into the run. freeze() handles rig=None, so
    # this is a warning rather than a dead end - only the pawn-based reachability check is lost.
    rig = engine.Rig(ucv, W, H, float(task["camera"]["fov_deg"]))
    try:
        fov = rig.configure()
        print(f"[run] rig {rig.name}, camera {rig.cam}, fov {fov:.2f}")
    except Exception as e:
        print(f"[run] no camera-host pawn on this map ({type(e).__name__}: {str(e)[:120]}); "
              f"continuing without it")
        rig = None

    frozen_path, frozen = fr.freeze(task, ucv=ucv, rig=rig)
    rc = frozen.get("reachability") or {}
    if rc and not rc.get("reachable"):
        bad = rc["unreachable_samples"][:3]
        print(f"[run] REFUSING to capture: {len(rc['unreachable_samples'])} sampled poses are "
              f"pushed sideways by geometry (max lateral {rc['max_lateral_error_cm']} cm). "
              f"Capsule sweeps trace the Visibility channel, which is not what blocks a Pawn, so "
              f"a clean sweep is not proof. First offenders: {bad}")
        if rig:
            rig.destroy()
        return 4

    if not frozen["collision"]["collision_free"]:
        print(f"[run] REFUSING to capture: the frozen route is not collision free "
              f"({frozen['collision']['collision_count']} sweeps hit, "
              f"{frozen['collision']['penetration_count']} near-plane penetrations). "
              f"Fix the route, not the recording.")
        if rig:
            rig.destroy()
        return 3

    # The in-engine capture needs no rig: it places the scene-capture components on the frozen
    # pose itself. The rig existed only to own a CameraComponent for the external loop, and it is
    # released before capture so nothing of ours is in the shot.
    if rig:
        rig.destroy()
    if os.environ.get("CAPTURE_MODE", "engine") == "engine":
        ep = cape.capture(frozen_path, ucv=ucv)
    else:
        rig2 = engine.Rig(ucv, W, H, float(task["camera"]["fov_deg"]))
        rig2.configure()
        ep = cap.capture(frozen_path, ucv=ucv, rig=rig2)
        rig2.destroy()

    import package as pk
    pk.build(ep)
    if not skip_qa:
        import qa_video as qv
        qv.render(ep)
        # the manifest is written by package(), which runs before the QA video exists; refresh it
        # so files.sha256 actually covers every delivered file rather than all but the last one
        n = pk.sha256_manifest(ep)
        print(f"[run] manifest refreshed after QA render: {n} files")
    print(f"[run] done in {time.time()-t0:.0f}s -> {ep}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--skip-qa", action="store_true")
    a = ap.parse_args()
    return run(a.task, a.skip_qa)


if __name__ == "__main__":
    sys.exit(main())
