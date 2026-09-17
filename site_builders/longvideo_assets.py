#!/usr/bin/env python3
"""Fetch each delivered episode's OWN contact sheet and sample frames for the /longvideo page.

Keyed by the full episode id. The first version keyed by shard suffix alone - "s00" - and the
assets directory held only ChemicalPlant's s00 and s03, so Downtown_West s00, Pyramids s00,
Tokyo s00 and ForestGasStation s00 were all shown with ChemicalPlant's frames under their names.
A page that shows one map's pictures under another map's title is wrong data presented as right,
which is the one failure this whole repository is organised against, and it happened on the page
built to review the data. Every image now comes from the episode it is shown under, and a manifest
lists exactly which exist so the page never renders a placeholder as if it were a frame.
"""
import json, subprocess, sys
from pathlib import Path
import cv2

HERE = Path(__file__).resolve().parent
OUT = HERE / "site" / "longvideo" / "assets"
SNAP = HERE / "site" / "longvideo" / "status.json"
FRAMES = ("000000", "020000", "060000")

def s3cp(src, dst):
    r = subprocess.run(["aws", "s3", "cp", src, str(dst), "--only-show-errors"],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and dst.exists()

def shrink(path, width, q=78):
    a = cv2.imread(str(path))
    if a is None:
        return False
    h, w = a.shape[:2]
    if w > width:
        a = cv2.resize(a, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    out = path.with_suffix(".jpg")
    cv2.imwrite(str(out), a, [cv2.IMWRITE_JPEG_QUALITY, q])
    if out != path:
        path.unlink(missing_ok=True)
    return True

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    eps = json.loads(SNAP.read_text())["episodes"]
    manifest = {}
    for e in eps:
        key = e["episode_id"]
        pre = e["prefix"]
        have = {"contact": False, "frames": []}
        c = OUT / f"{key}.contact.png"
        if c.with_suffix(".jpg").exists() or (s3cp(f"{pre}contact.png", c) and shrink(c, 1500)):
            have["contact"] = True
        for f in FRAMES:
            p = OUT / f"{key}.{f}.jpg"
            if p.exists() or (s3cp(f"{pre}rgb/{f}.jpg", p) and shrink(p, 880, 76)):
                have["frames"].append(f)
        manifest[key] = have
        print(f"  {key[-40:]:42s} contact={'✓' if have['contact'] else '✗'} "
              f"frames={len(have['frames'])}", flush=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"清单: {len(manifest)} 集")

if __name__ == "__main__":
    sys.exit(main())
