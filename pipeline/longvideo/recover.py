#!/usr/bin/env python3
"""Turn the frames a killed capture left on disk into a packaged episode. No engine, no recording.

    python3 recover.py                      # every recoverable episode under episodes/
    python3 recover.py <episode_dir> ...    # just these

The engine writes `rgb/%06d.jpg`, `depth/%06d.exr` and `engine_states.jsonl` as it captures, and
`capture_engine.capture()` writes everything that makes those frames an EPISODE - the per-frame
table, capture_summary, trajectory - only after the last frame. A capture killed in between
therefore leaves hours of real frames with none of the metadata, and nothing downstream will look
at them: `uploader.sh` waits for `acceptance.json`, so the data sits on the instance disk and the
instance stops.

That is what happened to most of a 31-shard fleet run. This reads what is on disk, decides how
many frames are actually there, and runs the same finalisation and packaging the capture would
have run - so the episode that comes out is built by the same code, not a reconstruction of it.

What it cannot recover is the capture's own measurements: engine fps, the render/readback/write
timings, and whether the skylight was made dynamic. Those are recorded as null with a reason,
never invented.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import capture_engine as cape  # noqa: E402
import package as pk  # noqa: E402

EPISODES = PIPE / "episodes"
FROZEN = PIPE / "frozen"


def contiguous_frames(ep):
    """How many frames from 0 are present in rgb/, depth/ AND engine_states.jsonl.

    The lowest of the three, and contiguous from zero: a gap is not a shorter episode, it is a
    hole, and `frame_gap` is one of the acceptance gates for exactly that reason.

    State lines are counted only if they can actually become a frames.csv row, using
    capture_engine's own `parse_state` - the same code finalise will run. Counting non-blank
    lines instead let a torn tail into the total, and finalise then wrote a row with blank
    columns which failed hundreds of lines later inside an acceptance gate. Two places deciding
    "how many frames are there" by different rules is the bug; there is now one rule.
    """
    rgb = {int(p.stem) for p in (ep / "rgb").glob("*.jpg") if p.stem.isdigit()}
    dep = {int(p.stem) for p in (ep / "depth").glob("*.exr") if p.stem.isdigit()}
    js = ep / "engine_states.jsonl"
    lines = torn = 0
    if js.exists():
        with open(js) as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                if cape.parse_state(ln) is None:
                    torn += 1
                    break        # the tail is torn; nothing past it is usable either
                lines += 1
    n = 0
    while n in rgb and n in dep and n < lines:
        n += 1
    return n, len(rgb), len(dep), lines, torn


def set_aside_orphans(ep, n):
    """Move frames past the last engine_states line into orphan_frames/, and say how many.

    The engine writes rgb/%06d.jpg, depth/%06d.exr and the state line for a frame at slightly
    different moments, so a SIGKILL can leave images with no state line behind them. Those pixels
    are real but they have no pose, no timestamp and no action - they cannot be part of an episode,
    and `frames.csv` is built from the state lines, so leaving them in rgb/ makes the delivered
    image count disagree with the frame table for the whole episode.

    They are MOVED, not deleted. They are recorded data and it is not this script's place to throw
    them away; a note in the episode says where they went and why.
    """
    moved = {}
    for sub, ext in (("rgb", "jpg"), ("depth", "exr")):
        d = ep / sub
        if not d.is_dir():
            continue
        orphans = sorted(p for p in d.glob(f"*.{ext}")
                         if p.stem.isdigit() and int(p.stem) >= n)
        if not orphans:
            continue
        dst = ep / "orphan_frames" / sub
        dst.mkdir(parents=True, exist_ok=True)
        for p in orphans:
            p.rename(dst / p.name)
        moved[sub] = len(orphans)
    if moved:
        (ep / "orphan_frames" / "README.txt").write_text(
            f"{moved} frames were written by the engine past frame {n - 1}, which is the last "
            f"frame with a line in engine_states.jsonl. They have no pose, timestamp or action, "
            f"so they are not part of the episode and are not counted in frames.csv. They are "
            f"kept here rather than deleted because they are recorded data.\n")
    return moved


def frozen_for(ep):
    """The frozen plan this episode was captured from - its name is the episode id."""
    p = FROZEN / f"{ep.name}.json"
    if p.exists():
        return p
    hits = sorted(FROZEN.glob(f"{ep.name}*.json"))
    return hits[0] if hits else None


def recover(ep):
    ep = Path(ep)
    if (ep / "acceptance.json").exists():
        return {"episode": ep.name, "skipped": "already packaged"}
    fzp = frozen_for(ep)
    if fzp is None:
        return {"episode": ep.name, "error": "no frozen plan on disk; it is the only place the "
                                             "poses, camera and task live"}
    n, nrgb, ndep, njs, torn = contiguous_frames(ep)
    fz = json.loads(fzp.read_text())
    task = fz["task"]
    fps = float(fz["fps"])
    planned = int(fz.get("frames") or 0)
    floor = int(task.get("min_frames") or 0)
    info = {"episode": ep.name, "contiguous_frames": n, "rgb": nrgb, "depth": ndep,
            "engine_states_lines": njs, "planned_frames": planned,
            "hours": round(n / fps / 3600, 3)}
    if torn:
        info["torn_state_tail"] = ("the last engine_states.jsonl line could not become a row, "
                                   "so the episode ends before it")
    if n < max(floor, 1):
        info["error"] = (f"only {n} contiguous frames (rgb {nrgb}, depth {ndep}, states {njs}); "
                         f"below the task's min_frames of {floor}")
        return info
    print(f"[recover] {ep.name}: {n} contiguous frames ({n/fps/3600:.2f} h) of a planned "
          f"{planned}; finalising", flush=True)
    orphans = set_aside_orphans(ep, n)
    if orphans:
        info["orphan_frames"] = orphans
        print(f"[recover] set aside {orphans} frames past the last engine state", flush=True)
    # SKIP_GATES: package for delivery only - no acceptance gates, no review video. Set for a
    # run whose purpose is to get recorded frames into S3, where the verdict can be computed
    # later from the upload by longvideo/readjudicate.py. The gates cost about an hour per
    # 400k-frame episode; on this job they also wrongly rejected two sound episodes and crashed
    # on thirteen, all inside a check, with the frames themselves never in question.
    gates = os.environ.get("SKIP_GATES", "").strip().lower() not in ("1", "true", "yes")
    cape.finalise(ep, fz, task, n, planned_total=planned,
                  short_reason="capture was interrupted; recovered from the frames on disk by "
                               "longvideo/recover.py, which cannot know why it stopped",
                  engine_s=None, st=None, lighting=None, review_video=gates)
    pk.build(ep, gates=gates)
    info["gates_run"] = gates
    # If the table turned out to be malformed and DELIVER_PARTIAL took its well-formed prefix,
    # the episode is now shorter than the frames on disk. Set the surplus aside: shipping images
    # that frames.csv does not describe puts frames with no pose, no timestamp and no action into
    # the delivered set, which is precisely what this dataset exists to avoid.
    trunc = ep / "_table_truncated.json"
    if trunc.exists():
        t = json.loads(trunc.read_text())
        kept = int(t["kept_rows"])
        info["table_truncated"] = t
        extra = set_aside_orphans(ep, kept)
        if extra:
            info["orphan_frames_after_truncation"] = extra
            print(f"[recover] set aside {extra} frames past the truncated table", flush=True)
        # capture_summary must not keep claiming the longer count.
        cs = ep / "capture_summary.json"
        d = json.loads(cs.read_text())
        d["frames_in_table"] = kept
        d["frames_written_by_engine"] = d.get("frames")
        d["frames"] = kept
        d["duration_s"] = kept / float(d["fps"])
        d["table_truncated"] = t["why"]
        cs.write_text(json.dumps(d, indent=1) + "\n")
        info["hours"] = round(kept / fps / 3600, 3)
        info["contiguous_frames"] = kept
    acc = json.loads((ep / "acceptance.json").read_text())
    info["accepted"] = acc.get("accepted")     # None means UNJUDGED, not failed
    info["failed_gates"] = [g["gate"] for g in acc["gates"]
                            if str(g.get("result", "")).upper().startswith("FAIL")]
    info["packaged"] = True
    return info


def main(argv):
    # Default to THIS SHARD's episodes only. With no filter it walked all 106 episode
    # directories one instance's disk happened to carry - the AMI ships previous campaigns - and
    # re-packaged two unrelated 1,441-frame ones. Nothing was uploaded only because uploader.sh
    # filters by the same shard tag; the run was still an hour of pointless work per instance,
    # and every unrelated episode is one more chance to fail on a file this job never wrote.
    shard = os.environ.get("SHARD_ID", "").strip()
    if argv:
        targets = [Path(a) for a in argv]
    else:
        cand = sorted(d for d in EPISODES.glob("*") if d.is_dir() and (d / "rgb").is_dir())
        if shard:
            targets = [d for d in cand if f"lv_{shard}" in d.name]
            print(f"[recover] {len(targets)} of {len(cand)} episodes on disk belong to shard "
                  f"{shard}", flush=True)
        else:
            targets = cand
            print(f"[recover] SHARD_ID is not set, so all {len(cand)} episodes on disk are "
                  f"candidates - including any this job did not record", flush=True)
    out = []
    if not targets:
        # Still write the beacon. "Nothing to recover" and "the recovery never ran" are different
        # facts and they were indistinguishable from outside: a fleet of 15 instances sat idle for
        # 70 minutes because cloud-init had skipped their user-data, and the absence of a beacon
        # looked exactly like the absence of data.
        out = [{"episode": None, "error": f"nothing under {EPISODES} with an rgb/ directory"}]
        print(f"[recover] nothing under {EPISODES} with an rgb/ directory", flush=True)
        print("RECOVER " + json.dumps(out[0]), flush=True)
        (PIPE / "_recover.json").write_text(json.dumps(out, indent=1) + "\n")
        return 0
    for ep in targets:
        try:
            r = recover(ep)
        except Exception as e:
            import traceback
            traceback.print_exc()
            r = {"episode": Path(ep).name, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        out.append(r)
        print("RECOVER " + json.dumps(r), flush=True)
    (PIPE / "_recover.json").write_text(json.dumps(out, indent=1) + "\n")
    ok = sum(1 for r in out if r.get("packaged"))
    print(f"[recover] {ok} of {len(out)} episodes packaged")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
