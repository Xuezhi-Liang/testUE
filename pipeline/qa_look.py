#!/usr/bin/env python3
"""Is the episode actually LOOKING at the world, or is the camera buried in it?

The complaint that some maps "do not look like what the eye sees" needs a measurement, not an
opinion. Brightness alone does not separate the two failure modes we have seen:

  - camera inside geometry (a hedge, a wall, soil). Nearly every pixel is a surface a few
    centimetres away, so the frame is dark AND its depth is tiny. This is a route defect.
  - genuinely dark scene (night, shadow, interior). Dark pixels, but depth is normal. This
    would be a rendering/exposure defect, and is a different fix.

Depth is what tells them apart, and we already write it per frame. `buried` counts frames where
over half the image is closer than BURIED_M - no real standing viewpoint looks like that.
"""
import json
import os
import sys
from pathlib import Path

# Must be set before cv2 is imported: this build ships the OpenEXR codec disabled, and without the
# flag every depth read raises rather than returning None - which would look like corrupt files.
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2  # noqa: E402
import numpy as np

BURIED_M = 0.6          # under a metre from the eye across most of the frame: not a viewpoint
DARK = 16               # 8-bit level below which a pixel carries no visible content
STRIDE = 12             # every half second at 24 fps; enough to characterise 2 minutes


def measure(ep: Path) -> dict:
    rgbs = sorted((ep / "rgb").glob("*.jpg"))
    exrs = sorted((ep / "depth").glob("*.exr"))
    if not rgbs:
        return {"episode": ep.name, "error": "no rgb frames"}
    dark_frac, means, buried, near_frac, depth_read = [], [], 0, [], 0
    for i in range(0, len(rgbs), STRIDE):
        g = cv2.imread(str(rgbs[i]), cv2.IMREAD_GRAYSCALE)
        if g is None:
            continue
        means.append(float(g.mean()))
        dark_frac.append(float((g < DARK).mean()))
        if i < len(exrs):
            d = cv2.imread(str(exrs[i]), cv2.IMREAD_UNCHANGED)
            if d is not None:
                depth_read += 1
                # PITFALL - OpenCV decodes EXR as BGRA, so our R channel is index 2, not 0.
                # Index 0 is B, which we never write: it reads as all zeros, `z > 0` selects
                # nothing, and this function then reported "0% near pixels" for every episode.
                # A wrong channel does not error, it just answers a question nobody asked.
                z = d[..., 2] if d.ndim == 3 else d
                valid = z > 0                      # -1 is sky/out of range, not a measurement
                f = float((z[valid] < BURIED_M).mean()) if valid.any() else 0.0
                near_frac.append(f)
                if f > 0.5:
                    buried += 1
    n = len(means)
    out = {
        "episode": ep.name,
        "frames_sampled": n,
        "mean_brightness": round(float(np.mean(means)), 1),
        "dark_pixel_fraction": round(float(np.mean(dark_frac)), 3),
        "worst_frame_dark_fraction": round(float(np.max(dark_frac)), 3),
    }
    if depth_read:
        out.update({
            "depth_frames_sampled": depth_read,
            "median_near_pixel_fraction": round(float(np.median(near_frac)), 3),
            "buried_frames": buried,
            "buried_fraction": round(buried / depth_read, 3),
        })
    else:
        out["depth_frames_sampled"] = 0
    return out


if __name__ == "__main__":
    roots = [Path(a) for a in sys.argv[1:]] or sorted(Path("episodes").iterdir())
    rows = [measure(r) for r in roots if r.is_dir()]
    for r in rows:
        print(json.dumps(r))
    print()
    print(f"{'episode':58s} {'bright':>7s} {'dark%':>7s} {'near%':>7s} {'buried%':>8s}")
    for r in rows:
        if "error" in r:
            print(f"{r['episode'][:58]:58s}  {r['error']}")
            continue
        print(f"{r['episode'][:58]:58s} {r['mean_brightness']:7.1f} "
              f"{100*r['dark_pixel_fraction']:7.1f} "
              f"{100*r.get('median_near_pixel_fraction', float('nan')):7.1f} "
              f"{100*r.get('buried_fraction', float('nan')):8.1f}")
