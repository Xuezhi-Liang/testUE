#!/usr/bin/env python3
"""One long episode for one map, inside one editor session.

    SLUG=... MAP_ID=... DEADLINE_EPOCH=... PORT=9208 python3 runner.py

Differences from campaign/runner.py, all forced by the episode being 20 h rather than 60 s:

  - **One task, no quota loop.** The episode is the job.
  - **The cap is recomputed from the clock on every attempt.** There is no way to resume a
    capture partway - the frozen route is fine but the in-engine capture actor starts at frame 0 -
    so an editor lost at hour 15 means starting again. Retrying at the original 20 h would need
    20 more hours the instance does not have, so each attempt asks for what is left minus the
    overhead it will need to package and upload. A second attempt is a shorter episode, which is
    a dataset; a second attempt that runs out of instance is nothing.
  - **The time-of-day freeze is not optional.** It was already here for multi-hour sessions
    (ContainerYard's sun slid into dusk over three episodes and the black-region gate started
    rejecting everything). Over a single 20 h session it is the difference between one lighting
    condition and a full day-night cycle nobody asked for.
"""
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
import capture_engine as cape  # noqa: E402
import engine  # noqa: E402
import freeze_coverage as fzc  # noqa: E402
import package as pk  # noqa: E402

SHARD_ID = os.environ["SHARD_ID"]
SLUG = os.environ.get("SLUG") or SHARD_ID.split("__s")[0]
MAP_ID = os.environ["MAP_ID"]
STATE = HERE / f"state_{SHARD_ID}.json"
# Wall clock this machine has left, as an absolute epoch. Set by driver.sh from the instance's
# own budget so a relaunch does not reset it.
DEADLINE = float(os.environ.get("DEADLINE_EPOCH", "0")) or (time.time() + 20.5 * 3600)
# What packaging, the QA video and the final upload need after the recording stops. Measured on
# the 10 min IndustrialArea episode and scaled: sha256 over 3.5 M files is the long pole.
OVERHEAD_S = float(os.environ.get("OVERHEAD_S", str(1.5 * 3600)))
# Wall-clock seconds per second of footage. Measured on the fleet, not on the dev host: the two
# ChemicalPlant episodes that completed report engine_fps 14.423 and 14.432 against 24 fps of
# footage - 139,186 frames in 9,650 s - so an hour of footage costs 1.665 h of wall clock.
#
# The previous value, 0.906, came from a 14,400-frame run on the dev host at ~26.5 fps. It was
# 1.84x too optimistic for the fleet's g6.4xlarge, and every shard was therefore budgeted almost
# half the wall clock its own plan needed. That is why all 31 shards were still working when
# their 7 h budget expired, and why the watchdog found them mid-packaging.
#
# 16 Sep: the render defaults changed (fresh view state per frame, 2x supersampling - see
# RENDER_QUALITY.md) and the in-engine rate with them measured 9.6-9.9 fps on the dev host's
# g6.4xlarge (Hwaseong 9.91, ChemicalPlant 9.58) against 12.2-12.7 without supersampling and
# 14.4 for the old path. 24 / 9.75 = 2.46 wall seconds per second of footage. Kept slightly
# conservative at 2.5; a 7 h budget is therefore ~2.8 h of footage, not 4.2.
RECORD_REALTIME_RATIO = float(os.environ.get("RECORD_REALTIME_RATIO", "2.5"))
# Least centreline head-clearance pruning may keep before the route is refused as an island
# cover. Street maps keep >0.9 (Tokyo kept more than the survey predicted); the two courtyard maps
# that produced 8-minute "100% covers" kept 0.22 and 0.35.
KEPT_MIN = float(os.environ.get("KEPT_MIN", "0.75"))


def save(st):
    tmp = STATE.with_suffix(".part")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE)


def budget_seconds_of_footage():
    """Footage seconds that fit in the wall clock that is left."""
    left = DEADLINE - time.time() - OVERHEAD_S
    return left / RECORD_REALTIME_RATIO


class Terminated(Exception):
    """SIGTERM arrived - stop capturing and package what is on disk.

    driver.sh's watchdog used to send SIGKILL when the frame counter stopped moving. -9 runs no
    handler, and capture_engine writes the files that make frames an episode only after the last
    frame, so a kill mid-capture left hours of real frames that nothing downstream would look at:
    uploader.sh waits for acceptance.json. Thirteen shards of one run ended that way.

    The watchdog now sends SIGTERM first and waits 180 s. This turns that signal into a normal
    exception inside main()'s try, so the recovery path is the ordinary one: finalise the frames
    that exist, package them, and report a short episode with the reason - instead of leaving the
    disk to be salvaged by hand afterwards.
    """


def _on_term(signum, frame):
    print(f"[lv:{SHARD_ID}] SIGTERM - packaging the frames captured so far", flush=True)
    raise Terminated(f"terminated by signal {signum} while capturing")


def salvage(frozen_path):
    """Package whatever the interrupted capture left on disk, via the recovery path itself.

    Deliberately `recover.recover`, not a second implementation: it already decides how many
    frames are actually present, sets aside images written past the last engine state, and runs
    the same finalise and package a completed capture runs. Two code paths for "turn these frames
    into an episode" is how they drift apart, and the one used less often is the one that breaks.
    """
    import recover as rc
    fz = json.loads(Path(frozen_path).read_text())
    ep = rc.EPISODES / fz["episode_id"]
    if not (ep / "rgb").is_dir():
        return {"error": "the capture had not written any frames yet"}
    return rc.recover(ep)


def main():
    st = json.loads(STATE.read_text()) if STATE.exists() else {"attempts": [], "done": None}
    if st.get("done") == "accepted":
        print(f"[lv:{SHARD_ID}] already finished", flush=True)
        return 0

    # The shard's own cap, not a wall-clock division. What the clock still decides is whether
    # there is time left to attempt it at all: an attempt that cannot finish is worse than none,
    # because the instance stops with a partial episode and no acceptance.
    rc = os.system(f"python3 {HERE}/gen_task.py {SHARD_ID}")
    if rc != 0:
        return 2
    task_path = PIPE / "tasks" / f"lv_{SHARD_ID}.json"
    cap_s = float(json.loads(task_path.read_text())["max_duration_s"])
    footage = budget_seconds_of_footage()
    if footage < cap_s * 0.6:
        print(f"[lv:{SHARD_ID}] {footage/3600:.2f} h of footage fits in the remaining wall clock "
              f"but this shard asks for {cap_s/3600:.2f} h; recording what fits", flush=True)
        if footage < 900:
            st["done"] = "out_of_time"
            save(st)
            return 0
    task = json.loads(task_path.read_text())
    W, H = task["camera"]["resolution"]
    attempt = len(st["attempts"]) + 1
    print(f"[lv:{SHARD_ID}] attempt {attempt}: cap {cap_s/3600:.2f} h of footage "
          f"({(DEADLINE-time.time())/3600:.2f} h of wall clock left, "
          f"{OVERHEAD_S/3600:.2f} h reserved for packaging and upload)", flush=True)

    ucv = engine.connect(W, H)
    if ucv is None:
        print(f"[lv:{SHARD_ID}] UE never became reachable", flush=True)
        return 3

    r = engine.query(ucv.client.request, FREEZE_TOD, timeout=180)
    print(f"[lv:{SHARD_ID}] time-of-day freeze: {json.dumps(r)[:400]}", flush=True)

    t0 = time.time()
    rec = {"attempt": attempt, "cap_hours": round(cap_s / 3600, 3),
           "started": time.strftime("%F %T")}
    # Installed for the WHOLE run, not around capture() only. A SIGTERM during freeze used to hit
    # the default handler: the process died with no state written, the beacon stayed at
    # "starting", and eight MedievalCastle shards vanished without a recorded reason.
    signal.signal(signal.SIGTERM, _on_term)
    frozen_path = None
    try:
        frozen_path, frozen = fzc.freeze(task, ucv=ucv)
        rec["frozen_frames"] = frozen["frames"]
        rec["length_bound_by"] = (frozen.get("length") or {}).get("bound_by")
        # The same three refusals run_coverage.py applies, and for the same reason: a route the
        # pipeline would refuse must not be recorded for 20 hours because this runner forgot to
        # ask. A refusal is a specific, expected outcome - it is recorded as one, not as a crash.
        refusal = None
        if not frozen["collision"]["collision_free"]:
            refusal = ("refused_collision",
                       f"the route is not collision free "
                       f"({frozen['collision']['collision_count']} sweeps hit, "
                       f"{frozen['collision']['penetration_count']} near-plane penetrations). "
                       f"Fix the route - a different seed, or a map whose corridors are wider - "
                       f"not the recording.")
        elif not frozen["depth_probe"].get("clear", False):
            dp = frozen["depth_probe"]
            # `clear` is three vetoes ANDed together, and this message used to describe only the
            # first. Three MedievalNight shards were refused reading "0 of 499 frames too close,
            # nearest 73.7 cm" - a refusal that contradicted its own numbers - because the veto
            # was all-sky probes and the message never said so. Name what actually fired.
            why = []
            if dp.get("clamped_count"):
                ex = dp.get("frames_clamped") or []
                why.append(f"{dp['clamped_count']} of {dp.get('probed_frames')} probed frames had "
                           f"depth clamped at the near plane (<= {dp.get('clip_cm')} cm; e.g. "
                           + ", ".join(f"frame {e.get('frame')} {e.get('phase')} {e.get('min_cm')} cm"
                                       for e in ex[:3]) +
                           ") - the camera is inside geometry there, which is the one thing the "
                           f"depth probe exists to catch; {dp.get('too_close_count', 0)} more frames "
                           f"were merely within {dp.get('threshold_cm')} cm of a surface and are "
                           f"reported, not refused")
            if dp.get("all_sky_probes"):
                ex = dp.get("all_sky_examples") or []
                why.append(f"{dp['all_sky_probes']} probed frames rendered NO valid depth at all "
                           f"(all sky) - e.g. " + ", ".join(
                               f"frame {e.get('frame')} {e.get('phase')} pitch {e.get('pitch_deg')}"
                               for e in ex[:3]) +
                           ". The capture actor refuses to write a depth channel with no depth in "
                           f"it and ENDS THE RUN at such a frame, so the route must not contain one")
            if dp.get("thin_depth_probes"):
                ex = dp.get("thin_depth_examples") or []
                why.append(f"{dp['thin_depth_probes']} probed frames had under "
                           f"{(dp.get('min_valid_fraction_required') or 0.01)*100:.0f}% valid "
                           f"depth (thinnest " + ", ".join(
                               f"frame {e.get('frame')} {e.get('phase')} pitch {e.get('pitch_deg')} "
                               f"valid {e.get('valid_fraction')}" for e in ex[:2]) +
                           ") - the all-sky failure approaching")
            if not why:
                why.append(f"depth probe reported clear=False without a named reason: {dp}")
            refusal = ("refused_depth_probe", "depth probe veto: " + "; ".join(why))
        elif (frozen.get("head_clearance_pruning") or {}).get("kept_fraction", 1.0) < KEPT_MIN:
            # Pruning is allowed to remove roads the body cannot pass. It is NOT allowed to remove
            # the map. Hwaseong: 39 of 846 roads blocked - the gates - and 547 more dropped as
            # disconnected, leaving 22% of the centreline; the route then covered that island
            # perfectly and every gate downstream measured it against the island. 100% of 22% is
            # the plausible-wrong-data shape this repo exists to refuse. Checked BEFORE the
            # partial-cover gate, because that gate's denominator is the already-pruned network.
            h = frozen["head_clearance_pruning"]
            refusal = ("refused_disconnected_network",
                       f"head-clearance pruning kept {h['kept_fraction']*100:.0f}% of the centreline "
                       f"({h['centreline_kept_m']:.0f} of {h['centreline_before_m']:.0f} m): "
                       f"{h['roads_blocked']} roads blocked, {h['dropped_disconnected']} more cut "
                       f"off behind them, {h['components_after_pruning']} islands. A cover of the "
                       f"spawn's island is not a cover of the map. Raise body.ground_clearance_cm "
                       f"(6 cm cannot step over a sill; see longvideo/probe_matrix.py) rather than "
                       f"recording the island.")
        elif frozen["coverage"]["fraction"] < 1.0:
            cv = frozen["coverage"]
            refusal = ("refused_partial_cover",
                       f"the route only reaches {cv['roads_walked']} of {cv['roads_total']} "
                       f"roads. Recording a partial cover and calling it coverage is the "
                       f"failure mode worth avoiding.")
        if refusal:
            rec["result"], rec["refusal"] = refusal[0], refusal[1]
            st["attempts"].append(rec); st["done"] = refusal[0]; save(st)
            print(f"[lv:{SHARD_ID}] REFUSING to capture: {refusal[1]}", flush=True)
            return 0
        ep = cape.capture(frozen_path, ucv=ucv)
        # SKIP_GATES applies to fresh recordings too, not only to recovery. The instruction was
        # "once it is recorded, upload it" - and for one round that was honoured only on the
        # recovery path, so four freshly recorded episodes were filed under _rejected/ for an
        # action-mix band they could not meet on a 114 m network. Gates off means: package the
        # metadata, write acceptance.json with accepted=null (unjudged), land on the normal prefix.
        gates = os.environ.get("SKIP_GATES", "").strip().lower() not in ("1", "true", "yes")
        pk.build(ep, gates=gates)
        acc = json.loads((Path(ep) / "acceptance.json").read_text())
        su = json.loads((Path(ep) / "capture_summary.json").read_text())
        verdict = acc.get("accepted")
        rec.update(result=("recorded" if verdict is None else
                           "accepted" if verdict else "rejected"),
                   episode=Path(ep).name, frames=su["frames"],
                   hours=round(su["frames"] / su["fps"] / 3600, 3),
                   planned_frames=su.get("planned_frames"),
                   ended_early=su.get("ended_early_reason"),
                   gates_failed=[g["gate"] for g in acc["gates"] if g["result"] == "FAIL"],
                   took_hours=round((time.time() - t0) / 3600, 3))
        st["attempts"].append(rec)
        st["done"] = rec["result"]
        save(st)
        print(f"[lv:{SHARD_ID}] {rec['result']}: {rec['frames']} frames "
              f"({rec['hours']:.2f} h) in {rec['took_hours']:.2f} h"
              + (f"; failed gates {rec['gates_failed']}" if rec["gates_failed"] else ""),
              flush=True)
        return 0
    except Terminated as e:
        # The watchdog asked us to stop. The frames on disk are real; what is missing is only the
        # metadata that makes them an episode, and salvage() writes exactly that.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if frozen_path is None:
            # Killed during freeze: nothing to salvage, but SAY SO.
            rec.update(result="terminated_during_freeze", error=str(e),
                       took_hours=round((time.time() - t0) / 3600, 3))
            st["attempts"].append(rec); st["done"] = "terminated_during_freeze"; save(st)
            print(f"[lv:{SHARD_ID}] terminated during freeze after {rec['took_hours']:.2f} h",
                  flush=True)
            return 0
        info = salvage(frozen_path)
        rec.update(result="salvaged" if info.get("accepted") is not None else "salvage_failed",
                   error=str(e), salvage=info,
                   episode=info.get("episode"), frames=info.get("contiguous_frames"),
                   hours=info.get("hours"),
                   gates_failed=info.get("failed_gates") or [],
                   took_hours=round((time.time() - t0) / 3600, 3))
        st["attempts"].append(rec)
        st["done"] = rec["result"]
        save(st)
        print(f"[lv:{SHARD_ID}] {rec['result']}: {info}", flush=True)
        return 0
    except Exception as e:
        # The type, not just the message: a StopIteration has no message at all, and a fleet
        # machine reported `error: ""` for it.
        msg = f"{type(e).__name__}: {str(e)[:280]}"
        traceback.print_exc()
        alive = engine.query(ucv.client.request, "RESULT.update({'ok': True})", timeout=30)
        dead = (not alive.get("ok")) or "socket" in msg.lower()
        rec.update(result=("engine_lost" if dead else "failed"), error=msg,
                   took_hours=round((time.time() - t0) / 3600, 3))
        st["attempts"].append(rec)
        save(st)
        if dead:
            print(f"[lv:{SHARD_ID}] engine appears dead after {rec['took_hours']:.2f} h - exiting "
                  f"for relaunch; the next attempt will ask for what the clock allows",
                  flush=True)
            return 3
        st["done"] = "failed"
        save(st)
        print(f"[lv:{SHARD_ID}] FAILED {msg[:200]}", flush=True)
        return 0


FREEZE_TOD = """
import unreal as U
w = U.EditorLevelLibrary.get_game_world()
frozen = []
for a in U.GameplayStatics.get_all_actors_of_class(w, U.Actor):
    name = (a.get_class().get_name() + '|' + a.get_name()).lower()
    if any(k in name for k in ('sky', 'sun', 'timeofday', 'time_of_day', 'daynight',
                               'day_night', 'weather', 'ultra_dynamic')):
        try:
            a.set_actor_tick_enabled(False)
            frozen.append(name)
        except Exception:
            pass
raised = []
for dl in U.GameplayStatics.get_all_actors_of_class(w, U.DirectionalLight):
    rot = dl.get_actor_rotation()
    if rot.pitch > -40.0:
        dl.set_actor_rotation(U.Rotator(roll=rot.roll, pitch=-55.0, yaw=rot.yaw), False)
        raised.append('%s pitch %.1f -> -55' % (dl.get_name(), rot.pitch))
if raised:
    for a in U.GameplayStatics.get_all_actors_of_class(w, U.Actor):
        c = a.get_component_by_class(U.SkyLightComponent)
        if c is not None:
            try:
                c.recapture_sky()
            except Exception:
                pass
RESULT.update({'ok': True, 'frozen': len(frozen), 'sun_raised': raised})
"""

if __name__ == "__main__":
    sys.exit(main())
