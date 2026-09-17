#!/usr/bin/env python3
"""Record a frozen route with sky-light fill x FACTOR and MANUAL exposure at EXPOSURE_BIAS_EV
(+ optional local exposure via LE_SHADOW / LE_DETAIL, history mode via CAPTURE_HISTORY).
    MAP=... FROZEN=... OUT=... FACTOR=4 EXPOSURE_BIAS_EV=11.5 python3 fill_fixed_record.py
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE))
import engine, capture_engine as cape, lighting_rig  # noqa: E402
MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"]); F = float(os.environ.get("FACTOR", "1"))
fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP; OUT.mkdir(parents=True, exist_ok=True)
ucv = engine.connect(1280, 720)
if ucv is None: raise SystemExit("UE never became reachable")
req = ucv.client.request
skyfix = cape.ensure_dynamic_sky(req); fill = lighting_rig.apply_fill(req, F)
if not fill.get("applied"): raise SystemExit(f"fill failed: {fill.get('error')}")
os.environ["LIGHTING_MODE"] = "level"; os.environ["EXPOSURE_MODE"] = "fixed"
t0 = time.time(); ep = cape.capture(str(FROZEN), out_root=OUT / "asis", ucv=ucv)
(OUT / "asis.json").write_text(json.dumps({"map_id": MAP, "frozen": str(FROZEN), "episode": str(ep), "factor": F, "bias": os.environ.get("EXPOSURE_BIAS_EV"),
    "history": os.environ.get("CAPTURE_HISTORY"), "le_shadow": os.environ.get("LE_SHADOW"), "record_seconds": round(time.time() - t0, 1)}, indent=1))
print("wrote", OUT / "asis.json")
