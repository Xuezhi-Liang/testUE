#!/usr/bin/env python3
"""Per-map lighting calibration: the smallest sky-light boost that meets the quality bar, and the
exposure bias that goes with it. Runs in the capture session, before the first frame; the result
is fixed for the whole episode, so a place has one appearance inside an episode and the numbers
adapt only between maps.

    factors  : sky-light multipliers tried in order, smallest first
    bar      : near-black <= 3%%, blown <= 1%%, contrast (p99 - p10) >= 80%% of the level's own
    choice   : the first factor whose chosen-bias render meets the bar. None meets it -> the one
               with the least near-black, flagged quality_bar_missed - a night level or a cave is
               reported, not brightened into a grey day.

Every number is a median over N route poses of a histogram computed in the engine on a rendered
frame (exposure_probe): a lighting change counts only when pixels say so.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import exposure_probe as xp   # noqa: E402
import lighting_rig           # noqa: E402

FACTORS = (1.0, 1.5, 2.0, 3.0, 4.0)
# brightness: the chosen render's median must stay within [0.6, 1.6] x the level's own median (a
# floor of 30 on that reference so a pitch-black level does not pin the target at 5). This is what
# keeps the calibration from re-lighting a night scene into a bright day: MedievalTown Nighttime
# "passed" the first three checks at +15.5 EV with stars in a daylit sky.
BAR = {"max_black": 0.03, "max_blown": 0.01, "min_contrast_ratio": 0.80, "p50_ratio": (0.6, 1.6), "p50_floor": 30}


def _row(table, ev):
    return next(t for t in table if t["ev"] == ev)


def calibrate(req, fz, out, factors=FACTORS, n=12, bar=None, lighting=None):
    bar = dict(BAR, **(bar or {}))
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    poses = list(enumerate(xp.pick_poses(fz, n)))
    t0 = time.time()
    # the level as shipped, its own auto-exposure: the contrast reference and the "before" picture
    ref = xp._render_set(req, fz, out / "ref", poses, [("current", None)], xp.W, xp.H, "")["current"]
    ref_contrast = xp.median([r["p99"] - r["p10"] for r in ref])
    ref_agg = {"p10": xp.median([r["p10"] for r in ref]), "p50": xp.median([r["p50"] for r in ref]),
               "p99": xp.median([r["p99"] for r in ref]), "black_frac": xp.median([r["black_frac"] for r in ref]),
               "blown_frac": xp.median([r["blown_frac"] for r in ref]), "contrast": ref_contrast}
    trials, evs = [], None
    for F in factors:
        fill = lighting_rig.apply_fill(req, F)
        if not fill.get("applied"):
            trials.append({"factor": F, "error": fill.get("error")})
            break
        # DAYLIGHT=1: the user wants night levels recorded as day. The brightness band then sits
        # around an absolute mid-grey instead of the level's own (black) median, so the sweep is
        # free to open the exposure as far as the blown/black limits allow.
        import os
        target_p50 = float(os.environ["DAYLIGHT_P50"]) if os.environ.get("DAYLIGHT") == "1" else max(ref_agg["p50"], bar["p50_floor"])
        table, ev = xp.sweep(req, fz, out / f"x{F:g}", evs=evs, n=n, lighting=fill, target_p50=target_p50)
        if ev is None:
            trials.append({"factor": F, "chosen_ev": None, "passes": False, "why": "no bias within the sweep limits"})
            continue
        row = _row(table, ev)
        # the fine grid found once is reused for the next factor - the picture moves by well under
        # a stop between adjacent factors, and a coarse stage per factor is the expensive part
        evs = [round(ev + d, 1) for d in (-1.0, -0.5, 0.0, 0.5, 1.0)]
        contrast = row["p99"] - row["p10"]
        lo, hi = bar["p50_ratio"]
        checks = {"black": row["black_frac"] <= bar["max_black"], "blown": row["blown_frac"] <= bar["max_blown"],
                  "contrast": contrast >= bar["min_contrast_ratio"] * ref_contrast,
                  "brightness": lo * target_p50 <= row["p50"] <= hi * target_p50}
        # how far outside the bar, summed over checks, for the fallback - "least near-black" picked
        # a washed-out x3 on ChemicalPlant over an x2 that missed by half a percent
        violation = (max(0.0, row["black_frac"] - bar["max_black"]) / bar["max_black"]
                     + max(0.0, row["blown_frac"] - bar["max_blown"]) / bar["max_blown"]
                     + max(0.0, bar["min_contrast_ratio"] * ref_contrast - contrast) / max(ref_contrast, 1)
                     + max(0.0, lo * target_p50 - row["p50"]) / target_p50 + max(0.0, row["p50"] - hi * target_p50) / target_p50)
        trials.append({"factor": F, "chosen_ev": ev, "p10": row["p10"], "p50": row["p50"], "p99": row["p99"],
                       "black_frac": row["black_frac"], "blown_frac": row["blown_frac"], "contrast": contrast,
                       "contrast_ratio": contrast / max(ref_contrast, 1), "p50_ratio": row["p50"] / target_p50, "target_p50": target_p50,
                       "violation": violation, "checks": checks, "passes": all(checks.values()),
                       "fill": {k: fill.get(k) for k in ("base_intensity", "fill_skylight")}})
        print(f"[calib] x{F:g}: EV {ev:+.1f}  black {row['black_frac']*100:.1f}%  blown {row['blown_frac']*100:.2f}%  "
              f"contrast {contrast:.0f} ({contrast/max(ref_contrast,1)*100:.0f}% of ref)  p50 {row['p50']} ({row['p50']/target_p50*100:.0f}% of target {target_p50:.0f})"
              f"  -> {'PASS' if all(checks.values()) else 'fail ' + ','.join(k for k, v in checks.items() if not v)}", flush=True)
        if all(checks.values()):
            break
    ok = [t for t in trials if t.get("passes")]
    # factors keep going while the bar is missed, so the loop above already stopped at the first
    # pass; brightness failing on EVERY factor (a night level) leaves ok empty by design
    if ok:
        chosen, missed = ok[0], False
    else:
        cands = [t for t in trials if t.get("chosen_ev") is not None]
        chosen = min(cands, key=lambda t: t["violation"]) if cands else None
        missed = True
    result = {"map_id": fz["map_id"], "poses": n, "factors_tried": [t["factor"] for t in trials], "bar": bar,
              "reference": ref_agg, "trials": trials,
              "chosen_factor": chosen["factor"] if chosen else None, "chosen_ev": chosen["chosen_ev"] if chosen else None,
              "quality_bar_missed": missed, "took_s": round(time.time() - t0, 1)}
    # leave the session at the chosen factor (the last trial may be a larger one)
    if chosen and trials and trials[-1].get("factor") != chosen["factor"]:
        lighting_rig.apply_fill(req, chosen["factor"])
    (out / "calibration.json").write_text(json.dumps(result, indent=1))
    if chosen:
        print(f"[calib] chosen: sky light x{chosen['factor']:g}, EV {chosen['chosen_ev']:+.1f}"
              + ("  (QUALITY BAR MISSED - least near-black of the tried factors)" if missed else "") + f"  in {result['took_s']:.0f} s", flush=True)
    else:
        print("[calib] nothing usable: every factor failed to find a bias", flush=True)
    return result
