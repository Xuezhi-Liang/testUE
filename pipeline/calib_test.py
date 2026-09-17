#!/usr/bin/env python3
"""One map: calibrate (factor + bias), then record the frozen route with the result. One connection.
    MAP=... FROZEN=... OUT=... python3 calib_test.py
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine, capture_engine as cape, exposure_probe as xp, lighting_calibrate as lc  # noqa: E402

MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"])


def main():
    fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP
    OUT.mkdir(parents=True, exist_ok=True)
    ucv = engine.connect(1280, 720)
    if ucv is None:
        raise SystemExit("UE never became reachable")
    req = ucv.client.request
    skyfix = cape.ensure_dynamic_sky(req)
    res = lc.calibrate(req, fz, OUT / "calib", lighting=skyfix)
    out = {"map_id": MAP, "frozen": str(FROZEN), "skylight_fix": skyfix, "calibration": res}
    if res.get("chosen_factor") is not None:
        os.environ["LIGHTING_MODE"] = "level"     # the session already sits at the chosen factor
        os.environ["EXPOSURE_MODE"] = "fixed"; os.environ["EXPOSURE_BIAS_EV"] = str(res["chosen_ev"])
        t1 = time.time(); ep = cape.capture(str(FROZEN), out_root=OUT / "rec", ucv=ucv)
        out["episode"] = str(ep); out["record_seconds"] = round(time.time() - t1, 1)
    # and the same route as shipped (level lighting, auto-exposure) for the page, if not already on disk
    (OUT / "test.json").write_text(json.dumps(out, indent=1)); print("wrote", OUT / "test.json")


if __name__ == "__main__":
    main()
