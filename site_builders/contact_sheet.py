#!/usr/bin/env python3
"""Build contact sheets: one frame per clip, tiled, so the footage can actually be eyed.

Numeric gates catch what they were written to catch. Every defect found so far - third
person framing, vertical shake, sideways jumps - was spotted by looking at the picture
first and only then turned into a measurement. With ~85 maps, opening clips one by one
is not practical, so tile a mid-clip frame from each into a few large images.

    python3 local_run/contact_sheet.py [cols] [tile_w]
"""
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.environ.get("ROOT",
                      "/home/ubuntu/WM-Unreal-data-collection/local_run/fpv_data")
OUT = "/home/ubuntu/WM-Unreal-data-collection/local_run/sheets"
COLS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
TW = int(sys.argv[2]) if len(sys.argv) > 2 else 480
PER_SHEET = COLS * 3


def frame_of(path, frac=0.45):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if n > 1:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * frac))
    ok, fr = cap.read()
    cap.release()
    if not ok:
        return None
    h, w = fr.shape[:2]
    return cv2.resize(fr, (TW, max(1, int(h * TW / w))))


clips = [d for d in sorted(glob.glob(os.path.join(ROOT, "*", "fpv_*")))
         if "_rejected" not in d]
os.makedirs(OUT, exist_ok=True)
tiles = []
for d in clips:
    img = frame_of(os.path.join(d, "video.mp4"))
    if img is None:
        continue
    label = "%s / %s" % (os.path.basename(os.path.dirname(d)).replace("Game_", ""),
                         os.path.basename(d))
    cv2.rectangle(img, (0, 0), (img.shape[1], 26), (0, 0, 0), -1)
    cv2.putText(img, label[:58], (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (255, 255, 255), 1, cv2.LINE_AA)
    tiles.append(img)

if not tiles:
    print("no clips found")
    raise SystemExit(0)

th = max(t.shape[0] for t in tiles)
written = []
for s in range(0, len(tiles), PER_SHEET):
    chunk = tiles[s:s + PER_SHEET]
    rows = []
    for r in range(0, len(chunk), COLS):
        row = chunk[r:r + COLS]
        row = [cv2.copyMakeBorder(t, 0, th - t.shape[0], 0, 0,
                                  cv2.BORDER_CONSTANT, value=(20, 20, 20))
               for t in row]
        while len(row) < COLS:
            row.append(np.full((th, TW, 3), 20, np.uint8))
        rows.append(np.hstack(row))
    sheet = np.vstack(rows)
    path = os.path.join(OUT, "sheet_%02d.jpg" % (s // PER_SHEET + 1))
    cv2.imwrite(path, sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    written.append(path)

print("clips: %d" % len(tiles))
for w in written:
    print("  %s" % w)
