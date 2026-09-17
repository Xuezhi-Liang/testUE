#!/usr/bin/env python3
"""Contact sheet of the core survey: one tile per map, ranked by core area.

Green is the core, mid grey the walkable surface that is NOT core - the apron a coverage walk
should not be spending the episode on. Orange is what makes it a core: the obstacle footprints.
"""
import json, sys
from pathlib import Path
import numpy as np, cv2

PIPE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
d = Path(sys.argv[1]) if len(sys.argv) > 1 else PIPE / "_core_survey"
rows = [r for r in json.load(open(d / "core_survey.json")) if "error" not in r]
rows.sort(key=lambda r: -r["core_m2"])
TILE, PAD, COLS = 300, 8, 5
LBL = 40
rowsn = (len(rows) + COLS - 1) // COLS
sheet = np.full((rowsn * (TILE + LBL + PAD) + PAD, COLS * (TILE + PAD) + PAD, 3), 18, np.uint8)
for i, r in enumerate(rows):
    img = cv2.imread(str(d / "png" / f"{r['map']}.png"))
    h, w = img.shape[:2]
    s = min(TILE / w, TILE / h)
    img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    gy, gx = divmod(i, COLS)
    y0 = PAD + gy * (TILE + LBL + PAD)
    x0 = PAD + gx * (TILE + PAD)
    sheet[y0:y0 + img.shape[0], x0:x0 + img.shape[1]] = img
    name = r["map"].removeprefix("Game_")
    name = (name[:34] + "..") if len(name) > 36 else name
    cv2.putText(sheet, f"{i+1}. {name}", (x0, y0 + TILE + 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(sheet, f"core {r['core_m2']:.0f} m2  ({r['core_frac_of_region_safe']*100:.0f}% of the "
                       f"walkable surface)", (x0, y0 + TILE + 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.40, (140, 220, 140), 1, cv2.LINE_AA)
out = d / "core_sheet.png"
cv2.imwrite(str(out), sheet)
print("wrote", out, sheet.shape)
