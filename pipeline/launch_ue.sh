#!/usr/bin/env bash
# Start UE for capture, waiting until the UnrealCV port is genuinely free first.
#
# A relaunch issued too soon after the previous editor died inherits a socket still held by the
# dying process; UnrealCV then logs "Cannot start listening on port ..., Port might be in use"
# followed by "Failed to start network server", and the editor comes up with no control channel
# at all. It looks like a slow startup and wastes the whole 15-minute load.
set -uo pipefail
PORT=${UNREALCV_PORT:-9208}
MAP=${GAME_MAP:?GAME_MAP is required and must match what the task JSON asks for}
LOG=${LOG:-/home/ubuntu/WM-Unreal-data-collection/local_run/ue_nr.log}

echo "[launch] waiting for port $PORT to be free"
for i in $(seq 1 120); do
  ss -tan 2>/dev/null | grep -q ":$PORT " || break
  sleep 2
done
if ss -tan 2>/dev/null | grep -q ":$PORT "; then
  echo "[launch] port $PORT is still held after 240s:"
  ss -tanp 2>/dev/null | grep ":$PORT "
  exit 1
fi

nohup enroot exec "${CTR_PID:-61703}" bash -lc \
  "GAME_MAP=$MAP UNREALCV_PORT=$PORT bash /home/ubuntu/WM-Unreal-data-collection/local_run/launch_ue_fast.sh" \
  > "$LOG" 2>&1 &
echo "[launch] started, log $LOG"

for i in $(seq 1 90); do
  if ss -tln 2>/dev/null | grep -q ":$PORT "; then
    echo "[launch] UnrealCV listening on $PORT after $((i*10))s"
    exit 0
  fi
  # a failed bind is fatal and known immediately - do not wait out the full timeout for it
  if enroot exec "${CTR_PID:-61703}" bash -lc \
      'grep -aq "Failed to start network server" /home/ue4/simworld/Saved/Logs/gym_citynav.log' \
      2>/dev/null; then
    echo "[launch] the editor failed to bind $PORT; kill it and retry"
    exit 1
  fi
  sleep 10
done
echo "[launch] no listener on $PORT after 900s"
exit 1
