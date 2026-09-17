#!/usr/bin/env bash
# Run one task per map, sequentially, restarting the editor for each.
#
# One Unreal process at a time, deliberately. Two on the same host deadlock - GPU at 0%, load
# average 0.04, the log stopping right after "LogDerivedDataCache: Maintenance finished" - because
# they share Saved/, Intermediate/ and the DDC. Parallel collection needs per-worker project
# copies before it can be attempted at all.
#
# Every task's full output goes to its own log. An earlier version of this pattern piped straight
# into grep and lost a Python traceback entirely: no directory, no message, nothing to debug.
set -uo pipefail
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
LOGD=$P/logs
mkdir -p "$LOGD"
CTR=${CTR_PID:-61703}

TASKS=("$@")
if [[ ${#TASKS[@]} -eq 0 ]]; then
  TASKS=(batch5_downtown_west batch5_tokyo batch5_medieval_town batch5_neighborhood
         batch5_winter_town)
fi

declare -A RESULT
START_ALL=$(date +%s)

for t in "${TASKS[@]}"; do
  TASK_JSON=$P/tasks/$t.json
  [[ -f $TASK_JSON ]] || { echo "!! no such task: $TASK_JSON"; RESULT[$t]="no task file"; continue; }
  MAP=$(python3 -c "import json;print(json.load(open('$TASK_JSON'))['map_id'])")
  LOG=$LOGD/$t.log
  echo "=================================================================="
  echo "[$t] map $MAP"
  echo "[$t] log ${LOG#$P/}"
  T0=$(date +%s)

  # Stop whatever editor is running first. launch_ue.sh only WAITS for the port to be free - it
  # does not free it - so without this every task waits out the 240 s port timeout and reports
  # "editor did not start". That is exactly how the first attempt at this batch produced five
  # failures and no data in eleven minutes.
  OLD=$(pgrep -f "[U]nrealEditor.*gym_citynav" | head -1)
  if [[ -n "$OLD" ]]; then
    echo "[$t] stopping the running editor (pid $OLD)" | tee -a "$LOG"
    kill "$OLD" 2>/dev/null
    for i in $(seq 1 60); do ps -p "$OLD" >/dev/null 2>&1 || break; sleep 2; done
    ps -p "$OLD" >/dev/null 2>&1 && { echo "[$t] SIGKILL $OLD" | tee -a "$LOG"; kill -9 "$OLD"; sleep 5; }
  fi

  # The editor is restarted per map: a map switch in a live editor keeps the previous level's
  # streaming state and the previous navmesh, and our synthesised bounds volume would follow it.
  if ! GAME_MAP="$MAP" LOG=$LOGD/ue_$t.log bash "$P/launch_ue.sh" >>"$LOG" 2>&1; then
    echo "[$t] FAILED: the editor never came up (see ${LOG#$P/} and ue_$t.log)"
    RESULT[$t]="editor did not start"
    continue
  fi

  # UE_LOG lets the packager name the assets behind a broken render. ModularNeighborhood's foliage
  # master material fails to compile, every tree in it draws as absolute black, and every gate
  # still passed - the editor log was the only place that said why.
  if enroot exec "$CTR" bash -lc \
      "PORT=9208 UE_LOG=$LOGD/ue_$t.log python3 -u $P/run_task.py $TASK_JSON --skip-qa" \
      >>"$LOG" 2>&1; then
    EP=$(grep -aoE "run\] done in [0-9]+s -> .*" "$LOG" | tail -1 | sed 's/.*-> //')
    FR=$(grep -aoE "frames in [0-9]+s \([0-9.]+ fps" "$LOG" | tail -1)
    GATES=$(grep -aoE "accepted=(True|False)  passed=[0-9]+ failed=[0-9]+ skipped=[0-9]+" "$LOG" | tail -1)
    RESULT[$t]="${GATES:-no gate line}  ${FR:-}"
    echo "[$t] $((($(date +%s)-T0)/60)) min  $GATES  $FR"
  else
    # A refusal is a specific, expected outcome and must not be reported as a crash.
    if grep -aq "REFUSING" "$LOG"; then
      RESULT[$t]="REFUSED: $(grep -aoE 'REFUSING to capture: [^.]*' "$LOG" | tail -1 | cut -c1-90)"
    else
      RESULT[$t]="FAILED: $(grep -aE 'Error|Traceback|RuntimeError' "$LOG" | tail -1 | cut -c1-90)"
    fi
    echo "[$t] ${RESULT[$t]}"
  fi
done

echo
echo "=================================================================="
echo "batch finished in $(( ($(date +%s)-START_ALL)/60 )) min"
for t in "${TASKS[@]}"; do printf "  %-26s %s\n" "$t" "${RESULT[$t]:-not run}"; done
