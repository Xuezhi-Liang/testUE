#!/usr/bin/env bash
# Restart the editor and run one diagnostic script against it. Same readiness rule as
# smoke_once.sh: the start hook's own success line.
set -uo pipefail
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
CTR=${CTR_PID:?}; MAP=${GAME_MAP:?}; SCRIPT=${SCRIPT:?}; TAG=${TAG:-probe}
UELOG=$P/logs/lv_${TAG}_ue.log; OUT=$P/logs/lv_${TAG}_out.log
OLD=$(pgrep -f "[U]nrealEditor.*gym_citynav" | head -1)
if [ -n "$OLD" ]; then
  kill "$OLD" 2>/dev/null
  for i in $(seq 1 60); do ps -p "$OLD" >/dev/null 2>&1 || break; sleep 2; done
  ps -p "$OLD" >/dev/null 2>&1 && { kill -9 "$OLD"; sleep 5; }
fi
CTR_PID=$CTR GAME_MAP=$MAP LOG=$UELOG bash "$P/launch_ue.sh" || exit 1
for i in $(seq 1 120); do
  grep -aq "Called editor_play_simulate()" "$UELOG" && break
  pgrep -f "[U]nrealEditor.*gym_citynav" >/dev/null || { echo "editor died"; exit 1; }
  sleep 5
done
enroot exec "$CTR" bash -lc "cd $P && MAP=$MAP SLUG=${SLUG:-} SEED=${SEED:-2000} PF_CAP_S=${PF_CAP_S:-7200} FROZEN=${FROZEN:-} N=${N:-60} CLEARANCES=${CLEARANCES:-6,15,25,40,60} GROUND_CLEARANCE_CM=${GROUND_CLEARANCE_CM:-} COMBOS=${COMBOS:-} VETO_RADIUS_CM=${VETO_RADIUS_CM:-} MAX_REROUTE_ATTEMPTS=${MAX_REROUTE_ATTEMPTS:-} PORT=9208 UE_LOG=/home/ue4/simworld/Saved/Logs/gym_citynav.log python3 -u $SCRIPT" > "$OUT" 2>&1
echo "rc=$? -> $OUT"
