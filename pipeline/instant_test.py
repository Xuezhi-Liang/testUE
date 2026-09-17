#!/usr/bin/env python3
"""One map, one connection: sky-light fill x F, then

  1. probes at 12 route poses under instant auto-exposure with local-exposure shadow scale in
     {1.0 (off), 0.8, 0.65, 0.5}; pick the first meeting the bar (near-black <= 3%, blown <= 1%,
     contrast >= 80% of the off setting, median grey within 0.6-1.6x of it); else least violation
  2. record the frozen route twice: instant auto-exposure alone, and instant + the chosen local
     exposure.

    MAP=... FROZEN=... OUT=... [FACTOR=1] [AE_SPEED=100] python3 instant_test.py
"""
import json, os, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine, capture_engine as cape, lighting_rig, exposure_probe as xp  # noqa: E402

MAP = os.environ["MAP"]; FROZEN = Path(os.environ["FROZEN"]); OUT = Path(os.environ["OUT"]); F = float(os.environ.get("FACTOR", "1"))
SCALES = (1.0, 0.8, 0.65, 0.5); DETAIL = 1.0
BAR = {"max_black": 0.03, "max_blown": 0.01, "min_contrast_ratio": 0.80, "p50_ratio": (0.6, 1.6)}


def probe_set(req, fz, out, poses, shadow):
    fov = float(fz["task"]["camera"]["fov_deg"]); rows = []
    for k, (i, p) in poses:
        r = engine.rgb_capture(req, (p["x_cm"], p["y_cm"], p["z_cm"]), p["yaw_deg"], p["pitch_deg"], fov, xp.W, xp.H,
                               str(out / f"pose{k:02d}_f{i:06d}_le{shadow:.2f}.png"), manual_exposure=False,
                               le_shadow=(0.0 if shadow >= 1.0 else shadow), le_detail=(0.0 if shadow >= 1.0 else DETAIL))
        if r.get("ok"): rows.append(r)
    med = lambda k: xp.median([r[k] for r in rows])
    return {"shadow_scale": shadow, "n": len(rows), "p10": med("p10"), "p50": med("p50"), "p99": med("p99"),
            "black_frac": med("black_frac"), "blown_frac": med("blown_frac"), "contrast": med("p99") - med("p10")}


def main():
    fz = json.loads(FROZEN.read_text()); assert fz["map_id"] == MAP; OUT.mkdir(parents=True, exist_ok=True)
    ucv = engine.connect(1280, 720)
    if ucv is None: raise SystemExit("UE never became reachable")
    req = ucv.client.request
    skyfix = cape.ensure_dynamic_sky(req); fill = lighting_rig.apply_fill(req, F)
    if not fill.get("applied"): raise SystemExit(f"fill failed: {fill.get('error')}")
    poses = list(enumerate(xp.pick_poses(fz, 12))); pdir = OUT / "le_probes"; pdir.mkdir(exist_ok=True)
    trials = []; ref = None; chosen = None
    for sc in SCALES:
        t = probe_set(req, fz, pdir, poses, sc)
        if ref is None: ref = t
        lo, hi = BAR["p50_ratio"]
        checks = {"black": t["black_frac"] <= BAR["max_black"], "blown": t["blown_frac"] <= BAR["max_blown"],
                  "contrast": t["contrast"] >= BAR["min_contrast_ratio"] * ref["contrast"],
                  "brightness": lo * max(ref["p50"], 30) <= t["p50"] <= hi * max(ref["p50"], 30)}
        t["checks"] = checks; t["passes"] = all(checks.values())
        t["violation"] = (max(0, t["black_frac"] - BAR["max_black"]) / BAR["max_black"] + max(0, t["blown_frac"] - BAR["max_blown"]) / BAR["max_blown"]
                          + max(0, BAR["min_contrast_ratio"] * ref["contrast"] - t["contrast"]) / max(ref["contrast"], 1))
        trials.append(t)
        print(f"[le] shadow {sc:.2f}: black {t['black_frac']*100:.1f}%  blown {t['blown_frac']*100:.2f}%  contrast {t['contrast']:.0f} ({t['contrast']/max(ref['contrast'],1)*100:.0f}%)  p50 {t['p50']}  -> {'PASS' if t['passes'] else 'fail ' + ','.join(k for k, v in checks.items() if not v)}", flush=True)
        if t["passes"] and sc < 1.0:
            chosen = t; break
        if t["passes"] and sc >= 1.0:
            chosen = t; break        # already fine without local exposure
    if chosen is None:
        chosen = min(trials, key=lambda t: t["violation"]); missed = True
    else:
        missed = False
    le_shadow = 0.0 if chosen["shadow_scale"] >= 1.0 else chosen["shadow_scale"]
    print(f"[le] chosen shadow scale {chosen['shadow_scale']:.2f}" + (" (bar missed)" if missed else ""), flush=True)

    out = {"map_id": MAP, "frozen": str(FROZEN), "factor": F, "ae_speed": float(os.environ.get("AE_SPEED", "100")),
           "skylight_fix": skyfix, "fill": fill, "le_trials": trials, "le_chosen": chosen["shadow_scale"], "le_bar_missed": missed}
    os.environ["LIGHTING_MODE"] = "level"; os.environ["EXPOSURE_MODE"] = "auto_instant"
    for tag, sh in (("instant", 0.0), ("instant_le", le_shadow)):
        if tag == "instant_le" and le_shadow == 0.0:
            out[tag] = {"skipped": "local exposure not needed on this map"}; continue
        os.environ["LE_SHADOW"] = str(sh); os.environ["LE_DETAIL"] = str(DETAIL if sh else 0.0)
        t1 = time.time(); ep = cape.capture(str(FROZEN), out_root=OUT / tag, ucv=ucv)
        out[tag] = {"episode": str(ep), "record_seconds": round(time.time() - t1, 1), "le_shadow": sh}
        print(f"[test] {tag}: {ep} in {time.time()-t1:.0f} s", flush=True)
    (OUT / "instant.json").write_text(json.dumps(out, indent=1)); print("wrote", OUT / "instant.json")


if __name__ == "__main__":
    main()
