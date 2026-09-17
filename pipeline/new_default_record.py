#!/usr/bin/env python3
"""Record a frozen route with the template's CURRENT render defaults, overriding whatever render
block the frozen file carries (frozen files predate these settings).
    MAP=... FROZEN=... OUT=... python3 new_default_record.py
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE))
import engine, capture_engine as cape  # noqa: E402
MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"])
fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP; OUT.mkdir(parents=True, exist_ok=True)
rend = json.loads((HERE / "tasks" / "longvideo_template.json").read_text())["render"]
os.environ["CAPTURE_HISTORY"] = str(rend.get("history_mode", 0))
os.environ["CAPTURE_SUPERSAMPLE"] = os.environ.get("CAPTURE_SUPERSAMPLE") or str(rend.get("supersample", 1))   # the frozen file's render block predates this key
os.environ["EXPOSURE_MODE"] = str(rend.get("exposure", "auto"))
os.environ["LIGHTING_MODE"] = str(rend.get("lighting", "level"))
os.environ["SKYLIGHT_FACTOR"] = os.environ.get("FORCE_SKYLIGHT_FACTOR") or str(rend.get("skylight_factor", "auto"))
le = rend.get("local_exposure") or {}
os.environ["LE_SHADOW"] = str(le.get("shadow_scale", 0)); os.environ["LE_HIGHLIGHT"] = str(le.get("highlight_scale", 0)); os.environ["LE_DETAIL"] = str(le.get("detail_strength", 0))
print("[newdefault] render:", json.dumps({k: rend.get(k) for k in ("history_mode", "supersample", "exposure", "lighting", "skylight_factor", "local_exposure")}), flush=True)
ucv = engine.connect(1280, 720)
if ucv is None: raise SystemExit("UE never became reachable")
sub = os.environ.get("OUT_SUB", "newdefault")
t0 = time.time(); ep = cape.capture(str(FROZEN), out_root=OUT / sub, ucv=ucv)
summ = json.loads((Path(ep) / "capture_summary.json").read_text())
(OUT / f"{sub}.json").write_text(json.dumps({"map_id": MAP, "frozen": str(FROZEN), "episode": str(ep), "render": rend, "cvars": os.environ.get("CAPTURE_CVARS"),
    "lighting": summ.get("lighting"), "record_seconds": round(time.time() - t0, 1), "engine_fps": summ.get("engine_fps")}, indent=1))
print("wrote", OUT / f"{sub}.json")
