#!/usr/bin/env python3
"""Compose the QA / preview video: the reference dashboard layout, filled from the package.

1280x720 render at 1:1 inside a 1920x1088 canvas, matching the sample: header, first-person
view, top-down route map, commanded action versus measured state, clearance, collision state and
a time axis with event ribbons.

DEPTH is now real: SimWorldCapture writes a per-frame EXR (linear metres in R, -1 for sky) and
this reads it back. CLEARANCE still comes from the frozen route's per-frame forward ray rather
than from a depth sample - the sample video states its own method under its own number, and so
does this.
"""
import argparse
import csv
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent

CW, CH = 1920, 1088
BG = (250, 249, 247)
INK = (26, 28, 32)
MUT = (122, 128, 138)
LINE = (214, 212, 208)
BLUE = (36, 106, 214)
PALE = (176, 205, 240)
ORANGE = (232, 106, 38)
RED = (206, 48, 48)
GREY = (170, 170, 168)
FD = "/usr/share/fonts/truetype/dejavu"


def fonts():
    def f(n, s):
        return ImageFont.truetype(f"{FD}/{n}", s)
    return {"title": f("DejaVuSansMono-Bold.ttf", 26), "meta": f("DejaVuSansMono.ttf", 15),
            "h": f("DejaVuSansMono.ttf", 14), "n": f("DejaVuSansMono.ttf", 15),
            "sm": f("DejaVuSansMono.ttf", 12), "big": f("DejaVuSansMono.ttf", 54)}


def spaced(d, xy, s, font, fill, tracking=1.2):
    x, y = xy
    for ch in s:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking
    return x


def card(d, box, label=None, sub=None, f=None):
    d.rounded_rectangle(box, 4, outline=LINE, width=1)
    if label:
        e = spaced(d, (box[0] + 14, box[1] + 12), label, f["h"], MUT, 1.6)
        if sub:
            d.text((e + 14, box[1] + 12), sub, font=f["h"], fill=MUT)


def bipolar(d, x, y, w, v, lim, colour=BLUE, h=13, na=False):
    d.rectangle([x, y, x + w, y + h], fill=(228, 228, 226))
    cx = x + w / 2
    if na:
        d.line([cx, y - 1, cx, y + h + 1], fill=GREY, width=1)
        return
    px = max(-1.0, min(1.0, v / (lim or 1.0))) * (w / 2)
    d.rectangle([min(cx, cx + px), y, max(cx, cx + px), y + h], fill=colour)
    d.line([cx, y - 2, cx, y + h + 2], fill=(120, 120, 118), width=1)


def meter(d, x, y, w, frac, colour=ORANGE, h=13):
    d.rectangle([x, y, x + w, y + h], fill=(228, 228, 226))
    frac = max(0.0, min(1.0, frac))
    if frac > 0:
        d.rectangle([x, y, x + w * frac, y + h], fill=colour)


def ffmpeg_bin():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def topdown(size, rows, traj, f):
    """Static layer: the frozen route in pale blue, plus a true-length scale bar."""
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    xs = [float(r["desired_x_cm"]) for r in rows] + [float(r["actual_x_cm"]) for r in rows]
    ys = [float(r["desired_y_cm"]) for r in rows] + [float(r["actual_y_cm"]) for r in rows]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    span = max(max(xs) - min(xs), max(ys) - min(ys), 400.0) * 1.35

    def px(x, y):
        return (size / 2 + (y - cy) / span * size, size / 2 - (x - cx) / span * size)

    pts = [px(float(r["desired_x_cm"]), float(r["desired_y_cm"])) for r in rows]
    if len(pts) > 1:
        d.line(pts, fill=PALE, width=4, joint="curve")
    # pick a round bar length that actually fits the panel; only doubling upward leaves the bar
    # wider than the panel on a small route and clips its own label
    px_per_m = size / (span / 100.0)
    bar_m = 1
    for cand in (1, 2, 5, 10, 20, 50, 100):
        if cand * px_per_m <= size * 0.55:
            bar_m = cand
        else:
            break
    bl = bar_m * px_per_m
    d.line([18, size - 20, 18 + bl, size - 20], fill=INK, width=2)
    d.text((24 + bl, size - 29), f"{bar_m} m", font=f["sm"], fill=MUT)
    return img, px


def render(ep, out="preview.mp4"):
    import os
    os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
    import cv2
    ep = Path(ep)
    f = fonts()
    # PITFALL - an opencv build reporting `OpenEXR: NO` returns None for a perfectly good EXR,
    # which looks exactly like corrupt data. Say which it is rather than drawing an empty panel.
    exr_ok = "OpenEXR:                     NO" not in cv2.getBuildInformation()
    rows = list(csv.DictReader(open(ep / "frames.csv")))
    traj = json.loads((ep / "trajectory.json").read_text())
    su = json.loads((ep / "capture_summary.json").read_text())
    rv = json.loads((ep / "revisits.json").read_text()) \
        if (ep / "revisits.json").exists() else {}
    fps = float(su["fps"])
    N = len(rows)
    clear = traj.get("collision", {}).get("clearance_per_frame_cm")

    # Source frames come from the delivered rgb/*.jpg, by index, not from rgb.mp4. Two reasons:
    # rgb.mp4 is a review artifact that a long episode does not have at full rate, and reading
    # the JPEGs makes striding a matter of arithmetic instead of seeking.
    #
    # STRIDE. This composites a panel per frame - PIL drawing, a depth EXR read, a top-down
    # redraw - so a 20 h episode is 1.73 M composited frames and the QA video never finishes.
    # Above the target length it becomes a timelapse, and the frame counter in the header stays
    # the true frame id so a reviewer is never looking at frame 40000 believing it is frame 12.
    QA_TARGET_FRAMES = int(os.environ.get("QA_VIDEO_TARGET_FRAMES", "3600"))
    stride = int(os.environ.get("QA_VIDEO_STRIDE", "0")) or max(
        1, -(-N // QA_TARGET_FRAMES))
    frame_ids = list(range(0, N, stride))
    rgb_dir = ep / "rgb"
    if not rgb_dir.is_dir():
        print(f"  no {rgb_dir}")
        return None
    if stride > 1:
        print(f"  {N} frames; rendering every {stride}th ({len(frame_ids)} panels) - "
              f"compositing all of them would outlast the capture", flush=True)

    TD = 566
    td_base, td_px = topdown(TD, rows, traj, f)
    walked = []
    aw = traj["anchor_window"]
    rw = traj["revisit_window"]
    yaw_lim = float(traj.get("yaw_deg_per_s", 60.0))

    proc = subprocess.Popen(
        [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-s", f"{CW}x{CH}", "-r", f"{fps}", "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-an", str(ep / out)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    for i in frame_ids:
        bgr = cv2.imread(str(rgb_dir / f"{i:06d}.jpg"))
        if bgr is None:
            continue
        r = rows[min(i, N - 1)]
        cv = Image.new("RGB", (CW, CH), BG)
        d = ImageDraw.Draw(cv)

        # header
        mx = spaced(d, (28, 18), su["trajectory_family"], f["title"], INK, 1.4) + 30
        spaced(d, (mx, 26), f"map {su['map_id'].split('/')[-1]}  |  camera first-person  |  "
                            f"fov {traj['intrinsics']['hfov_deg']:.0f} deg  |  "
                            f"{su['speed_tier']}/{su['yaw_tier']} tier  |  seed {su['seed']}",
               f["meta"], MUT, 0.8)
        t1 = (f"frame {i:06d} / {N-1:06d}" if stride == 1 else
              f"frame {i:06d} / {N-1:06d}  (every {stride}th)")
        d.text((CW - 28 - d.textlength(t1, font=f["meta"]), 16), t1, font=f["meta"], fill=INK)
        t2 = f"sim t = {float(r['episode_time_s']):8.3f} s"
        d.text((CW - 28 - d.textlength(t2, font=f["meta"]), 38), t2, font=f["meta"], fill=MUT)
        d.line([0, 62, CW, 62], fill=LINE, width=1)

        # first-person view at 1:1
        VX, VY = 24, 72
        cv.paste(Image.fromarray(bgr[:, :, ::-1]), (VX, VY))
        d.rectangle([VX, VY, VX + 1280, VY + 720], outline=LINE, width=1)
        DW, DH = 380, 214
        dx0, dy0 = VX + 1280 - DW - 16, VY + 720 - DH - 16
        d.rectangle([dx0, dy0, dx0 + DW, dy0 + DH], fill=(28, 30, 34))
        spaced(d, (dx0 + 12, dy0 + 8), "DEPTH (m)", f["h"], (152, 158, 168), 1.6)
        dpath = ep / (r.get("depth_path") or "")
        dimg = (cv2.imread(str(dpath), cv2.IMREAD_UNCHANGED)
                if (exr_ok and r.get("depth_path") and dpath.exists()) else None)
        if dimg is None:
            d.text((dx0 + 12, dy0 + 34),
                   "no EXR for this frame" if exr_ok else "this opencv has OpenEXR: NO",
                   font=f["sm"], fill=(152, 158, 168))
        else:
            # OpenCV decodes EXR as BGRA, so depth written to R comes back at index 2. Index 0 is
            # an all-zero plane that looks exactly like a failed capture.
            dd = dimg[:, :, 2].astype(np.float32) if dimg.ndim == 3 else dimg.astype(np.float32)
            v = dd > 0                     # -1 is sky, not a measurement
            lo, hi = 1.0, 30.0
            g = np.zeros(dd.shape, np.uint8)
            g[v] = np.clip((1.0 - (np.clip(dd[v], lo, hi) - lo) / (hi - lo)) * 255,
                           0, 255).astype(np.uint8)
            col = cv2.applyColorMap(g, cv2.COLORMAP_TURBO)
            col[~v] = 0                    # sky stays black so it cannot read as near geometry
            inset = cv2.resize(col, (DW - 24, DH - 60))
            cv.paste(Image.fromarray(inset[:, :, ::-1]), (dx0 + 12, dy0 + 28))
            # scale strip, labelled with the range the colours actually span
            sy = dy0 + DH - 26
            for k in range(DW - 24):
                frac = k / float(DW - 25)
                c8 = int(np.clip((1.0 - frac) * 255, 0, 255))
                bgr = cv2.applyColorMap(np.uint8([[c8]]), cv2.COLORMAP_TURBO)[0, 0]
                d.line([dx0 + 12 + k, sy, dx0 + 12 + k, sy + 10],
                       fill=(int(bgr[2]), int(bgr[1]), int(bgr[0])))
            d.text((dx0 + 12, sy + 11), f"{lo:.0f}", font=f["sm"], fill=(152, 158, 168))
            d.text((dx0 + DW - 52, sy + 11), f"{hi:.0f}+ m", font=f["sm"], fill=(152, 158, 168))
            ctr = dd[dd.shape[0] // 2, dd.shape[1] // 2]
            d.text((dx0 + 150, sy + 11),
                   f"centre {ctr:.2f} m   valid {v.mean()*100:.0f}%",
                   font=f["sm"], fill=(152, 158, 168))

        # top-down
        RX = 1330
        card(d, (RX, 72, CW - 24, 72 + TD + 56), "TOP-DOWN", "frozen route and actual pose", f)
        ax, ay = float(r["actual_x_cm"]), float(r["actual_y_cm"])
        yaw = float(r["actual_yaw_deg"])
        d.text((RX + 14, 110), f"X {ax/100:8.2f}   Y {ay/100:8.2f}   yaw {yaw:7.2f} deg",
               font=f["sm"], fill=INK)
        td = td_base.copy()
        dd = ImageDraw.Draw(td)
        walked.append(td_px(ax, ay))
        if len(walked) > 1:
            dd.line(walked, fill=BLUE, width=3, joint="curve")
        pxy = td_px(ax, ay)
        a = math.radians(yaw - 90.0)
        dd.polygon([(pxy[0] + 12 * math.cos(a), pxy[1] + 12 * math.sin(a)),
                    (pxy[0] + 9 * math.cos(a + 2.5), pxy[1] + 9 * math.sin(a + 2.5)),
                    (pxy[0] + 9 * math.cos(a - 2.5), pxy[1] + 9 * math.sin(a - 2.5))],
                   fill=ORANGE, outline=(140, 60, 20))
        cv.paste(td, (RX + 4, 132))

        # collision state, from the frozen route's validation
        y = 72 + TD + 70
        card(d, (RX, y, CW - 24, y + 58), "BODY COLLISION", None, f)
        hit = (traj["collision"]["collision_count"] > 0)
        d.text((RX + 14, y + 32),
               "route validated: no capsule hit, no near-plane penetration" if not hit
               else f"{traj['collision']['collision_count']} hits in the frozen route",
               font=f["sm" if not hit else "n"], fill=MUT if not hit else RED)

        # revisit window
        y2 = y + 72
        card(d, (RX, y2, CW - 24, y2 + 58), "REVISIT WINDOW", None, f)
        if aw[0] <= i <= aw[1]:
            msg, col = "anchor observation", BLUE
        elif any(e["window"][0] <= i <= e["window"][1]
                 for e in (rv.get("revisit_events") or [])):
            ev = next(e for e in rv["revisit_events"]
                      if e["window"][0] <= i <= e["window"][1])
            msg, col = (f"revisit #{ev.get('excursion')}  age {ev.get('realised_age_s', 0):.1f} s"
                        f"  closure {ev.get('translation_error_cm', 0):.0f} cm / "
                        f"{ev.get('rotation_error_deg', 0):.1f} deg"), ORANGE
        # rw is None for a family that has no revisit by construction - the coverage walk
        # covers every road once and never returns to an anchor. Absent is not zero-length.
        elif rw and rw[0] <= i <= rw[1]:
            msg, col = (f"revisit  age {rv.get('anchor_to_revisit_age_s', 0):.1f} s  "
                        f"closure {rv.get('translation_error_cm', 0):.0f} cm"), ORANGE
        else:
            msg, col = "-", MUT
        d.text((RX + 14, y2 + 32), msg, font=f["n"], fill=col)

        # clearance ahead
        y3 = y2 + 72
        card(d, (RX, y3, CW - 24, CH - 28), "CLEARANCE AHEAD", "in front of the camera", f)
        c = None
        if clear and i < len(clear):
            c = clear[i]
        if c is None:
            d.text((RX + 14, y3 + 44), "no hit", font=f["big"], fill=MUT)
        else:
            col = RED if c < 60 else (ORANGE if c < 150 else INK)
            d.text((RX + 14, y3 + 44), f"{c/100:.2f}", font=f["big"], fill=col)
            d.text((RX + 176, y3 + 76), "m", font=f["n"], fill=MUT)
            meter(d, RX + 210, y3 + 62, CW - 42 - (RX + 210), min(1.0, c / 1500.0), BLUE)
        d.text((RX + 14, CH - 64), "forward ray from the frozen pose, measured at freeze time",
               font=f["sm"], fill=MUT)
        d.text((RX + 14, CH - 48), "real collision geometry, not a depth sample",
               font=f["sm"], fill=MUT)

        # commanded action
        BY = 806
        card(d, (24, BY, 660, BY + 196), "COMMANDED ACTION", "(from the frozen trajectory)", f)
        dstep = math.dist((float(r["desired_x_cm"]), float(r["desired_y_cm"])),
                          (float(rows[max(0, i-1)]["desired_x_cm"]),
                           float(rows[max(0, i-1)]["desired_y_cm"]))) if i else 0.0
        dspeed = dstep / 100.0 * fps
        dyaw = (float(r["desired_yaw_deg"]) - float(rows[max(0, i-1)]["desired_yaw_deg"])
                + 540) % 360 - 180 if i else 0.0
        crows = [("move forward", dspeed, 1.5, "m/s", False),
                 ("move right", 0.0, 0.7, "n/a", True),
                 ("look yaw", dyaw * fps, yaw_lim, "deg/s", False),
                 ("look pitch", float(r["desired_pitch_deg"]), 15.0, "deg", False)]
        for k, (lab, v, lim, note, na) in enumerate(crows):
            yy = BY + 46 + k * 32
            d.text((38, yy - 2), lab, font=f["n"], fill=MUT if na else INK)
            bipolar(d, 168, yy, 200, v, lim, na=na)
            d.text((382, yy - 2), "   -   " if na else f"{v:+.3f}",
                   font=f["n"], fill=MUT if na else INK)
            d.text((464, yy - 1), f"of {lim:.2f}", font=f["sm"], fill=MUT)
            d.text((556, yy - 1), note, font=f["sm"], fill=MUT)

        # measured state
        card(d, (672, BY, 1305, BY + 196), "MEASURED STATE", "(read back from the engine)", f)
        spd = float(r["speed_m_s"])
        perr = float(r["pos_error_cm"])
        d.text((686, BY + 44), "speed", font=f["n"], fill=INK)
        meter(d, 830, BY + 46, 216, spd / 2.0)
        s1 = f"{spd:.2f} m/s"
        d.text((1291 - d.textlength(s1, font=f["n"]), BY + 44), s1, font=f["n"], fill=INK)
        d.text((686, BY + 76), "commanded pose", font=f["n"], fill=BLUE)
        meter(d, 830, BY + 78, 216, 1.0, BLUE)
        d.text((686, BY + 108), "pose error", font=f["n"], fill=ORANGE)
        meter(d, 830, BY + 110, 216, min(1.0, perr / 1.0))
        s2 = f"{perr*10:.3f} mm"
        d.text((1291 - d.textlength(s2, font=f["n"]), BY + 108), s2, font=f["n"], fill=INK)
        # agree with the acceptance gate's own limit (1 cm) rather than flagging a passing value
        ok = perr < 1.0
        d.text((686, BY + 148),
               f"camera is where the trajectory said ({perr*10:.2f} mm)" if ok
               else f"camera is {perr:.2f} cm off the commanded pose",
               font=f["n"], fill=MUT if ok else RED)

        # ribbons
        TY = BY + 210
        x0, x1 = 150, 1305
        # every revisit window, not just the last: a nested episode has several, and showing one
        # makes the earlier revisit invisible in the very video meant to expose problems
        rev_spans = [tuple(e["window"]) for e in (traj.get("revisit_events") or [])] \
            or ([tuple(rw)] if rw else [])
        for k, (lab, spans, col) in enumerate([
                ("anchor", [tuple(aw)], BLUE),
                (f"revisit x{len(rev_spans)}", rev_spans, ORANGE)]):
            ry = TY + 18 + k * 20
            d.text((24, ry - 5), lab, font=f["sm"], fill=MUT)
            d.rectangle([x0, ry - 5, x1, ry + 9], fill=(232, 231, 229))
            for a2, b2 in spans:
                pa = x0 + (x1 - x0) * a2 / max(1, N - 1)
                pb = x0 + (x1 - x0) * max(b2, a2 + 1) / max(1, N - 1)
                d.rectangle([pa, ry - 5, pb, ry + 9], fill=col)
        for s in range(0, int(N / fps) + 1, 10):
            d.text((x0 + (x1 - x0) * (s * fps) / max(1, N - 1), TY - 2), f"{s}s",
                   font=f["sm"], fill=MUT)
        ph = x0 + (x1 - x0) * i / max(1, N - 1)
        d.line([ph, TY + 8, ph, TY + 52], fill=INK, width=2)

        proc.stdin.write(np.asarray(cv).tobytes())

    proc.stdin.close()
    proc.wait(timeout=3600)
    print(f"[qa] {ep / out}  {(ep / out).stat().st_size/1e6:.1f} MB")
    return ep / out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episodes", nargs="+")
    a = ap.parse_args()
    for e in a.episodes:
        render(Path(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
