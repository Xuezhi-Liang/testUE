#!/usr/bin/env bash
# Long-video driver for one map: keep one editor alive for one 20 h episode, relaunch on engine
# loss, and watchdog the runner.
#
#   SLUG=... MAP_ID=... DEADLINE_EPOCH=... CTR_PID=... bash driver.sh
#
# Two things differ from campaign/driver.sh, both because the episode is 20 h and not 60 s:
#   - Attempts are few and the clock is the limit, not a count. Each attempt asks the runner for
#     what the wall clock allows; when nothing useful fits, the runner says so and exits 0.
#   - The silence watchdog is 45 min, not 20. capture prints progress every couple of seconds,
#     but freeze does not: planning 1.7 M poses, pruning roads with engine capsule sweeps and
#     validating every frame against collision geometry all run without output, and on the
#     biggest core that is far longer than 20 minutes.
set -uo pipefail
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
L=$P/longvideo
LOGD=$P/logs
CTR=${CTR_PID:?CTR_PID required}
SHARD_ID=${SHARD_ID:?} ; SLUG=${SLUG:-${SHARD_ID%%__s*}} ; MAP_ID=${MAP_ID:?}
DEADLINE_EPOCH=${DEADLINE_EPOCH:?}
SILENCE_S=${SILENCE_S:-2700}
RLOG=$LOGD/lv_${SHARD_ID}_runner.log
mkdir -p "$LOGD"

for attempt in $(seq 1 4); do
  now=$(date +%s)
  left=$(( DEADLINE_EPOCH - now ))
  if [ "$left" -lt 7200 ]; then
    echo "[lv-driver] only $((left/60)) min of wall clock left - stopping"
    break
  fi
  done_state=$(python3 - <<PY
import json
from pathlib import Path
p = Path("$L/state_${SHARD_ID}.json")
print((json.loads(p.read_text()).get("done") or "") if p.exists() else "")
PY
)
  case "$done_state" in
    accepted|rejected|failed|refused_*|out_of_time)
      echo "[lv-driver] runner reports '$done_state' - done"; break;;
  esac

  OLD=$(pgrep -f "[U]nrealEditor.*gym_citynav" | head -1)
  if [[ -n "$OLD" ]]; then
    kill "$OLD" 2>/dev/null
    for i in $(seq 1 60); do ps -p "$OLD" >/dev/null 2>&1 || break; sleep 2; done
    ps -p "$OLD" >/dev/null 2>&1 && { kill -9 "$OLD"; sleep 5; }
  fi
  echo "[lv-driver] attempt $attempt: launching editor on $MAP_ID ($((left/3600))h left)"
  if ! GAME_MAP=$MAP_ID LOG=$LOGD/lv_${SHARD_ID}_ue_$attempt.log bash "$P/launch_ue.sh"; then
    echo "[lv-driver] editor failed to start"; sleep 30; continue
  fi

  touch "$RLOG"
  # The watchdog measures the FRAME COUNTER, not the log's mtime, and it asks for a clean exit
  # before it forces one.
  #
  # Both of those were wrong before, and together they cost thirteen episodes in one run. The
  # capture's progress line was printed only when the frame counter moved, so a wedged in-engine
  # capture produced no output at all; the watchdog saw an unchanging mtime, called it silence at
  # 2923 s, and sent SIGKILL. `kill -9` runs no handler, and capture_engine writes the files that
  # make frames an episode only after the last frame - so hours of good frames were left on disk
  # with no frames.csv, and uploader.sh, which waits for acceptance.json, never looked at them.
  #
  # Now: capture_engine heartbeats every 30 s whether or not the counter moved and labels a stall
  # as a stall, so this can read the counter out of the log. A counter that has not advanced is
  # the actual fault condition. On finding one, SIGTERM first - runner.py finalises what it has,
  # which turns the kill into a short episode instead of orphaned frames - and escalate to
  # SIGKILL only if it is still there 180 s later.
  (
    last_frame=""; last_move=$(date +%s)
    while sleep 60; do
      pgrep -f "longvideo/runner.py" >/dev/null || break
      f=$(grep -o '^\[capture\] [0-9]*/' "$RLOG" 2>/dev/null | tail -1 | tr -dc '0-9')
      now=$(date +%s)
      if [ -n "$f" ] && [ "$f" != "$last_frame" ]; then
        last_frame=$f; last_move=$now
      fi
      # No heartbeat at all is its own fault: the log's mtime still guards against a runner that
      # has stopped writing entirely (a hung UnrealCV request, before any frame is captured).
      quiet=$(( now - $(stat -c %Y "$RLOG") ))
      # The stall clock runs only once a frame counter EXISTS. Before the first `[capture] N/`
      # line the runner is in freeze - nav export, network, pruning sweeps, reroutes, the mix
      # tuner - and on MedievalCastle (3,066 roads, 1.7 km of centreline, two reroute attempts)
      # that takes longer than 45 minutes. The first version of this check counted from boot,
      # read "no counter" as "counter stuck", and killed all eight Castle shards mid-freeze,
      # twice each. Freeze prints constantly, so the mtime check below still guards a true hang.
      if [ -n "$last_frame" ]; then stalled=$(( now - last_move )); else stalled=0; fi
      if [ "$stalled" -gt "$SILENCE_S" ] || [ "$quiet" -gt "$SILENCE_S" ]; then
        if [ "$stalled" -gt "$SILENCE_S" ]; then
          echo "[lv-watchdog] frame counter stuck at ${last_frame:-none} for ${stalled}s" >> "$RLOG"
        else
          echo "[lv-watchdog] no output at all for ${quiet}s" >> "$RLOG"
        fi
        echo "[lv-watchdog] SIGTERM to runner - it should package what it has" >> "$RLOG"
        pkill -TERM -f "longvideo/runner.py"
        for i in $(seq 1 36); do
          pgrep -f "longvideo/runner.py" >/dev/null || break
          sleep 5
        done
        if pgrep -f "longvideo/runner.py" >/dev/null; then
          echo "[lv-watchdog] still alive after 180s - SIGKILL" >> "$RLOG"
          pkill -9 -f "longvideo/runner.py"
        fi
        break
      fi
    done
  ) &
  WD=$!
  enroot exec "$CTR" bash -lc \
    "PORT=9208 SHARD_ID=$SHARD_ID SLUG=$SLUG MAP_ID=$MAP_ID DEADLINE_EPOCH=$DEADLINE_EPOCH \
     SKIP_GATES=${SKIP_GATES:-} \
     UE_LOG=/home/ue4/simworld/Saved/Logs/gym_citynav.log \
     python3 -u $L/runner.py" >>"$RLOG" 2>&1
  rc=$?
  kill $WD 2>/dev/null
  echo "[lv-driver] runner exited rc=$rc (attempt $attempt)"
  [[ $rc -eq 0 ]] && break
  sleep 20
done
echo "[lv-driver] finished"
