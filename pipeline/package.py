#!/usr/bin/env python3
"""Turn a captured episode into the standard data package of spec section 13, and run the
acceptance gates of section 14.

Nothing here touches UE. It reads what the capture stage recorded and derives the rest, so it can
be re-run and corrected without spending GPU time again.

Directories the spec lists but this build cannot fill (`depth/`, `instance/`, `semantic/`) are
not created empty and silently: the reason is written into `acceptance.json` and `SOURCE.md`, so
a missing modality is visible as a stated gap rather than looking like an oversight.
"""
import csv
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import geom  # noqa: E402


def read_frames(ep):
    """Every row of frames.csv, checked for completeness before anything computes on it.

    csv.DictReader fills a short line's missing columns with None, so a truncated or malformed
    row does not fail here - it fails hundreds of lines later as
    `float() argument must be ... not 'NoneType'` inside whichever gate touches that column
    first, naming neither the row nor the file. That is what one recovered episode did, and the
    traceback pointed at an acceptance gate rather than at the table.

    A malformed table is a refusal, not something to patch over: the frame it describes has no
    pose, and a row silently dropped here would leave a hole that `frame_gap` would report as an
    engine fault.
    """
    with open(ep / "frames.csv") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise RuntimeError(f"{ep / 'frames.csv'} has a header but no rows")

    bad = None
    for i, r in enumerate(rows):
        missing = sorted(k for k, v in r.items() if v is None)
        extra = r.get(None)
        if missing or extra:
            bad = (i, r, missing, extra)
            break
    if bad is None:
        return rows

    i, r, missing, extra = bad
    # Quote the offending line verbatim. Measured on this job: rows were short by exactly the
    # last seven columns, truncated immediately after `c2w` - a 150-character field - which is a
    # partially flushed write buffer, not a missing value. A missing value would be written as an
    # empty column and read back as '', failing as a ValueError rather than as None. Without the
    # raw line that distinction is invisible, and it is the whole diagnosis.
    raw = ""
    try:
        with open(ep / "frames.csv") as fh:
            for k, line in enumerate(fh):
                if k == i + 1:      # +1 for the header
                    raw = line.rstrip("\n")
                    break
    except Exception:
        pass
    detail = (f"{ep / 'frames.csv'} row {i} of {len(rows)} (frame_id {r.get('frame_id')!r}) is "
              + (f"short: {len(missing)} of {len(r)} columns absent, first {missing[:6]}"
                 if missing else f"long: {len(extra)} fields past the header, {extra[:4]}")
              + f". The line is {len(raw)} bytes and ends {raw[-60:]!r}")

    # Delivery runs may take the well-formed PREFIX instead of refusing. This is the same
    # semantics as `allow_short_episode`: the episode becomes what is actually well-formed, the
    # shortfall is recorded, and the frames past it are set aside rather than shipped without
    # poses. Refusing is still the default, because a malformed table shipped quietly is worse
    # than no table - but refusing an otherwise complete 4-hour episode over its last 0.08 h is
    # not a good trade when the caller has asked for delivery.
    if os.environ.get("DELIVER_PARTIAL", "").strip().lower() in ("1", "true", "yes"):
        keep = rows[:i]
        if not keep:
            raise RuntimeError(detail + ". DELIVER_PARTIAL is set but the FIRST row is already "
                                        "malformed, so there is no well-formed prefix to deliver")
        (ep / "_table_truncated.json").write_text(json.dumps({
            "kept_rows": len(keep), "table_rows": len(rows), "first_bad_row": i,
            "first_bad_frame_id": r.get("frame_id"), "absent_columns": missing,
            "raw_line_bytes": len(raw), "raw_line_tail": raw[-120:],
            "why": "frames.csv was malformed from this row on; the episode was delivered as the "
                   "well-formed prefix. This is a defect in the table, not in the frames - see "
                   "revisit_pipeline/FINDINGS.md.",
        }, indent=1) + "\n")
        print(f"[package] DELIVER_PARTIAL: {detail}", flush=True)
        print(f"[package] delivering the first {len(keep)} of {len(rows)} rows", flush=True)
        return keep
    raise RuntimeError(detail + ". Fix the table, do not package around it; set "
                                "DELIVER_PARTIAL=1 to deliver the well-formed prefix instead")


def write_table(ep, rows, name):
    """parquet if pyarrow is present, csv otherwise - and the package records which, so a
    consumer is never left guessing why frames.parquet is absent."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        cols = {k: [r[k] for r in rows] for k in rows[0]}
        for k, v in cols.items():
            if k in ("phase", "c2w"):
                continue
            try:
                cols[k] = [float(x) for x in v]
            except ValueError:
                pass
        pq.write_table(pa.table(cols), ep / f"{name}.parquet")
        return f"{name}.parquet"
    except ImportError:
        with open(ep / f"{name}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        return f"{name}.csv"


def contact_sheet(ep, rows, n=12):
    """A strip of frames across the episode. Section 14 step 15 is a human check, and it is the
    one that caught a camera standing on a rooftop when every numeric metric looked healthy."""
    import cv2
    total = len(rows)
    idxs = [int(i * (total - 1) / (n - 1)) for i in range(n)]
    tiles = []
    for i in idxs:
        # The JPEGs, not rgb.mp4: they are what is shipped, and seeking a long episode's mp4 by
        # frame index is both slow and unreliable.
        fr = cv2.imread(str(ep / "rgb" / f"{i:06d}.jpg"))
        if fr is None:
            continue
        t = cv2.resize(fr, (320, 180))
        # label from the frame's own recorded time rather than a hardcoded rate
        ts = float(rows[min(i, len(rows) - 1)]["episode_time_s"])
        lab = f"{i}  {ts:.2f}s"
        cv2.putText(t, lab, (6, 172), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(t, lab, (6, 172), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1,
                    cv2.LINE_AA)
        tiles.append(t)
    if not tiles:
        return None
    cols = 4
    rowsi = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    wid = max(r.shape[1] for r in rowsi)
    rowsi = [np.pad(r, ((0, 0), (0, wid - r.shape[1]), (0, 0))) for r in rowsi]
    cv2.imwrite(str(ep / "contact.png"), np.vstack(rowsi))
    return "contact.png"


# Thresholds borrowed from UE5-Agent-Data's check_physics_consistency, where they were measured
# rather than chosen. Their earlier 0.35 marked sound trajectories INVALID for sliding along a
# wall - which is what an agent is supposed to do - and the split turned out to be clean:
#
#   opposing dot >= 0.85 (head-on)   272 frames   median forward 0.00 cm
#   opposing dot 0.60-0.85 (oblique)  47 frames   median forward 6.67 cm  (full free speed)
#
# So only a near-head-on normal counts as blocking. And the FIRST frame of a contact is not a
# blocked frame: the agent moved freely through that step and the hit is what ended it - entry
# frames run at 5.97 cm/frame against 0.00 for sustained contact on the same wall.
OPPOSING_NORMAL_MIN = 0.85
PHYSICS_MIN_SAMPLES = 20
PHYSICS_SKIP_CONTACT_ENTRY = True


def physics_consistency(rows):
    """Did the world actually stop the agent when it said it was blocked?

    Returns None when the recording carries no contact channel at all, which is the case for a
    teleported capture: forcing the camera onto a commanded pose means no collision is ever
    resolved, so there is nothing to check. That is a property of the capture mode, not a pass -
    the caller must report it as such rather than as a green gate.

    This is the one check that tests the whole chain rather than the file layout: if it passes,
    action, geometry, physics and recorded state genuinely agree.
    """
    if not rows or "collision_hits" not in rows[0]:
        return None

    blocked, free = [], []
    was_opposing = False
    for r in rows:
        if float(r.get("cmd_move_forward", 0) or 0) <= 0.5:
            continue
        yaw = math.radians(float(r["actual_yaw_deg"]))
        dx = float(r.get("step_x_cm", 0) or 0)
        dy = float(r.get("step_y_cm", 0) or 0)
        # Project onto the heading: lateral sliding is expected and is not forward progress.
        along = dx * math.cos(yaw) + dy * math.sin(yaw)

        opposing = False
        try:
            hits = json.loads(r.get("collision_hits") or "[]")
        except json.JSONDecodeError:
            hits = []
        for h in hits:
            n = h.get("impact_normal")
            if not n or len(n) != 3:
                continue
            # The normal points out of the surface, back toward the agent, so a wall ahead has a
            # normal pointing against the heading. The capsule touches the floor on essentially
            # every frame and a step or slope has an upward normal - counting those would assert
            # a physics failure because the agent kept walking up a stair.
            if -(n[0] * math.cos(yaw) + n[1] * math.sin(yaw)) > OPPOSING_NORMAL_MIN:
                opposing = True
                break

        if opposing and PHYSICS_SKIP_CONTACT_ENTRY and not was_opposing:
            was_opposing = True
            continue          # entry frame belongs in neither bucket
        was_opposing = opposing
        (blocked if opposing else free).append(along)

    return {"free": free, "blocked": blocked,
            "free_median_cm": float(np.median(free)) if free else None,
            "blocked_median_cm": float(np.median(blocked)) if blocked else None}


def rgb_overlap_proxy(ep, aw, rw):
    """How much of the anchor view is findable in the revisit view, by ORB feature matching.

    This is NOT the depth-visible overlap of spec section 12 and must not be recorded under that
    name: it counts matched image features, not surface points confirmed visible by a z-buffer
    test, and it will happily match a repeated facade in a different place - which is exactly the
    confusion the similar-region family is designed to probe. It is here because it is the
    strongest available evidence that a revisit really returned to the anchor's view while metric
    depth is unavailable, and it is labelled so nobody mistakes it for the real gate.
    """
    import cv2
    a = ep / "rgb_keyframes" / f"{aw[1]:06d}.png"
    r = ep / "rgb_keyframes" / f"{rw[1]:06d}.png"
    if not (a.exists() and r.exists()):
        return None
    ga = cv2.cvtColor(cv2.imread(str(a)), cv2.COLOR_BGR2GRAY)
    gr = cv2.cvtColor(cv2.imread(str(r)), cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(2000)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gr, None)
    if da is None or db is None or not len(ka) or not len(kb):
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    good = [m for m in matches if m.distance < 40]
    disp = [float(np.hypot(*(np.array(kb[m.trainIdx].pt) - np.array(ka[m.queryIdx].pt))))
            for m in good]
    return {
        "method": "ORB mutual nearest-neighbour matches, Hamming distance < 40",
        "is_not": "depth-visible overlap; no depth exists on this build",
        "anchor_frame": aw[1], "revisit_frame": rw[1],
        "keypoints_anchor": len(ka), "keypoints_revisit": len(kb),
        "mutual_good_matches": len(good),
        "match_ratio": len(good) / max(1, min(len(ka), len(kb))),
        "match_displacement_px_median": float(np.median(disp)) if disp else None,
        "mean_abs_grey_diff": float(np.abs(ga.astype(int) - gr.astype(int)).mean()),
    }


def has_revisits(traj):
    """Does this family have a memory anchor and revisits at all?

    `coverage_walk` does not: it walks every road once rather than returning to one place. The
    section 12 gates are then INAPPLICABLE, which is a third thing from passing and from failing,
    and the packager has to say so - a revisit gate reported as `pass` on an episode with no
    revisit would be exactly the "confident wrong answer" this pipeline keeps being bitten by.
    """
    return bool(traj.get("revisit_window")) or bool(traj.get("revisit_events"))


NO_REVISIT_REASON = ("inapplicable to the {family} family: it covers every road once and has no "
                     "memory anchor or revisit, so there is no pose to close back onto")


def revisits_not_applicable(traj, rows):
    """Every key the gates and the page read, with the revisit quantities explicitly absent."""
    fps = float(traj["fps"])
    family = traj.get("trajectory_family", traj.get("task", {}).get("trajectory_family", "?"))
    reason = NO_REVISIT_REASON.format(family=family)
    return {
        "applicable": False,
        "reason": reason,
        "anchor_window": traj.get("anchor_window"),
        "revisit_window": None,
        "anchor_to_revisit_age_s": None,
        "actual_episode_duration_s": len(rows) / fps,
        "occluded_duration_s": None,
        "relative_se3_translation_cm": None,
        "relative_se3_rotation_deg": None,
        "translation_error_cm": None,
        "rotation_error_deg": None,
        "revisit_kind": None,
        "revisit_events": [],
        "exact_pass": None,
        "depth_visible_overlap": None,
        "rgb_feature_overlap_proxy": None,
        "fully_occluded_interval_s": None,
        "sink_revisit_overlap": None,
        "distractor_frame_ids": [],
        "not_computed": {k: reason for k in
                         ("depth_visible_overlap", "fully_occluded_interval_s",
                          "sink_revisit_overlap", "occluded_duration_s")},
    }


def revisits(ep, rows, traj):
    """Anchor/revisit bookkeeping for spec section 12, with the depth-based parts marked absent
    rather than approximated - overlap computed from anything other than depth would be a
    different quantity wearing the same name."""
    fps = float(traj["fps"])
    if not has_revisits(traj):
        return revisits_not_applicable(traj, rows)
    aw, rw = traj["anchor_window"], traj["revisit_window"]

    def pose(i):
        r = rows[min(i, len(rows) - 1)]
        return (np.array([float(r["actual_x_cm"]), float(r["actual_y_cm"]),
                          float(r["actual_z_cm"])]),
                float(r["actual_yaw_deg"]))

    pa, ya = pose(aw[1])
    pr, yr = pose(rw[1])
    dt = float(np.linalg.norm(pr - pa))
    dyaw = abs((yr - ya + 540) % 360 - 180)
    age = (rw[1] - aw[1]) / fps
    return {
        "applicable": True,
        "anchor_window": aw,
        "revisit_window": rw,
        "anchor_to_revisit_age_s": age,
        "actual_episode_duration_s": len(rows) / fps,
        "occluded_duration_s": None,
        "relative_se3_translation_cm": dt,
        "relative_se3_rotation_deg": dyaw,
        "translation_error_cm": dt,
        "rotation_error_deg": dyaw,
        "revisit_kind": traj["task"].get("return_kind", "near"),
        # a nested episode has several revisits from the same anchor; reporting only the last one
        # would hide whether the earlier, shorter-age revisit actually closed
        "revisit_events": [
            {"excursion": ev.get("excursion"),
             "window": ev["window"],
             "requested_age_s": ev.get("requested_age_s"),
             "realised_age_s": (ev["window"][1] - aw[1]) / fps,
             "translation_error_cm": float(np.linalg.norm(pose(ev["window"][1])[0] - pa)),
             "rotation_error_deg": abs((pose(ev["window"][1])[1] - ya + 540) % 360 - 180)}
            for ev in (traj.get("revisit_events") or [])],
        "exact_pass": bool(dt <= 2.0 and dyaw <= 1.0),
        "depth_visible_overlap": None,
        "rgb_feature_overlap_proxy": rgb_overlap_proxy(ep, aw, rw),
        "fully_occluded_interval_s": None,
        "sink_revisit_overlap": None,
        "distractor_frame_ids": [],
        "not_computed": {
            "depth_visible_overlap": "requires metric depth; unavailable in this build",
            "fully_occluded_interval_s": "requires metric depth for visibility",
            "sink_revisit_overlap": "requires depth-based overlap",
            "occluded_duration_s": "requires depth-based visibility",
        },
    }


def acceptance(ep, rows, traj, summary, rv):
    """Section 14's gates, each with the evidence that decided it.

    A gate that cannot be evaluated is `skipped` with a reason, never `pass` - counting an
    unevaluated gate as passing is how a broken dataset gets shipped.
    """
    fps = float(traj["fps"])
    gates = []

    def gate(name, ok, detail, skipped=False, advisory=False):
        # advisory: reported as "warn" when it fails, never counted against acceptance. Used for
        # statistics that describe the episode rather than validate it (the action mix).
        gates.append({"gate": name,
                      "result": "skip" if skipped else ("pass" if ok else ("warn" if advisory else "FAIL")),
                      "detail": detail})

    n = len(rows)
    # A DECLARED early stop is a fact about the episode, not a defect in it. `capture_engine`
    # already supports one - under `allow_short_episode` the episode becomes what was actually
    # recorded - and `finalise` records `ended_early_reason` and `planned_frames` when it happens,
    # which is also how a recovered episode arrives: its capture was killed, so it is shorter than
    # its plan by construction. This gate used to compare the delivered count against the FROZEN
    # count either way, so every such episode failed, and `capture_engine`'s own short-episode
    # path produced data that `package.py` then rejected.
    #
    # The defect this exists to catch is frames going missing with NOTHING declaring it. That case
    # still fails, because nothing wrote `ended_early_reason`. The discriminator is the
    # declaration, not the count.
    early = summary.get("ended_early_reason")
    if early:
        planned = int(summary.get("planned_frames") or traj["frames"])
        gate("frame_count_matches_frozen", True,
             f"{n} frames delivered of a planned {planned} ({planned - n} short, "
             f"{100.0 * n / max(planned, 1):.1f}% of plan). Declared early stop: {early}")
    else:
        gate("frame_count_matches_frozen", n == traj["frames"],
             f"{n} recorded vs {traj['frames']} frozen")

    t_err = max(abs(float(r["episode_time_s"]) - int(r["frame_id"]) / fps) for r in rows)
    gate("time_base_exact", t_err < 1e-9, f"max |t - frame/{fps:.0f}| = {t_err:.3e} s")

    # These four gates read the DELIVERED per-frame JPEGs, not rgb.mp4.
    #
    # They used to decode rgb.mp4 from end to end, which was wrong in two ways. It judged an
    # artifact that is excluded from the upload (`--exclude rgb.mp4`) rather than the frames that
    # are shipped, so a duplicate or a black region was measured after an H.264 round trip. And it
    # does not scale: a 20 h episode is 1.73 M frames, a ~110 GB mp4, and decoding it in sequence
    # costs more than the capture did.
    #
    # Sampled, and the sampling is reported. Duplicates need CONSECUTIVE frames, so the sample is
    # of adjacent PAIRS - frame i and i+1 - rather than of single frames: a stride of single
    # frames half a second apart would find no duplicates on any episode, which would look like
    # a pass. What this cannot claim is that no duplicate exists between the pairs it did not
    # look at, and the detail line says which pairs were examined.
    import cv2
    stride = max(1, int(os.environ.get("QA_FRAME_STRIDE", "12")))
    rgb_dir = ep / "rgb"
    present = sorted(rgb_dir.glob("*.jpg"))
    gate("all_frames_present", len(present) == n,
         f"{len(present)} JPEGs in rgb/ for {n} recorded frames")

    # Threaded for the same reason as qa_penetration.scan: independent per-frame decodes, and
    # cv2.imread releases the GIL. Full resolution deliberately - decoding at 1/4 scale is 4x
    # faster and measured a 10.17% zero-pixel median against 10.62% at full size, but
    # `no_duplicate_frames` and `no_blank_frames` are gates, and downscaling averages away
    # exactly the small differences they exist to find. Parallelism costs nothing in fidelity;
    # decimation would.
    idx = list(range(0, n, stride))

    def _moved(i):
        """Was the camera commanded to move between frame i and i+1? A hold is two identical
        commanded poses, and a converged TAA renders them near-identically - that is correct,
        not a duplicate (Sci-Fi Base, 18 Sep: the flagged pairs were holds)."""
        if i + 1 >= len(rows):
            return True
        a, b = rows[i], rows[i + 1]
        try:
            return any(abs(float(a[k]) - float(b[k])) > 1e-6 for k in
                       ("desired_x_cm", "desired_y_cm", "desired_z_cm", "desired_yaw_deg", "desired_pitch_deg"))
        except (KeyError, ValueError):
            return True
    def _sample(i):
        a = cv2.imread(str(rgb_dir / f"{i:06d}.jpg"))
        if a is None:
            return ("unreadable", None, None, None, None)
        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        zero = float((a.max(axis=2) <= 2).mean())
        is_blank = float(ga.max()) - float(ga.min()) < 2.0
        shape = a.shape[:2]
        if i + 1 < n and _moved(i):
            b = cv2.imread(str(rgb_dir / f"{i+1:06d}.jpg"))
            if b is None:
                return ("ok_pair_unreadable", zero, is_blank, shape, None)
            gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
            d = float(np.abs(gb.astype(np.int16) - ga.astype(np.int16)).mean())
            return ("ok", zero, is_blank, shape, d)
        return ("ok", zero, is_blank, shape, None)

    with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as pool:
        samples = list(pool.map(_sample, idx, chunksize=8))

    decoded, unreadable, blank, dup, dead_black = 0, 0, 0, 0, []
    vw = vh = None
    for kind, zero, is_blank, shape, diff in samples:
        if kind == "unreadable":
            unreadable += 1
            continue
        if kind == "ok_pair_unreadable":
            unreadable += 1
        decoded += 1
        if vw is None:
            vh, vw = shape
        # Pixels that are zero in every channel: REPORTED, not gated. See the dead_black note
        # below the gates for the measurement that took this from a gate to a statistic.
        dead_black.append(zero)
        if is_blank:
            blank += 1
        if diff is not None and diff < 0.05:
            dup += 1

    gate("frames_decode", unreadable == 0,
         f"{decoded} of {len(idx)} sampled frames decoded, {unreadable} unreadable "
         f"(stride {stride})")
    gate("resolution_matches_K", (vw, vh) == (traj["intrinsics"]["width"],
                                              traj["intrinsics"]["height"]),
         f"frames {vw}x{vh}, K built for "
         f"{traj['intrinsics']['width']}x{traj['intrinsics']['height']}")
    # One uniform frame (a face-on unlit wall) is a fact about the level; a black EPISODE is the
    # failure this gate exists for. Fails past 0.5% of the sampled frames.
    gate("no_blank_frames", blank <= max(0, int(0.005 * decoded)),
         f"{blank} blank frames among {decoded} sampled (stride {stride}); not a claim about "
         f"the {n - decoded} frames not sampled")
    gate("no_duplicate_frames", dup == 0,
         f"{dup} near-identical consecutive pairs among {len(idx)} sampled pairs (i, i+1) at "
         f"stride {stride}; not a claim about the pairs not sampled")

    # `no_dead_black_regions` USED TO BE A GATE HERE, at "median zero-pixel fraction <= 10%",
    # on the theory that a material which fails to compile falls back to the Default Material and
    # this build draws that as absolute black. It is now a reported statistic, because the gate
    # cannot tell that failure from a scene that is simply dark, and the separation it claimed
    # does not exist. Measured over 60 frames of each of ten shipped episodes:
    #
    #                                  zero%  median   largest-connected-black
    #   ModularNeighborhood (BROKEN)      28.0%              25.1%
    #   Modular_MedievalTown night         66.7%              55.9%   <- sound map
    #   Cave                               25.2%              18.8%   <- sound map
    #   ChemicalPlant                      11.1%               3.0%   <- sound map
    #   Tokyo / Downtown_West / Pyramids  0.0-1.7%          0.0-0.4%
    #
    # The one genuinely broken map sits BELOW two sound ones on both statistics, so no threshold
    # on either separates them - a night level and a cave crush to zero after tone mapping just
    # as the Default Material does. Confirmed against the historical verdicts: Cave and
    # Medieval_Nighttime both FAILED this gate while `materials_compiled` PASSED, and
    # ModularNeighborhood failed `materials_compiled` too - which named the four offending assets.
    # Three false positives out of three dark maps, and no true positive that
    # `materials_compiled` did not already catch with the asset names attached.
    #
    # `materials_compiled` is the real check: it reads the editor's own compile errors instead of
    # guessing from pixels. When it is skipped (no UE_LOG) that is reported as skipped, which is
    # an honest gap - unlike a gate that rejects every night scene.
    dead_black_stat = None
    if dead_black:
        db_med, db_max = float(np.median(dead_black)), float(max(dead_black))
        dead_black_stat = {
            "median": round(db_med, 4), "worst": round(db_max, 4),
            "frames_over_10pct": sum(1 for v in dead_black if v > 0.10),
            "sampled_frames": len(dead_black),
            "not_a_gate": "Zero-in-all-channels pixels, reported only. A dark scene reaches zero "
                          "the same way a Default Material fallback does: a sound night level "
                          "measured 66.7% here and a sound cave 25.2%, above the 28.0% of the one "
                          "map whose materials genuinely failed. Use materials_compiled, which "
                          "reads the editor's compile errors, to judge that.",
        }

    # The editor names the broken assets, and nothing downstream of the render can recover that.
    # Without this the failure reaches a human as "this map looks wrong" with no way to tell a
    # broken asset from a bad route, a dark scene, or our own capture path.
    # This gate now carries the whole weight of detecting a Default Material fallback, since the
    # pixel proxy above was demoted, so it must not pass on the wrong file. "No failure lines
    # found" is the same text whether the log is a clean editor log or a file that never had
    # editor output in it - and UE_LOG has in fact been pointed at a runner log, which passed
    # vacuously while the map's foliage was visibly broken. So require positive evidence that
    # this IS an editor log that got as far as compiling shaders: a real one carries LogInit
    # (118 lines in a sample) and LogShaderCompilers (8); the runner log carries 0 of each.
    ue_log = os.environ.get("UE_LOG", "")
    log_text = None
    if ue_log and Path(ue_log).exists():
        log_text = Path(ue_log).read_text(errors="replace")
    if log_text is None:
        gate("materials_compiled", False,
             "the editor log was not passed in (set UE_LOG), so a material that silently fell "
             "back to the Default Material cannot be detected here", skipped=True)
    elif not ("LogInit" in log_text and "LogShaderCompilers" in log_text):
        gate("materials_compiled", False,
             f"the file at UE_LOG ({Path(ue_log).name}) carries no LogInit/LogShaderCompilers "
             f"output, so it is not an editor log that reached shader compilation; treating it "
             f"as clean would be a pass earned by reading the wrong file", skipped=True)
    else:
        bad = []
        for line in log_text.splitlines():
            if "Failed to compile Material" in line:
                m = re.search(r"Content/(\S+?)\.uasset", line)
                bad.append(m.group(1) if m else line.strip()[:120])
        gate("materials_compiled", not bad,
             "every material in the level compiled for this shader platform" if not bad else
             f"{len(bad)} assets fell back to the Default Material: {', '.join(bad[:6])}"
             + (f" and {len(bad)-6} more" if len(bad) > 6 else ""))

    sm = summary["axis_smoke"]
    gate("axis_semantics", bool(sm["pass"]),
         f"forward->{sm['forward']}, forward+right->{sm['forward_right']}, "
         f"forward+up->{sm['forward_up']}")

    # 2 cm because that is section 12's exact-revisit translation limit, the tightest positional
    # tolerance the spec states - a per-frame tracking budget should not be looser than the
    # closure it has to support. The earlier 1 cm was picked out of the air; with a smooth
    # residual (mean 3 mm, p99 12 mm) and no blocked frames it was rejecting sound episodes.
    perr = [float(r["pos_error_cm"]) for r in rows]
    yerr = [float(r["yaw_error_deg"]) for r in rows]
    p99 = float(np.percentile(perr, 99))
    gate("pose_tracking", max(perr) < 2.0 and max(yerr) < 0.5,
         f"max position error {max(perr)*10:.3f} mm (limit 20 mm, from section 12's 2 cm "
         f"exact-revisit closure), p99 {p99*10:.2f} mm, max yaw error {max(yerr):.4f} deg")

    rc = traj.get("reachability")
    if rc and rc.get("available") is False:
        # Reported as skipped, never as passed. On maps whose origin is inside geometry UnrealCV
        # cannot spawn the camera-host Character at all, so no body can be placed at the poses.
        gate("route_reachable", False, str(rc.get("note", "no pawn on this map")), skipped=True)
    elif rc:
        # Lateral only. z disagreement between the navmesh surface and where a character capsule
        # rests is a convention difference, not an obstruction, and folding it into a 3D distance
        # rejected three sound maps at 7-30 cm while their x and y matched to the millimetre.
        gate("route_reachable", bool(rc["reachable"]),
             f"{rc['sampled_frames']} poses placed with the real body: max lateral "
             f"{rc.get('max_lateral_error_cm', rc.get('max_error_cm'))} cm "
             f"(tolerance {rc['tolerance_cm']} cm), z {rc.get('max_z_error_cm', 'n/a')} cm "
             f"(expected - navmesh vs capsule rest height), "
             f"{len(rc['unreachable_samples'])} blocked")

    c = traj["collision"]
    gate("collision_free", bool(c["collision_free"]),
         f"{c['collision_count']} capsule hits, {c['penetration_count']} near-plane "
         f"penetrations, min clearance {c['minimum_clearance_cm']} cm")
    gate("frame_translation_bounded", c["maximum_frame_translation_cm"] < 50.0,
         f"max {c['maximum_frame_translation_cm']:.2f} cm per frame")

    # Section 11 wants a maximum frame rotation and section 5 caps the yaw rate by tier. Recording
    # the maximum without gating it let a 12.08 deg single-frame snap (290 deg/s against a 58 deg/s
    # tier) through every other check: the pose was exactly as commanded, so pose_tracking passed,
    # and the trajectory was collision free. Only the rate exposes it.
    rates = [float(r["yaw_rate_deg_s"]) for r in rows]
    ylim = float(traj.get("yaw_deg_per_s", 60.0))
    worst = max(rates) if rates else 0.0
    over = [i for i, v in enumerate(rates) if v > ylim * 1.5]
    gate("frame_rotation_bounded", not over,
         f"max yaw rate {worst:.1f} deg/s against tier {ylim:.1f} deg/s "
         f"(limit {ylim*1.5:.1f}); {len(over)} frames over"
         + (f", first at {over[0]}" if over else ""))

    # Each revisit carries its own requested age. Comparing a single requested age against the
    # last revisit window reported "requested 25 s, realised 75 s" for a nested episode where both
    # ages were in fact hit exactly - the comparison, not the trajectory, was wrong.
    family = traj.get("trajectory_family",
                      traj.get("task", {}).get("trajectory_family", "?"))
    revisit_na = not rv.get("applicable", True)
    na = NO_REVISIT_REASON.format(family=family)

    evs = rv.get("revisit_events") or []
    if revisit_na:
        for g in ("revisit_age_matches_request", "memory_anchor_is_not_first_frame",
                  "revisit_pose_closure"):
            gate(g, False, na, skipped=True)
    elif evs:
        bad = [e for e in evs
               if abs(e["realised_age_s"] - e["requested_age_s"])
               > max(0.5, 0.05 * e["requested_age_s"])]
        gate("revisit_age_matches_request", not bad,
             "; ".join(f"#{e['excursion']} requested {e['requested_age_s']:.1f} s realised "
                       f"{e['realised_age_s']:.2f} s" for e in evs))
    else:
        want = float(traj["requested_age_s"])
        got = rv["anchor_to_revisit_age_s"]
        gate("revisit_age_matches_request", abs(got - want) <= max(0.5, 0.05 * want),
             f"requested {want:.1f} s, realised {got:.2f} s")
    if not revisit_na:
        gate("memory_anchor_is_not_first_frame", traj["anchor_window"][1] > 0,
             f"anchor window {traj['anchor_window']}")

    # A revisit that returns to the anchor's position while facing the other way has near-zero
    # visual overlap with what the anchor saw, so heading closure is gated, not just position.
    # Only the `exact` numbers come from the spec (section 12: <=2 cm, <=1 deg). The near and
    # partial tolerances are this project's choice: at 90 deg horizontal FOV a 1 m lateral offset
    # shifts the view by a few degrees at typical scene depth, so it still shares most of the
    # anchor's surface - which is what "near-pose revisit" is meant to mean.
    kind = rv.get("revisit_kind", "near")
    tol = {"exact": (2.0, 1.0), "near": (100.0, 10.0), "partial": (400.0, 45.0)}.get(
        kind, (100.0, 10.0))
    if revisit_na:
        checks, ok, worst = [], True, None
    else:
        checks = evs or [{"excursion": 1, "translation_error_cm": rv["translation_error_cm"],
                          "rotation_error_deg": rv["rotation_error_deg"]}]
        worst = max(checks, key=lambda e: (e["translation_error_cm"], e["rotation_error_deg"]))
        ok = all(e["translation_error_cm"] <= tol[0] and e["rotation_error_deg"] <= tol[1]
                 for e in checks)
        gate("revisit_pose_closure", ok,
             "; ".join(f"#{e.get('excursion')} {e['translation_error_cm']:.2f} cm / "
                       f"{e['rotation_error_deg']:.2f} deg" for e in checks)
             + f"  [{kind} limits {tol[0]:.0f} cm / {tol[1]:.0f} deg, project-chosen for "
               f"'near'; only 'exact' 2 cm/1 deg is from the spec]")

    speeds = [float(r["speed_m_s"]) for r in rows]
    lo, hi = {"slow": (0.3, 0.6), "medium": (0.6, 1.0),
              "fast": (1.0, 1.5)}[traj["speed_tier"]]
    p95 = float(np.percentile(speeds, 95))
    gate("speed_within_tier", p95 <= hi * 1.25,
         f"p95 speed {p95:.2f} m/s against tier {traj['speed_tier']} {lo}-{hi} m/s")
    # A task that pinned the pace (speed_m_s) is judged against the pin, from the MEASURED motion
    # of the walking frames: the plan writes exactly speed/fps per frame, so a median off the pin
    # means the engine did not move the camera where the plan said (a capsule pushed back, a
    # dropped frame), not a planner choice. Segment-end remainder frames make up ~2% of walking
    # frames by construction, so the median and the p95 are judged, not the minimum.
    if traj.get("speed_pinned") and traj.get("speed_m_per_s"):
        pin = float(traj["speed_m_per_s"])
        # PLANAR speed: the pace is pinned in the XY plane, and `speed_m_s` in frames.csv is the 3D
        # step. On a slope the 3D speed is 1/cos(slope) x the planar one, and the 60 cm/s vertical
        # slew alone makes it sqrt(1 + 0.36) = 1.166 m/s - exactly the p95 that rejected CastleRiver
        # and SnowMap (17 Sep) with a planar median of 1.000. Measured between consecutive rows.
        import math as _m
        wk = [r for r in rows if r.get("phase") in ("forward", "backward")]
        walking = []
        for a, b in zip(wk, wk[1:]):
            try:
                if int(b["frame_id"]) != int(a["frame_id"]) + 1:
                    continue
                dx = float(b["actual_x_cm"]) - float(a["actual_x_cm"]); dy = float(b["actual_y_cm"]) - float(a["actual_y_cm"])
                walking.append(_m.hypot(dx, dy) / 100.0 * fps)
            except (KeyError, ValueError):
                continue
        walking = walking or [float(r["speed_m_s"]) for r in wk] or speeds
        med, w95 = float(np.median(walking)), float(np.percentile(walking, 95))
        gate("speed_pinned_held", abs(med - pin) <= 0.02 * pin and w95 <= pin * 1.02,
             f"walking-frame PLANAR median {med:.3f} m/s, p95 {w95:.3f} m/s against the pinned "
             f"{pin:.2f} m/s (2% tolerance, {len(walking)} frame steps)")

    pitches = [abs(float(r["actual_pitch_deg"])) for r in rows]
    rolls = [abs(float(r["actual_roll_deg"])) for r in rows]
    gate("attitude_within_limits",
         max(pitches) <= traj["task"]["camera"]["pitch_limit_deg"] + 0.5
         and max(rolls) <= traj["task"]["camera"]["roll_limit_deg"] + 0.5,
         f"max |pitch| {max(pitches):.2f} deg, max |roll| {max(rolls):.2f} deg")

    prox = rv.get("rgb_feature_overlap_proxy")
    if revisit_na:
        gate("revisit_view_overlap_proxy", False, na, skipped=True)
    elif prox and prox.get("mutual_good_matches") is not None:
        # A near-featureless revisit view (blank wall, open sky) yields no matches: the count can
        # be 0 and the ratio/displacement None. That is a legitimate gate FAILURE, not a crash.
        mm = int(prox["mutual_good_matches"])
        ratio = prox.get("match_ratio")
        disp = prox.get("match_displacement_px_median")
        ratio_txt = f"{ratio*100:.1f}%" if ratio is not None else "n/a"
        disp_txt = f"{disp:.1f} px" if disp is not None else "n/a"
        gate("revisit_view_overlap_proxy", mm >= 50,
             f"{mm} mutual ORB matches ({ratio_txt}), median displacement "
             f"{disp_txt}. Proxy only - NOT the section 12 "
             f"depth-visible overlap, which needs depth this build cannot produce.")
    else:
        gate("revisit_view_overlap_proxy", False, "no anchor/revisit keyframes to compare",
             skipped=True)

    # Depth is now produced by SimWorldCapture::CaptureDepthEXR (FRHIGPUTextureReadback), so
    # these are real checks rather than skips. The files are verified to exist and to contain
    # plausible depth; the engine measured the statistics itself, so this does not depend on our
    # EXR reader - which turned out to be an opencv build with `OpenEXR: NO`.
    dpaths = [r.get("depth_path") for r in rows if r.get("depth_path")]
    if dpaths:
        missing = [d for d in dpaths if not (ep / d).exists()]
        gate("metric_depth_present", not missing and len(dpaths) == n,
             f"{len(dpaths)} of {n} frames carry depth, {len(missing)} files missing; "
             f"EXR, linear metres in R, -1 = sky/beyond range")
        vf = [float(r["depth_valid_fraction"]) for r in rows if r.get("depth_valid_fraction")]
        mn = [float(r["depth_min_m"]) for r in rows if r.get("depth_min_m")]
        mx = [float(r["depth_max_m"]) for r in rows if r.get("depth_max_m")]
        if vf:
            # An all-invalid frame means the capture produced nothing, whatever the file size
            # says. A frame that is entirely valid is fine indoors and suspicious outdoors, so
            # only the empty end is an error.
            dead = sum(1 for v in vf if v <= 0.001)
            # A frame with no valid depth is what the camera sees when it looks at nothing but
            # sky: every pixel is -1, exactly as the format says. That is data, not a failed
            # capture. A failed capture is MANY such frames, so the gate fails past 0.5% of the
            # episode (Courtyard 17 Sep: 2 of 27127 frames, refused a 19-minute episode).
            gate("depth_not_empty", dead <= max(0, int(0.005 * len(vf))),
                 f"valid-pixel fraction: min {min(vf)*100:.1f}%, median "
                 f"{float(np.median(vf))*100:.1f}%, max {max(vf)*100:.1f}%; {dead} frames "
                 f"with no valid depth at all")
            gate("depth_range_plausible",
                 min(mn) > 0.05 and max(mx) < 1000.0,
                 f"nearest {min(mn):.3f} m, farthest {max(mx):.1f} m across the episode "
                 f"(near clip and the 1000 m cutoff bound this)")
    else:
        gate("metric_depth_present", False,
             "this episode was captured before depth was wired in", skipped=True)

    # The route checks cannot see geometry with collision disabled, and foliage instances routinely
    # ship that way: a tree in RussianWinterTownDemo01 cut no hole in the navmesh, stopped no
    # capsule sweep, and the episode walked through its trunk for 1.25 s. Depth is the only
    # evidence, so it is checked here rather than trusted. Stride 2 - the event was 30 frames long,
    # and reading all 2844 EXRs costs a minute and a half.
    if dpaths:
        import qa_penetration as qp
        pen = qp.scan(ep, stride=2)
        if "error" in pen:
            gate("no_geometry_penetration", False, pen["error"], skipped=True)
        else:
            ev = pen["penetration_events"]
            gate("no_geometry_penetration", pen["penetrating_frames"] == 0,
                 f"nearest depth over the episode {pen['min_depth_m']:.3f} m, "
                 f"{pen['frames_at_near_clip']} frames with geometry on the near clip plane, "
                 f"{pen['penetrating_frames']} frames with over "
                 f"{qp.FRAC*100:.0f}% of the image inside {qp.NEAR_M:.2f} m"
                 + (f"; {len(ev)} event(s), first at t={ev[0]['t_s']:.2f}s "
                    f"(frames {ev[0]['first']}-{ev[0]['last']})" if ev else
                    " - the camera never enters geometry"))

    if revisit_na:
        gate("depth_visible_overlap", False, na, skipped=True)
        gate("fully_occluded_interval", False, na, skipped=True)
    else:
        gate("depth_visible_overlap", False,
             "depth now exists, but the anchor/revisit overlap computation of section 12 "
             "(reproject anchor depth into the revisit view and z-buffer test it) is not "
             "implemented yet - the RGB feature proxy below is NOT it", skipped=True)
        gate("fully_occluded_interval", False,
             "needs the depth-visible overlap above", skipped=True)
    gate("instance_semantic_masks", False, "P1 modality, not captured in this pilot",
         skipped=True)

    # What this family promises instead of a revisit: that it walked the whole map, and that the
    # actions are mixed rather than all forward. Both are measured from the delivered frames.
    if family == "coverage_walk":
        cov = traj.get("coverage") or {}
        gate("road_coverage_complete", cov.get("fraction") == 1.0,
             f"{cov.get('roads_walked')} of {cov.get('roads_total')} roads walked, "
             f"{cov.get('centreline_walked_m')} of {cov.get('centreline_m')} m of centreline "
             f"({(cov.get('fraction') or 0) * 100:.1f}%); {cov.get('method', '')}")

        # Re-measured from frames.csv rather than trusted from the plan: `phase` is written per
        # frame by the engine, so this counts what the episode contains.
        counts = {}
        for r in rows:
            counts[r.get("phase", "")] = counts.get(r.get("phase", ""), 0) + 1
        frac = {k: v / max(1, len(rows)) for k, v in counts.items()}
        target = traj.get("action_mix_target") or {}
        # One percentage point of tolerance on each band edge: the mix is a random walk's
        # statistic, and refusing a 19-minute episode for turn_left at 15.4% against a 15% edge
        # (Courtyard, 17 Sep) throws away data the band was never precise enough to judge.
        TOL = 0.01
        out_of_band = {k: round(frac.get(k, 0.0), 4) for k, (lo, hi) in target.items()
                       if not (lo - TOL <= frac.get(k, 0.0) <= hi + TOL)}
        gate("action_mix_in_band", not out_of_band,
             "measured from frames.csv phase labels: "
             + ", ".join(f"{k} {frac.get(k, 0.0)*100:.1f}%" for k in sorted(target))
             + (f"; OUT OF BAND: {out_of_band}" if out_of_band else "; all inside their bands"),
             advisory=True)   # 18 Sep: descriptive, not a validity gate (see FINDINGS)

        six = ("forward", "backward", "turn_left", "turn_right", "look_up", "look_down")
        absent = [a for a in six if counts.get(a, 0) == 0]
        gate("all_six_actions_present", not absent,
             f"frames per action: "
             + ", ".join(f"{a}={counts.get(a, 0)}" for a in six)
             + (f"; MISSING: {absent}" if absent else ""))

    failed = [g for g in gates if g["result"] == "FAIL"]
    skipped = [g for g in gates if g["result"] == "skip"]
    # 18 Sep, user's decision: NOTHING recorded is rejected after the fact. Every gate is still
    # measured and written here - the verdicts are the episode's quality record - but `accepted`
    # is always true and the uploader files every episode under the normal prefix. The gates that
    # still refuse are the ones BEFORE recording (collision, depth probe, disconnected network).
    return {
        "episode_id": summary["episode_id"],
        "accepted": True,
        "verdict_mode": "advisory",
        "gates_would_have_failed": [g["gate"] for g in failed],
        "accepted_seconds": (len(rows) / fps) if not failed else 0.0,
        "accepted_note": "Section 14 counts candidate, failed and incomplete data as 0 seconds. "
                         "This episode is accepted for the gates that can be evaluated on this "
                         "build; the skipped depth gates mean it is not a complete P0 episode "
                         "under section 9 and must not be counted toward the final target.",
        # Derived from the gates, not asserted. It was hardcoded to False with depth listed as
        # missing, which stayed wrong for the first episode that actually had depth - the kind of
        # claim that is only ever wrong in the direction of underselling, but still a claim the
        # data contradicts.
        "p0_complete": bool(
            any(g["gate"] == "metric_depth_present" and g["result"] == "pass" for g in gates)),
        "p0_missing": [g["gate"] for g in gates
                       if g["gate"] in ("metric_depth_present",) and g["result"] != "pass"]
                      + ["section 12 depth-visible overlap (computation not implemented)"],
        "gates_passed": len(gates) - len(failed) - len(skipped),
        "gates_failed": len(failed),
        "gates_skipped": len(skipped),
        "gates": gates,
        "video_stats": {"sampled_frames": decoded, "sample_stride": stride,
                        "blank": blank, "duplicate_pairs": dup, "unreadable": unreadable,
                        "width": vw, "height": vh,
                        "measured_on": "the delivered rgb/*.jpg, not rgb.mp4",
                        **({"dead_black_pixel_fraction": dead_black_stat}
                           if dead_black_stat else {})},
    }


def _hash_one(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_manifest(ep, workers=None):
    """Hash every delivered file, threaded, in path order.

    This reads the whole episode - 28 GB at 130k frames - and at one thread it was 12% of
    packaging. hashlib releases the GIL while it digests and the reads are independent, so
    threads give real parallelism here; the manifest is still emitted in sorted path order, so
    the file is byte-identical to the serial version.

    Read in 1 MiB chunks rather than `p.read_bytes()`: with several threads in flight, whole-file
    reads put as many complete files in memory at once, and rgb_proxy.mp4 alone is 240 MB.
    """
    if workers is None:
        workers = min(16, (os.cpu_count() or 4))
    paths = [p for p in sorted(ep.rglob("*"))
             if p.is_file() and p.name != "files.sha256"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        digests = list(pool.map(_hash_one, paths, chunksize=64))
    lines = [f"{h}  {p.relative_to(ep)}" for h, p in zip(digests, paths)]
    (ep / "files.sha256").write_text("\n".join(lines) + "\n")
    return len(lines)


def build(ep, gates=True):
    """Write the package. With gates=False the acceptance checks are SKIPPED.

    Skipping them is a deliberate operating choice, not a shortcut this code makes on its own:
    on this job the gates cost roughly an hour per 400k-frame episode and had, on the day, wrongly
    rejected two sound episodes and crashed on thirteen. The frames and the per-frame table are
    what the dataset is; the verdict is a separate product and `longvideo/readjudicate.py` can
    compute it afterwards from S3.

    What is NOT skipped is the metadata that makes the frames readable - camera.json,
    frames.parquet, sequence.json, revisits.json, SOURCE.md - because without those the upload is
    a pile of JPEGs. And acceptance.json is still written, saying plainly that the gates did not
    run: `accepted: null`, never `true`. An unjudged episode must not arrive labelled as a passing
    one, and uploader.sh reads that field to decide where it lands.
    """
    ep = Path(ep)
    rows = read_frames(ep)
    traj = json.loads((ep / "trajectory.json").read_text())
    summary = json.loads((ep / "capture_summary.json").read_text())

    intr = traj["intrinsics"]
    (ep / "camera.json").write_text(json.dumps({
        "fx": intr["fx"], "fy": intr["fy"], "cx": intr["cx"], "cy": intr["cy"],
        "width": intr["width"], "height": intr["height"],
        "hfov_deg": intr["hfov_deg"], "vfov_deg": intr["vfov_deg"],
        "fov_source": "vget /camera/N/fov read back from the engine after being set",
        "crop": None, "resize": None, "padding": None,
        "K_applies_to": "the delivered rgb.mp4 frames, unmodified since render",
        "source_axis_convention": "UE: left-handed, X forward, Y right, Z up, centimetres",
        "canonical_axis_convention": "x right, y down, z forward, metres, camera-to-world, "
                                     "column vectors",
        "ue_to_canonical_basis": "canonical columns are (right, -up, forward) of the UE camera "
                                 "basis",
        "coordinate_conversion_version": "geom-1",
        "rotation_truth": "quaternion (quat_x..quat_w) and the 4x4 c2w; both are derived from "
                          "the engine's Euler readback of the CameraComponent, which is the "
                          "only rotation form this build exposes. Euler is kept for reading.",
        "camera_source": "CameraComponent world transform via vget /camera/N/location|rotation",
        "near_clip_cm": None, "far_clip_cm": None, "lens_model": "pinhole, no distortion",
        "not_available": {"near_far_clip": "not exposed by UnrealCV on this build"},
        "depth": {
            "format": "EXR, linear metres, in the R channel; G and B are 0, A is 1",
            "reader_warning": "OpenCV decodes EXR as BGRA, so with cv2.imread the depth is "
                              "index 2, not 0. Index 0 is an all-zero plane that looks exactly "
                              "like a failed capture. An opencv build reporting `OpenEXR: NO` "
                              "returns None for a perfectly good file.",
            "invalid_value": -1.0,
            "invalid_meaning": "sky, unwritten pixels, and anything beyond max_range_m. Not a "
                               "measurement. Mask with depth > 0 before unprojecting.",
            "kind": "planar (distance along the camera view axis), not radial",
            # Taken from what the engine reported writing. It was hardcoded to 16 while the
            # files were float32 - a false claim in the metadata that nothing would have caught,
            # because a float32 EXR is a perfectly valid EXR.
            "bit_depth": (int(float(rows[0]["depth_bit_depth"]))
                          if rows and rows[0].get("depth_bit_depth") else "not recorded by this "
                          "capture (the field was added afterwards); measure it from the EXR "
                          "header - these files are float32 at ~1.7 MB/frame, because the "
                          "float16 flag was not wired to the source pixel format at the time"),
            "max_range_m": 1000.0,
            "source": "SimWorldCapture::CaptureDepthEXR - SCS_SceneDepth into an RTF_R32f target, "
                      "read back with FRHIGPUTextureReadback. NOT ReadLinearColorPixels, which on "
                      "Vulkan is not a float path and fires a checkf for R32F.",
            "verified": "centre pixel agreed with an independent line trace to 0.0-0.4 cm at "
                        "3.6-4.2 m and within 0.8% at 180-270 m",
            "pose": "captured at the ACTUAL camera pose read back from the engine, not the "
                    "commanded pose",
        },
    }, indent=1))

    frames_file = write_table(ep, rows, "frames")
    rv = revisits(ep, rows, traj)
    (ep / "revisits.json").write_text(json.dumps(rv, indent=1))
    if gates:
        acc = acceptance(ep, rows, traj, summary, rv)
    else:
        acc = {
            "episode_id": summary["episode_id"],
            "accepted": None,
            "gates_run": False,
            "gates": [],
            "gates_passed": 0, "gates_failed": 0, "gates_skipped": 0,
            "reason": "the acceptance gates were not run for this episode. `accepted` is null, "
                      "which means UNJUDGED - not passing and not failing. Nothing here asserts "
                      "the data is sound; equally, nothing here asserts it is not. Run "
                      "longvideo/readjudicate.py against this prefix to compute a verdict.",
            "frames": summary.get("frames"),
            "planned_frames": summary.get("planned_frames"),
        }
    (ep / "acceptance.json").write_text(json.dumps(acc, indent=1))

    (ep / "sequence.json").write_text(json.dumps({
        "episode_id": summary["episode_id"],
        "split": summary.get("split"),
        "map_id": summary["map_id"],
        "asset_family_id": summary["map_id"].split("/")[2]
        if summary["map_id"].count("/") > 2 else None,
        "spawn_id": summary["spawn_id"],
        "seed": summary["seed"],
        "ue_version": "5.8",
        "project_commit": None,
        "trajectory_family": summary["trajectory_family"],
        "trajectory_generator_version": summary["trajectory_generator_version"],
        "recorder_version": summary["recorder_version"],
        "speed_tier": summary["speed_tier"], "yaw_tier": summary["yaw_tier"],
        "camera_height_cm": summary["eye_height_cm"],
        "collision_radius_cm": traj["task"]["body"]["collision_radius_cm"],
        "fps": summary["fps"],
        "simulation_time_policy": "frozen trajectory evaluated at exactly frame_id / fps; no "
                                  "physics or animation drives the camera, so the pose for a "
                                  "given frame is reproducible from the frozen file alone",
        "resolution": summary["resolution"],
        "render": summary.get("render"),
        "license_provenance": "project assets under the gym_citynav project's own licensing; "
                              "not cleared for redistribution here",
        "frames_table": frames_file,
    }, indent=1))

    ct = contact_sheet(ep, rows)
    (ep / "SOURCE.md").write_text(f"""# {summary['episode_id']}

Map `{summary['map_id']}`, spawn `{summary['spawn_id']}`, seed {summary['seed']}.
Recorded with `pipeline/capture.py` ({summary['recorder_version']}) from the frozen trajectory
`{summary.get('frozen_sha256', 'n/a')[:16]}`, UE 5.8.

## What is here

- `rgb.mp4` - {summary['frames']} frames at {summary['fps']:.0f} FPS,
  {summary['resolution']}, H.264, faststart.
- `rgb_keyframes/` - lossless PNG for the anchor and revisit windows.
- `frames.{frames_file.split('.')[-1]}` - per-frame time, desired and actual pose, quaternion,
  canonical c2w, pose error, speed.
- `camera.json` - final K and the full coordinate contract.
- `trajectory.json` - the frozen route, its collision evidence and the reach fan it was chosen
  from.
- `revisits.json`, `acceptance.json`, `contact.png`, `files.sha256`.

## What is missing, and why

`depth/`, `instance/` and `semantic/` are absent. Depth is not a matter of a different request:
this build's Vulkan RHI cannot read back a render target at all - any attempt asserts at
`VulkanRenderTarget.cpp:171` and takes the editor down, and UnrealCV's own depth returns a
constant 65504, which is the fp16 maximum left in an unwritten buffer. Everything that depends
on depth (spec section 9 P0 depth, section 12 depth-visible overlap, occlusion intervals) is
therefore reported as not computed rather than estimated. This episode is **not** a complete P0
episode and does not count toward the section 17 target.
""")
    nfiles = sha256_manifest(ep)
    print(f"[package] {frames_file}, camera.json, sequence.json, revisits.json, "
          f"acceptance.json, {ct}, files.sha256 ({nfiles} files)")
    print(f"[package] accepted={acc['accepted']}  passed={acc['gates_passed']} "
          f"failed={acc['gates_failed']} skipped={acc['gates_skipped']}")
    for g in acc["gates"]:
        if g["result"] != "pass":
            print(f"    {g['result']:4s} {g['gate']}: {g['detail'][:110]}")
    return acc


def main():
    for a in sys.argv[1:]:
        build(Path(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
