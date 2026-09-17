#!/usr/bin/env python3
"""Re-run the acceptance gates on an episode that is already in S3, without downloading it.

    python3 readjudicate.py s3://bucket/prefix/<episode>/ [...]
    python3 readjudicate.py --all            # every episode under the long-video prefix

Why this exists: the 15 recovery instances were already running `recover.py` when two gates were
found to be wrong for a recovered episode - `frame_count_matches_frozen` compared the delivered
count against the frozen plan, which a recovered episode is shorter than by construction, and
`all_frames_present` counted images the engine wrote past the last engine_states line. Restarting
the fleet to pick up the fix would have risked losing instances to capacity for what is a verdict
problem, not a data problem, so the episodes ship with the old verdict and this corrects it.

It downloads the metadata (a few MB) and the JPEGs the video gates actually sample (stride 12, so
~8% of the frames), not the episode. The corrected acceptance.json is written back beside the
original, which is KEPT as acceptance_asrun.json - the verdict a run produced is part of its
record, and overwriting it would erase the fact that it was ever wrong.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

ROOT = "s3://pan-simworld/ue-revist-long-video"
SAMPLE_FRAMES = 120
META = ["capture_summary.json", "trajectory.json", "camera.json", "sequence.json",
        "frames.csv", "frames.parquet", "revisits.json", "acceptance.json"]


def s3(*args, capture=True):
    r = subprocess.run(["aws", "s3", *args], capture_output=capture, text=True)
    return r


def list_episodes():
    out = s3("ls", f"{ROOT}/", "--recursive").stdout or ""
    eps = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        key = parts[3]
        if key.endswith("/acceptance.json"):
            eps.add(f"s3://pan-simworld/{key[:-len('acceptance.json')]}")
    return sorted(eps)


def fetch(url, work):
    """Metadata, plus only the frames the video gates sample."""
    for name in META:
        s3("cp", f"{url}{name}", str(work / name), "--only-show-errors")
    summary = work / "capture_summary.json"
    if not summary.exists():
        return None
    n = int(json.loads(summary.read_text())["frames"])
    # Only a TOKEN sample of frames is fetched, and none of the frame-reading gates' verdicts are
    # kept - see reconcile(). The gates worth re-adjudicating (frame_count_matches_frozen, and
    # everything derived from frames.csv) need no pixels at all. The sample exists so package.py
    # has images to open instead of dividing by an empty set, and it is small on purpose: a
    # faithful stride-12 sample of a 400k-frame episode is 66k objects, which is both a download
    # and an --include list long enough to overflow the command line.
    (work / "rgb").mkdir(exist_ok=True)
    (work / "depth").mkdir(exist_ok=True)
    step = max(1, n // SAMPLE_FRAMES)
    want = sorted({i for j in range(0, n, step) for i in (j, j + 1) if i < n})[:2 * SAMPLE_FRAMES]
    inc = ["--exclude", "*"]
    for i in want:
        inc += ["--include", f"{i:06d}.jpg"]
    s3("sync", f"{url}rgb/", str(work / "rgb"), *inc, "--only-show-errors")
    got = len(list((work / "rgb").glob("*.jpg")))
    print(f"[readj] {url.rstrip('/').split('/')[-1]}: {n} frames, fetched {got} token JPEGs "
          f"(the frame-reading gates are carried over, not re-measured)", flush=True)
    return n


def reconcile(work, n):
    """Judge what the fetched subset supports, and say what it does not."""
    import package as pk
    acc_old = json.loads((work / "acceptance.json").read_text())
    pk.build(work)
    acc_new = json.loads((work / "acceptance.json").read_text())
    # Gates whose verdict depends on files this tool did not download. Their result is CARRIED
    # OVER from the run, never re-measured: re-running them here would judge the absence of a
    # download as a fault in the data. The first pass of this tool did exactly that and turned a
    # bogus `no_dead_black_regions` failure into a bogus `metric_depth_present` failure - a
    # different wrong answer is not progress.
    ungradeable = {
        # need every delivered frame, and only a token sample was fetched
        "all_frames_present", "frames_decode", "no_blank_frames", "no_duplicate_frames",
        "sha256_manifest_complete",
        # need depth/, which is not fetched at all
        "metric_depth_present", "depth_not_empty", "depth_range_plausible",
        "no_geometry_penetration",
    }
    gates, dropped = [], []
    for g in acc_new["gates"]:
        if g["gate"] in ungradeable:
            dropped.append(g["gate"])
            prev = next((x for x in acc_old["gates"] if x["gate"] == g["gate"]), None)
            if prev:
                prev = dict(prev)
                prev["detail"] = (f"{prev.get('detail', '')} [carried over from the run: this "
                                  f"gate reads every delivered frame and re-adjudication fetched "
                                  f"only the sampled ones, so it was not re-measured]")
                gates.append(prev)
        else:
            gates.append(g)
    acc_new["gates"] = gates
    failed = [g for g in gates if str(g.get("result", "")).upper().startswith("FAIL")]
    skipped = [g for g in gates if str(g.get("result", "")).lower().startswith("skip")]
    acc_new["accepted"] = len(failed) == 0
    acc_new["gates_failed"] = len(failed)
    acc_new["gates_skipped"] = len(skipped)
    acc_new["gates_passed"] = len(gates) - len(failed) - len(skipped)
    acc_new["readjudicated"] = {
        "reason": "the run's verdict used a frame_count gate that failed every recovered episode "
                  "by construction, and an all_frames_present gate that counted frames written "
                  "past the last engine state",
        "not_re_measured": dropped,
        "verdict_as_run": {"accepted": acc_old.get("accepted"),
                           "failed": [g["gate"] for g in acc_old["gates"]
                                      if str(g.get("result", "")).upper().startswith("FAIL")]},
    }
    return acc_old, acc_new


def one(url, apply):
    url = url if url.endswith("/") else url + "/"
    work = Path(tempfile.mkdtemp(prefix="readj_"))
    try:
        n = fetch(url, work)
        if n is None:
            return {"episode": url, "error": "no capture_summary.json at that prefix"}
        acc_old, acc_new = reconcile(work, n)
        name = url.rstrip("/").split("/")[-1]
        res = {"episode": name,
               "was": {"accepted": acc_old.get("accepted"),
                       "failed": acc_new["readjudicated"]["verdict_as_run"]["failed"]},
               "now": {"accepted": acc_new["accepted"],
                       "failed": [g["gate"] for g in acc_new["gates"]
                                  if str(g.get("result", "")).upper().startswith("FAIL")]}}
        if apply:
            s3("cp", f"{url}acceptance.json", f"{url}acceptance_asrun.json", "--only-show-errors")
            p = work / "acceptance_new.json"
            p.write_text(json.dumps(acc_new, indent=1) + "\n")
            s3("cp", str(p), f"{url}acceptance.json", "--only-show-errors")
            res["applied"] = True
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="write the corrected acceptance.json back to S3, keeping the original "
                         "as acceptance_asrun.json. Without this it only reports.")
    a = ap.parse_args()
    urls = a.urls or (list_episodes() if a.all else [])
    if not urls:
        print("nothing to do: pass episode URLs or --all")
        return 2
    out = []
    for u in urls:
        try:
            r = one(u, a.apply)
        except Exception as e:
            import traceback
            traceback.print_exc()
            r = {"episode": u, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        out.append(r)
        print("READJ " + json.dumps(r), flush=True)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
