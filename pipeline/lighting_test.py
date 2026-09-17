#!/usr/bin/env python3
"""Current lighting vs the Dubai rig on one map, one UnrealCV connection:

  1. N poses along a frozen route rendered under the level's own lighting + auto-exposure
  2. apply lighting_rig (hide the level's global lights and post-process, spawn the rig, auto-exposure off)
  3. the same N poses again
  4. record the frozen route under the rig (the "current" recording is reused from exposure_test)

    MAP=/Game/... FROZEN=<frozen.json> OUT=<dir> [N=24] python3 lighting_test.py
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine, capture_engine as cape, lighting_rig, exposure_probe as xp  # noqa: E402

MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"])
N = int(os.environ.get("N", "24")); W, H = 640, 360


def main():
    fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP
    OUT.mkdir(parents=True, exist_ok=True)
    ucv = engine.connect(1280, 720)
    if ucv is None:
        raise SystemExit("UE never became reachable")
    req = ucv.client.request
    skyfix = cape.ensure_dynamic_sky(req)
    poses = list(enumerate(xp.pick_poses(fz, N)))
    t0 = time.time()
    cur = xp._render_set(req, fz, OUT / "probes", poses, [("current", None)], W, H, "")
    rig = lighting_rig.apply_rig(req)
    if not rig.get("applied"):
        raise SystemExit(f"rig failed: {rig.get('error')}")
    new = xp._render_set(req, fz, OUT / "probes", poses, [("rig", None)], W, H, "")
    print(f"[test] probes done in {time.time()-t0:.0f} s", flush=True)
    os.environ["LIGHTING_RIG"] = "0"      # rig is already applied to this session
    os.environ["EXPOSURE_MODE"] = "off"
    root = OUT / "rig"; root.mkdir(exist_ok=True)
    t1 = time.time()
    ep = cape.capture(str(FROZEN), out_root=root, ucv=ucv)
    (OUT / "test.json").write_text(json.dumps({
        "map_id": MAP, "frozen": str(FROZEN), "skylight_fix": skyfix, "rig": rig,
        "probes": {"current": cur["current"], "rig": new["rig"]},
        "episode_rig": str(ep), "record_seconds": round(time.time() - t1, 1)}, indent=1))
    print("wrote", OUT / "test.json")


if __name__ == "__main__":
    main()
