#!/usr/bin/env python3
"""Write one shard's task: a random walk over one map, sized to this shard's share.

    python3 gen_task.py <shard_id>

A shard is not a slice of one long episode. Splitting the passes of a single episode across
machines would need every machine to build the same road network and the same chained walk -
`prepare_walks` starts each pass where the last one ended, and an independent start once put the
camera 41.4 m away in a single frame - and the network comes from a live nav synthesis that is not
identical across machines: the same map exported 2414 m2 of region once and 5878 m2 another time.

So a shard is its own complete episode with its own seed, internally continuous, covering the map
`target_passes` times. Shards of one map add up to the footage a single long episode would have
given, and what they give up is the long revisit interval: nothing in a 5 h shard is ever seen
5 h apart, where a 20 h episode reaches 19 h. That was accepted deliberately - this dataset is not
for the revisit task.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent


def main(shard_id):
    shards = {s["shard_id"]: s for s in
              json.loads((PIPE / "longvideo_shards.json").read_text())}
    sh = shards.get(shard_id)
    if sh is None:
        raise SystemExit(f"{shard_id} is not in longvideo_shards.json ({len(shards)} shards)")
    task = json.loads((PIPE / "tasks" / "longvideo_template.json").read_text())
    task.update(task_id=f"lv_{shard_id}", map_id=sh["map_id"], seed=int(sh["seed"]),
                target_passes=int(sh["target_passes"]),
                max_duration_s=float(sh["max_duration_s"]),
                allow_short_episode=True,
                min_frames=int(float(task["fps"]) * 600))   # ten minutes
    # Per-map body overrides. Ground clearance is not one number: 6 cm is the flat-street profile
    # and turns every door sill and stair on a courtyard map into a wall (Hwaseong kept 22% of its
    # network at 6 cm). The shard plan may say otherwise for such maps.
    for k in ("ground_clearance_cm", "corridor_clear_cm"):
        if sh.get(k) is not None:
            task.setdefault("body", {})[k] = float(sh[k])
    # Exposure bias is per map (exposure_probe.py measures it); the capture refuses a fixed
    # exposure without one, so a shard plan that lacks it will refuse at capture, not record black.
    if sh.get("exposure_bias_ev") is not None:
        task.setdefault("render", {})["exposure_bias_ev"] = float(sh["exposure_bias_ev"])
    # Per-map lighting mode. `fill` (the template default) hung the editor on two INDOOR maps on
    # 17 Sep (Cave, SICKA Interior2: the captured-scene sky light recapture never returned); those
    # shards say `level` and keep the author's lighting untouched.
    if sh.get("lighting"):
        task.setdefault("render", {})["lighting"] = sh["lighting"]
    if sh.get("nav_bounds"):
        task["nav_bounds"] = sh["nav_bounds"]
    task["shard"] = {k: sh[k] for k in ("shard", "shards", "estimated_hours",
                                        "one_pass_min_estimate")}
    out = PIPE / "tasks" / f"lv_{shard_id}.json"
    out.write_text(json.dumps(task, indent=2) + "\n")
    print(f"[gen] {out.name}  {sh['map_id']}  seed {sh['seed']}  "
          f"{sh['target_passes']} passes  cap {sh['max_duration_s']/3600:.2f} h  "
          f"(shard {sh['shard']+1} of {sh['shards']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
