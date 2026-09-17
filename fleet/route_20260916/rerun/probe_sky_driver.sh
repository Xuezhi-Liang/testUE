#!/bin/bash
# One editor per map, serially: launch, wait for PIE, probe, stop. Never a second client.
D=/home/ubuntu/ue_route_fleet_20260916/rerun; L=/home/ubuntu/WM-Unreal-data-collection/local_run/launch_ue_fast.sh
NATIVE=/proc/22964/root/home/ue4/simworld/Saved/Logs/gym_citynav.log
for slug in "$@"; do
  map=$(python3 -c "import json;print(next(m['map_id'] for m in json.load(open('/home/ubuntu/ue_route_validation_20260916/manifest.json')) if m['slug']=='$slug'))")
  while pgrep -x UnrealEditor >/dev/null; do sleep 5; done
  t0=$(date +%s); echo "$(date -u +%H:%M:%S) START $slug $map"
  setsid enroot exec 22964 env GAME_MAP="$map" UNREALCV_PORT=9208 bash $L > "$D/sky_editor_$slug.log" 2>&1 < /dev/null &
  EPID=$!
  ready=0
  for i in $(seq 1 300); do
    sleep 2
    if grep -q 'Start listening on port 9208' "$D/sky_editor_$slug.log" 2>/dev/null && grep -q 'PIE: Play in editor total start time' "$D/sky_editor_$slug.log" "$NATIVE" 2>/dev/null; then ready=1; break; fi
    kill -0 $EPID 2>/dev/null || break
  done
  if [ $ready = 1 ]; then
    sleep 3; echo "$(date -u +%H:%M:%S) ready after $(( $(date +%s)-t0 ))s"
    timeout -k 20 600 enroot exec 22964 env PYTHONUNBUFFERED=1 OPENCV_IO_ENABLE_OPENEXR=1 python3 $D/probe_sky.py "$slug" 2>&1 | tail -30
  else echo "$(date -u +%H:%M:%S) editor never became ready for $slug"; fi
  pkill -x UnrealEditor; sleep 5; pkill -9 -x UnrealEditor 2>/dev/null; sleep 5
  echo "$(date -u +%H:%M:%S) END $slug"
done
echo ALL_DONE
