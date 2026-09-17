#!/usr/bin/env python3
"""Collect the long-video job's live state into site/longvideo/status.json.

Written for the sub-page at /longvideo, which is a status board rather than a clip gallery: the
work it reports on runs on 15 other machines and lands in S3, so there is no clip tree to scan.

Cost discipline, because this runs on a loop:
  - EC2 describe-instances and `s3 ls` of the small _status/ prefix are cheap; every tick.
  - Per-episode metadata is a handful of small JSON objects; every tick.
  - The bucket's object count and total size need a recursive listing of ~600k keys, which takes
    minutes. That is refreshed at most every SIZE_EVERY_S and otherwise carried forward from the
    previous snapshot, with the timestamp of when it was actually measured - a stale number
    labelled stale beats a fresh page that takes four minutes to load.
"""
import json
import os
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "site" / "longvideo"
SNAP = OUT / "status.json"
REGION = "eu-north-1"
BUCKET = "s3://pan-simworld/ue-revist-long-video"
SIZE_EVERY_S = 900


def sh(*args, timeout=420):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def instances():
    out = sh("aws", "ec2", "describe-instances", "--region", REGION,
             "--filters", "Name=tag-key,Values=LongVideoShard",
             "--query", "Reservations[].Instances[].[Tags[?Key=='LongVideoShard']|[0].Value,"
                        "State.Name,Tags[?Key=='LongVideoRecover']|[0].Value,InstanceType]",
             "--output", "text", timeout=90)
    rows = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) >= 4 and f[0] not in ("", "None"):
            rows.append({"shard": f[0], "state": f[1],
                         "recover": f[2] in ("1", "true", "yes"), "type": f[3]})
    return sorted(rows, key=lambda r: r["shard"])


def episode_prefixes():
    """Episode prefixes, found by walking the delimiter - NOT by a recursive listing.

    `aws s3 ls --recursive` over this bucket means enumerating every delivered frame: it passed
    6 million keys once the long episodes landed and the collector started timing out at 20
    minutes. Listing map prefixes, then episode prefixes under each (and under _rejected/), is
    three dozen cheap calls instead of one enormous one.
    """
    maps = [ln.split()[-1] for ln in sh("aws", "s3", "ls", f"{BUCKET}/", timeout=60).splitlines()
            if ln.strip().startswith("PRE") and not ln.strip().endswith(("_ops/", "_status/"))]
    out = []
    for m in maps:
        for lvl in (f"{BUCKET}/{m}", f"{BUCKET}/{m}_rejected/"):
            for ln in sh("aws", "s3", "ls", f"{lvl}", timeout=60).splitlines():
                ln = ln.strip()
                if not ln.startswith("PRE"):
                    continue
                name = ln.split()[-1]
                if name == "_rejected/":
                    continue
                out.append(f"{lvl}{name}")
    return sorted(set(out))


def episodes():
    """Every packaged episode in the bucket, with the numbers that matter and its verdict."""
    eps = []
    for pre in episode_prefixes():
        sm = sh("aws", "s3", "cp", f"{pre}capture_summary.json", "-", timeout=60)
        ac = sh("aws", "s3", "cp", f"{pre}acceptance.json", "-", timeout=60)
        if not sm:
            continue
        try:
            sm = json.loads(sm)
            ac = json.loads(ac) if ac else {}
        except Exception:
            continue
        fails = [g["gate"] for g in ac.get("gates", [])
                 if str(g.get("result", "")).upper().startswith("FAIL")]
        eps.append({
            "prefix": pre, "rejected": "/_rejected/" in pre,
            "episode_id": sm.get("episode_id", ""),
            "map_id": sm.get("map_id", ""), "seed": sm.get("seed"),
            "frames": sm.get("frames"), "hours": round((sm.get("duration_s") or 0) / 3600, 3),
            "fps": sm.get("fps"), "engine_fps": sm.get("engine_fps"),
            "record_hours": round((sm.get("record_seconds") or 0) / 3600, 2)
                            if sm.get("record_seconds") else None,
            "accepted": ac.get("accepted"),
            "passed": ac.get("gates_passed"), "failed": ac.get("gates_failed"),
            "skipped": ac.get("gates_skipped"), "failed_gates": fails,
            "measurements_unavailable": bool(sm.get("measurements_unavailable")),
        })
    return sorted(eps, key=lambda e: e["episode_id"])


def beacons():
    out = sh("aws", "s3", "ls", f"{BUCKET}/_status/", timeout=90)
    names = [ln.split()[-1] for ln in out.splitlines() if ln.endswith("_recover.json")]
    res = []
    for nm in names:
        body = sh("aws", "s3", "cp", f"{BUCKET}/_status/{nm}", "-", timeout=60)
        try:
            d = json.loads(body)
        except Exception:
            continue
        lv = [r for r in (d if isinstance(d, list) else [d])
              if "lv_" in (r.get("episode") or "")]
        for r in lv:
            res.append({"shard": nm[: -len("_recover.json")],
                        "frames": r.get("contiguous_frames"), "hours": r.get("hours"),
                        "accepted": r.get("accepted"), "skipped": r.get("skipped"),
                        "error": r.get("error"), "failed_gates": r.get("failed_gates"),
                        "orphans": r.get("orphan_frames")})
        if not lv:
            res.append({"shard": nm[: -len("_recover.json")],
                        "error": "no episode for this shard on its disk"})
    return sorted(res, key=lambda r: r["shard"])


def bucket_size_cloudwatch():
    """Bucket size from CloudWatch's own S3 storage metrics.

    Splitting the recursive listing per episode did not make it cheaper - it still enumerates
    every key, just in sixteen calls instead of one - so this asks the service that already
    counted. `AWS/S3 BucketSizeBytes` and `NumberOfObjects` are free, instant and authoritative,
    at daily granularity. Daily is the right trade for a figure nobody needs to the minute; the
    per-episode frame counts, which come from capture_summary.json, are what moves during a run.
    """
    def metric(name, stype):
        out = sh("aws", "cloudwatch", "get-metric-statistics", "--region", "eu-north-1",
                 "--namespace", "AWS/S3", "--metric-name", name,
                 "--dimensions", "Name=BucketName,Value=pan-simworld",
                 f"Name=StorageType,Value={stype}",
                 "--start-time", time.strftime("%Y-%m-%dT%H:%M:%S",
                                               time.gmtime(time.time() - 4 * 86400)),
                 "--end-time", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                 "--period", "86400", "--statistics", "Average",
                 "--query", "sort_by(Datapoints,&Timestamp)[-1].[Average,Timestamp]",
                 "--output", "text", timeout=60)
        f = out.split()
        if len(f) >= 2:
            try:
                return float(f[0]), f[1]
            except ValueError:
                pass
        return None, None
    b, bt = metric("BucketSizeBytes", "StandardStorage")
    o, _ = metric("NumberOfObjects", "AllStorageTypes")
    if b is None:
        return {}
    return {"bytes": int(b), "objects": int(o) if o is not None else None,
            "measured_at": bt, "scope": "the whole pan-simworld bucket, not just this prefix",
            "method": "CloudWatch AWS/S3 daily storage metrics - free and instant, but a day "
                      "behind; during a run the per-episode frame counts are the live figure"}


def bucket_size(prev, eps):
    """Objects and bytes, summed PER EPISODE rather than by one recursive listing.

    `--recursive --summarize` over the whole bucket walks every delivered frame - past 6 million
    keys once the long episodes landed, which is minutes of wall clock and the reason the
    collector was killed at its 20-minute timeout. Per-episode summaries are the same arithmetic
    from listings small enough to finish, and they give the size breakdown for free.

    Still rate-limited to SIZE_EVERY_S: it is the expensive half of this collector, and a size
    that is fifteen minutes old, labelled as such, is worth more than a page that will not load.
    """
    age = time.time() - (prev.get("bucket", {}).get("measured_at_epoch") or 0)
    if prev.get("bucket", {}).get("objects") and age < SIZE_EVERY_S:
        return prev["bucket"]
    objs = size = 0
    per = {}
    for e in eps:
        out = sh("aws", "s3", "ls", e["prefix"], "--recursive", "--summarize", timeout=600)
        o = b = None
        for ln in out.splitlines():
            if "Total Objects:" in ln:
                o = int(ln.split(":")[1].strip())
            if "Total Size:" in ln:
                b = int(ln.split(":")[1].strip())
        if o is None:
            continue
        objs += o; size += b
        per[e["episode_id"]] = {"objects": o, "bytes": b}
    if not per:
        return prev.get("bucket", {})
    return {"objects": objs, "bytes": size, "per_episode": per,
            "episodes_measured": len(per),
            "measured_at_epoch": time.time(),
            "measured_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "method": "summed per episode prefix; the bucket-wide recursive listing is 6M+ keys"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    # One at a time. The expensive half can take longer than the refresh interval, and without a
    # lock the loop stacks copies of itself, each re-listing the same keys.
    lock = OUT / ".collector.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3600:
        print(f"[longvideo_status] another collector started "
              f"{int(time.time() - lock.stat().st_mtime)}s ago; skipping this tick")
        return 0
    lock.write_text(str(os.getpid()))
    prev = {}
    if SNAP.exists():
        try:
            prev = json.loads(SNAP.read_text())
        except Exception:
            prev = {}
    snap = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "generated_at_epoch": time.time(),
        "instances": instances(),
        "beacons": beacons(),
    }
    snap["episodes"] = episodes()
    snap.update({
        # CloudWatch by default. The exact per-episode listing enumerates every delivered frame -
        # 6 M keys and counting - which took longer than this collector's own timeout twice.
        # EXACT_SIZE=1 asks for it anyway, for a final tally where the wait is acceptable.
        "bucket": (bucket_size(prev, snap["episodes"])
                   if os.environ.get("EXACT_SIZE", "").strip() in ("1", "true", "yes")
                   else prev.get("bucket", {})),
        "bucket_cloudwatch": bucket_size_cloudwatch(),
        "packaging_profile": {
            "sample_frames": 14400,
            "note": "cProfile of package.acceptance on one 14,400-frame episode",
            "phases": [
                {"name": "qa_penetration.scan", "seconds": 211.9, "at_150k_min": 36.8,
                 "detail": "reads every depth frame at stride 1 - the only check that can see "
                           "geometry with collision disabled"},
                {"name": "video gates", "seconds": 45.3, "at_150k_min": 7.9,
                 "detail": "sampled JPEG pairs at stride 12, full resolution"},
                {"name": "files.sha256", "seconds": 39.1, "at_150k_min": 6.8,
                 "detail": "every delivered byte"},
                {"name": "frames table / contact sheet / revisits", "seconds": 0.2,
                 "at_150k_min": 0.05, "detail": ""},
            ],
        },
    })
    tmp = SNAP.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(SNAP)          # atomic, so the page never reads a half-written snapshot
    lock.unlink(missing_ok=True)
    print(f"[longvideo_status] {snap['generated_at']}  "
          f"{len(snap['instances'])} instances, {len(snap['episodes'])} episodes, "
          f"{len(snap['beacons'])} beacons")


if __name__ == "__main__":
    main()
