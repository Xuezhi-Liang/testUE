#!/usr/bin/env python3
"""Append shard plans for new maps to longvideo_shards.json, by the rule the first 31 followed.

    python3 gen_shards.py <slug> [<slug> ...]        # appends; prints the plan
    python3 gen_shards.py --dry <slug> ...           # prints only

The rule, read back from the 31 shards already in the file rather than invented here:

  one pass         = 4.0 x the survey's pure tour time. The tour is walking only; the action mix
                     (backward, look, scan, hold) measured 3.2-4.3x on the nine planned maps,
                     median 4.0. Where a shard actually ran, the estimate matched delivery:
                     Tokyo 8.3 h planned / 8.30 h delivered, Pyramids 20.0 / 20.00.
  episode          = 8 passes or 20 h, whichever is smaller. On small maps 8 passes binds.
  shards           = ceil(episode_h / 5), each ~5 h, passes and hours split evenly. A shard is its
                     own complete episode with its own seed - see gen_task.py for why passes are
                     never split across machines.
"""
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
SURVEY = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/_coverage_survey.json")
PREFLIGHT = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/longvideo_preflight_list.json")
SHARDS = PIPE / "longvideo_shards.json"

MIX_FACTOR = 4.0
# 17 Sep: two passes per map, one episode per map, no cap and no shard split (the user dropped the
# "8 passes or 20 h, 5 h shards" rule). The per-map max_duration_s written below is a runaway guard
# at 3x the two-pass estimate - passes alternate ~4x/~8x the tour on Tokyo - not a target.
TARGET_PASSES = 2
CAP_H = math.inf
SHARD_H = math.inf
# Pass 2 is the study-style fill pass: same distance, about twice the action density, so it costs
# ~2x the covering pass. The episode estimate is therefore one_pass x (1 + PASS2_FACTOR).
PASS2_FACTOR = 2.0
RUNAWAY_FACTOR = 2.0
MAP_IDS_EXTRA = {   # maps the preflight list does not carry
    "Game_Medieval_Environment_Medieval_Castle_Vol1_Maps_CF_01_Demo_Scene":
        "/Game/Medieval_Environment/Medieval_Castle_Vol1/Maps/CF_01_Demo_Scene",
}


def plan_for(slug, survey, pre, seed0, passes_per_shard=None, rnd="", cap_h=None):
    s = survey[slug]
    one_pass_min = round(s["tour_min"] * MIX_FACTOR)
    episode_h = min((1.0 + PASS2_FACTOR * (TARGET_PASSES - 1)) * one_pass_min / 60.0, CAP_H)
    if passes_per_shard:
        # Fixed passes per shard, shards = 8 / that. With 1 pass per shard every machine records
        # exactly one complete cover from a fresh seed, and eight machines are eight covers.
        # Measured reason to prefer this over fewer, longer shards: pass durations ALTERNATE -
        # Tokyo s00's passes were 78, 159, (13) min, and the full-plan projection was
        # [113k, 257k, 122k, 244k, ...] frames - odd passes ~4x the tour, even passes ~8x. A
        # 4-pass shard capped at 4.15 h therefore completed 2 passes, not 4. One pass per shard
        # is the only split that actually delivers the 8 covers the task asks for.
        passes = passes_per_shard
        n = max(1, math.ceil(TARGET_PASSES / passes))
        per_h = min(passes * one_pass_min / 60.0 * 1.25, CAP_H / n)   # 25% headroom on the cap
        if cap_h is not None:
            # The survey's pass estimate is for the survey's network. With head-clearance fixed the
            # real network can be much larger: MedievalCastle's survey said 51 min a pass and the
            # instance measured 1.95 h. An explicit cap for such maps, instead of a rule that
            # silently truncates a pass at 64 minutes.
            per_h = cap_h
    else:
        n = 1 if not math.isfinite(SHARD_H) else max(1, math.ceil(episode_h / SHARD_H))
        passes = max(1, round(TARGET_PASSES / n))
        per_h = episode_h / n * RUNAWAY_FACTOR
    map_id = (pre.get(slug) or {}).get("map_id") or MAP_IDS_EXTRA.get(slug)
    if not map_id:
        raise SystemExit(f"no map_id for {slug}")
    return [{
        "shard_id": f"{slug}__{rnd}s{i:02d}", "slug": slug, "map_id": map_id,
        "shard": i, "shards": n, "seed": seed0 + i,
        "target_passes": passes, "max_duration_s": round(per_h * 3600.0, 1),
        "one_pass_min_estimate": one_pass_min, "estimated_hours": round(per_h, 2),
    } for i in range(n)]


def main(argv):
    dry = "--dry" in argv
    replace = "--replace" in argv
    pps = None; body = {}; rnd = ""; cap_h = None
    for a in argv:
        if a.startswith("--round="):
            # A second pass over a map whose first-round shards already exist. Shard ids must
            # not collide: state beacons, ledgers and S3 prefixes are all keyed by shard_id.
            rnd = f"r{int(a.split('=', 1)[1])}"
        if a.startswith("--passes-per-shard="):
            pps = int(a.split("=", 1)[1])
        elif a.startswith("--ground-clearance="):
            body["ground_clearance_cm"] = float(a.split("=", 1)[1])
        elif a.startswith("--corridor-clear="):
            body["corridor_clear_cm"] = float(a.split("=", 1)[1])
        elif a.startswith("--cap-hours="):
            cap_h = float(a.split("=", 1)[1])
    slugs = [a for a in argv if not a.startswith("--")]
    survey = {r["map"]: r for r in json.loads(SURVEY.read_text())}
    pre = {m["slug"]: m for m in json.loads(PREFLIGHT.read_text())}
    existing = json.loads(SHARDS.read_text())
    if replace:
        before = len(existing)
        existing = [x for x in existing if x["slug"] not in slugs]
        print(f"  --replace: 移除 {before - len(existing)} 条旧计划")
    have = {s["slug"] for s in existing}
    seed = max(s["seed"] for s in existing) + 100      # a gap, so nothing collides with a retry
    seed = (seed // 100) * 100
    new = []
    for slug in slugs:
        if slug in have and not rnd:
            print(f"  跳过 {slug}: 已在计划中（要加一轮用 --round=N）"); continue
        if slug not in survey:
            print(f"  跳过 {slug}: 不在 _coverage_survey.json"); continue
        rows = plan_for(slug, survey, pre, seed, pps, rnd, cap_h); seed += 10
        for r in rows:
            r.update(body)
        new += rows
        r = rows[0]
        print(f"  {slug[5:][:46]:48s} {len(rows)} 片 x {r['target_passes']} 遍 x "
              f"{r['estimated_hours']:.2f} h  (一遍 {r['one_pass_min_estimate']} min)  "
              f"= {sum(x['estimated_hours'] for x in rows):.1f} h")
    print(f"  合计 {len(new)} 片, {sum(x['estimated_hours'] for x in new):.1f} h")
    if not dry and new:
        SHARDS.write_text(json.dumps(existing + new, indent=1) + "\n")
        print(f"  已写入 {SHARDS} ({len(existing)} -> {len(existing) + len(new)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
