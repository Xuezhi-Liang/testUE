#!/usr/bin/env python3
"""Record a frozen route as shipped: the level's own lighting (plus the session sky-light fix every
recording gets) and its own auto-exposure - the reference the calibrated recording is compared to.
    MAP=... FROZEN=... OUT=... python3 asis_record.py
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine, capture_engine as cape  # noqa: E402

MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"])
fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP
OUT.mkdir(parents=True, exist_ok=True)
ucv = engine.connect(1280, 720)
if ucv is None:
    raise SystemExit("UE never became reachable")
os.environ["LIGHTING_MODE"] = "level"; os.environ["EXPOSURE_AUTO"] = "1"
t0 = time.time(); ep = cape.capture(str(FROZEN), out_root=OUT / "asis", ucv=ucv)
(OUT / "asis.json").write_text(json.dumps({"map_id": MAP, "frozen": str(FROZEN), "episode": str(ep), "record_seconds": round(time.time() - t0, 1),
                                           "mode": "level lighting + auto-exposure (as shipped)"}, indent=1))
print("wrote", OUT / "asis.json")
