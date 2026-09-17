#!/usr/bin/env python3
"""Sky-light fill on one map, one UnrealCV connection: for each factor, boost the level's own sky
light, sweep the exposure bias, record the frozen route at that bias. The author's sun, sky and
grading stay. The "current" recording (level lighting, auto-exposure) comes from exposure_test.py.

    MAP=... FROZEN=... OUT=... [FACTORS=1,2,3] [N=24] python3 fill_test.py
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine, capture_engine as cape, lighting_rig, exposure_probe as xp  # noqa: E402

MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"])
FACTORS = [float(x) for x in os.environ.get("FACTORS", "1,2,3").split(",")]
N = int(os.environ.get("N", "24"))


def main():
    fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP
    OUT.mkdir(parents=True, exist_ok=True)
    ucv = engine.connect(1280, 720)
    if ucv is None:
        raise SystemExit("UE never became reachable")
    req = ucv.client.request
    skyfix = cape.ensure_dynamic_sky(req)
    poses = list(enumerate(xp.pick_poses(fz, N)))
    results = {"map_id": MAP, "frozen": str(FROZEN), "skylight_fix": skyfix, "variants": {}}
    # reference probes: the level as shipped, auto-exposure, before any fill
    cur = xp._render_set(req, fz, OUT / "probes", poses, [("current", None)], 640, 360, "")
    results["probes_current"] = cur["current"]
    for F in FACTORS:
        tag = f"fill_x{F:g}"
        fill = lighting_rig.apply_fill(req, F)
        if not fill.get("applied"):
            raise SystemExit(f"fill x{F} failed: {fill.get('error')}")
        table, ev = xp.sweep(req, fz, OUT / "sweep" / tag, lighting=fill)
        if ev is None:
            print(f"[test] {tag}: no bias within limits, skipping the recording", flush=True)
            results["variants"][tag] = {"factor": F, "fill": fill, "sweep": table, "chosen_ev": None}
            continue
        if os.environ.get("SKIP_RECORD") == "1":
            results["variants"][tag] = {"factor": F, "fill": fill, "sweep": table, "chosen_ev": ev}
            (OUT / "test.json").write_text(json.dumps(results, indent=1))
            continue
        os.environ["LIGHTING_MODE"] = "level"          # fill already applied to the session
        os.environ["EXPOSURE_MODE"] = "fixed"
        os.environ["EXPOSURE_BIAS_EV"] = str(ev)
        root = OUT / tag; root.mkdir(exist_ok=True)
        t1 = time.time()
        ep = cape.capture(str(FROZEN), out_root=root, ucv=ucv)
        results["variants"][tag] = {"factor": F, "fill": fill, "sweep": table, "chosen_ev": ev,
                                    "episode": str(ep), "record_seconds": round(time.time() - t1, 1)}
        print(f"[test] {tag}: EV {ev:+.1f}, recorded in {time.time()-t1:.0f} s", flush=True)
        (OUT / "test.json").write_text(json.dumps(results, indent=1))
    (OUT / "test.json").write_text(json.dumps(results, indent=1))
    print("wrote", OUT / "test.json")


if __name__ == "__main__":
    main()
