#!/usr/bin/env bash
# Freeze-only pre-flight over every long-video map: one editor per map, restarted between them.
#
#   CTR_PID=<pid> bash preflight_all.sh
#
# One editor at a time and restarted per map, because two on one host deadlock on shared Saved/,
# Intermediate/ and DDC, and a map switch in a live editor keeps the previous level's streaming
# state and navmesh. Readiness is the start hook's own success line, not the port.
set -uo pipefail
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
L=$P/longvideo
CTR=${CTR_PID:?CTR_PID required}
LIST=$P/longvideo_preflight_list.json
OUT=$P/_longvideo_preflight.jsonl
: > "$OUT"

n=$(python3 -c "import json;print(len(json.load(open('$LIST'))))")
for i in $(seq 0 $((n-1))); do
  read -r SLUG MAP SEED <<< "$(python3 -c "
import json
r = json.load(open('$LIST'))[$i]
print(r['slug'], r['map_id'], r['seed'])")"
  echo "=== [$((i+1))/$n] $SLUG  seed $SEED ==="
  UELOG=$P/logs/pf_${SLUG}_ue.log
  RUNLOG=$P/logs/pf_${SLUG}_run.log

  OLD=$(pgrep -f "[U]nrealEditor.*gym_citynav" | head -1)
  if [ -n "$OLD" ]; then
    kill "$OLD" 2>/dev/null
    for k in $(seq 1 60); do ps -p "$OLD" >/dev/null 2>&1 || break; sleep 2; done
    ps -p "$OLD" >/dev/null 2>&1 && { kill -9 "$OLD"; sleep 5; }
  fi

  if ! CTR_PID=$CTR GAME_MAP=$MAP LOG=$UELOG bash "$P/launch_ue.sh"; then
    echo "PREFLIGHT {\"slug\": \"$SLUG\", \"verdict\": \"NO_EDITOR\", \"error\": \"launch_ue.sh failed\"}" | tee -a "$OUT"
    continue
  fi
  ready=0
  for k in $(seq 1 120); do
    grep -aq "Called editor_play_simulate()" "$UELOG" && { ready=1; break; }
    pgrep -f "[U]nrealEditor.*gym_citynav" >/dev/null || break
    sleep 5
  done
  if [ "$ready" -ne 1 ]; then
    echo "PREFLIGHT {\"slug\": \"$SLUG\", \"verdict\": \"NO_EDITOR\", \"error\": \"start hook never reported ready\"}" | tee -a "$OUT"
    continue
  fi

  enroot exec "$CTR" bash -lc \
    "cd $P && MAP=$MAP SLUG=$SLUG SEED=$SEED PORT=9208 \
     UE_LOG=/home/ue4/simworld/Saved/Logs/gym_citynav.log \
     python3 -u $L/preflight_one.py" > "$RUNLOG" 2>&1
  line=$(grep -a "^PREFLIGHT " "$RUNLOG" | tail -1)
  if [ -z "$line" ]; then
    line="PREFLIGHT {\"slug\": \"$SLUG\", \"verdict\": \"CRASHED\", \"error\": \"no verdict line; see $RUNLOG\"}"
  fi
  echo "$line" | sed 's/^PREFLIGHT //' >> "$OUT"
  echo "$line"
done
echo "=== pre-flight done -> $OUT ==="
