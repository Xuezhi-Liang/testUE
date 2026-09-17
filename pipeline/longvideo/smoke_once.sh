#!/usr/bin/env bash
# kill the editor -> relaunch -> wait for the start hook's own success line -> run one task.
#
# Readiness is the hook's line, not the port and not a LoadMap message. The port binds ~70 s into
# a startup that is not finished, and the world comes up as UEDPIE_0_<map> - grepping for
# "Bringing world" with a lower-case w matches nothing, which cost 15 minutes of waiting for an
# editor that had been ready the whole time.
set -uo pipefail
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
CTR=${CTR_PID:?CTR_PID required}
MAP=${GAME_MAP:?}
TASK=${TASK:?}
TAG=${TAG:-smoke}
UELOG=$P/logs/lv_${TAG}_ue.log
RUNLOG=$P/logs/lv_${TAG}_run.log

OLD=$(pgrep -f "[U]nrealEditor.*gym_citynav" | head -1)
if [ -n "$OLD" ]; then
  echo "[smoke] killing editor $OLD"
  kill "$OLD" 2>/dev/null
  for i in $(seq 1 60); do ps -p "$OLD" >/dev/null 2>&1 || break; sleep 2; done
  ps -p "$OLD" >/dev/null 2>&1 && { kill -9 "$OLD"; sleep 5; }
fi

echo "[smoke] launching editor on $MAP"
CTR_PID=$CTR GAME_MAP=$MAP LOG=$UELOG bash "$P/launch_ue.sh" || { echo "[smoke] launch failed"; exit 1; }

echo "[smoke] waiting for the start hook to report simulate mode"
for i in $(seq 1 120); do
  grep -aq "Called editor_play_simulate()" "$UELOG" && { echo "[smoke] hook ready after $((i*5))s"; break; }
  pgrep -f "[U]nrealEditor.*gym_citynav" >/dev/null || { echo "[smoke] editor died during startup"; exit 1; }
  sleep 5
done
grep -aq "Called editor_play_simulate()" "$UELOG" || { echo "[smoke] hook never reported ready"; exit 1; }

echo "[smoke] running $TASK"
enroot exec "$CTR" bash -lc \
  "cd $P && PORT=9208 UE_LOG=/home/ue4/simworld/Saved/Logs/gym_citynav.log python3 -u run_coverage.py $TASK" \
  > "$RUNLOG" 2>&1
echo "[smoke] run_coverage rc=$?"
