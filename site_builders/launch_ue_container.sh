#!/usr/bin/env bash
# Start UE for capture in a fresh enroot container instance, and wait until UnrealCV really answers.
#
# launch_ue.sh assumes a long-lived container to `enroot exec` into (CTR_PID, default 61703). After
# a host reboot there is no such container, so this creates a short-lived one - and it has to
# reproduce the mounts the original launch used, because two of them are not optional:
#
#   tools   -> /home/ue4/tools    the editor is started with --ExecutePythonScript=.../start_hook.py,
#                                 and EditorPythonExecuter QUITS THE EDITOR when that script cannot
#                                 be read. The container's own /home/ue4/tools is an empty directory
#                                 with mode 000, so without this mount the editor binds the UnrealCV
#                                 port, fails to open the hook, and exits - which from outside looks
#                                 like a successful start followed by "connection refused".
#   Content -> simworld/Content   the licensed asset packs live on the host, not in the image. The
#                                 container's own Content directory is empty, so every map is
#                                 missing without this.
#
# Readiness is read out of the LOG, and no socket is ever opened to test it. UnrealCV spawns a
# server thread per connection and this build dies on the next request after a client disconnects,
# so a probe that connects and closes is not a health check - it is the documented way to kill the
# editor. A three-connect handshake probe here did exactly that: the log showed the second thread
# with `Socket: NULL`, then SE_ECONNRESET, then the process was gone.
#
# `ss -tln` is passive and safe, but a listening port alone is not readiness either: the port is
# bound early in startup and stays bound for a moment after the editor decides to quit. So the wait
# is for the start-up hook to report success, with the port present and the process alive.
set -uo pipefail

export ENROOT_DATA_PATH="${ENROOT_DATA_PATH:-/home/ubuntu/datasets/SimWorld/.local/share/enroot}"
export ENROOT_RUNTIME_PATH="${ENROOT_RUNTIME_PATH:-/tmp/enroot-runtime-ue}"
CONTAINER="${CONTAINER:-ue_ps_run}"
REPO=/home/ubuntu/WM-Unreal-data-collection
SIMWORLD_SRC="${SIMWORLD_SRC:-/home/ubuntu/prj/SimWorld/simworld_100maps_1}"
CONTENT_HOST_DIR="${CONTENT_HOST_DIR:-$SIMWORLD_SRC/Content}"
TOOLS_DIR="${TOOLS_DIR:-/home/ubuntu/prj/SimWorld/launch_unreal_instance/tools}"
PROJ_DIR="${PROJ_DIR:-/home/ue4/simworld}"
PORT="${UNREALCV_PORT:-9208}"
MAP="${GAME_MAP:?GAME_MAP is required and must match what the task JSON asks for}"
LOG="${LOG:-$REPO/local_run/ue_coverage.log}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-1800}"

for d in "$CONTENT_HOST_DIR" "$TOOLS_DIR"; do
  [ -d "$d" ] || { echo "[launch] required mount source missing: $d"; exit 1; }
done
[ -r "$TOOLS_DIR/start_hook.py" ] || { echo "[launch] $TOOLS_DIR/start_hook.py is not readable"; exit 1; }
mkdir -p "$ENROOT_RUNTIME_PATH"

if pgrep -f "[U]nrealEditor.*gym_citynav" >/dev/null; then
  echo "[launch] an editor is already running: $(pgrep -f '[U]nrealEditor.*gym_citynav' | tr '\n' ' ')"
  echo "[launch] stop it first - two editors on one host deadlock on the shared Saved/, Intermediate/ and DDC"
  exit 1
fi

echo "[launch] waiting for port $PORT to be free"
for _ in $(seq 1 120); do
  ss -tan 2>/dev/null | grep -q ":$PORT " || break
  sleep 2
done
if ss -tan 2>/dev/null | grep -q ":$PORT "; then
  echo "[launch] port $PORT is still held after 240s:"; ss -tanp 2>/dev/null | grep ":$PORT "; exit 1
fi

: > "$LOG"
setsid enroot start --rw \
  --mount "$REPO:$REPO:x-create=dir" \
  --mount "$TOOLS_DIR:/home/ue4/tools" \
  --mount "$CONTENT_HOST_DIR:$PROJ_DIR/Content" \
  "$CONTAINER" \
  bash -lc "GAME_MAP=$MAP UNREALCV_PORT=$PORT bash $REPO/local_run/launch_ue_fast.sh" \
  </dev/null >>"$LOG" 2>&1 &
echo "[launch] started, map $MAP, log $LOG"

ready=0
for i in $(seq 1 $((BOOT_TIMEOUT / 5))); do
  # Fatal conditions are known immediately; do not wait out the whole timeout for them.
  if grep -aq "Failed to start network server" "$LOG" 2>/dev/null; then
    echo "[launch] the editor failed to bind $PORT"; tail -20 "$LOG"; exit 1
  fi
  if grep -aqE "Could not load Python file|Python script executed with errors" "$LOG" 2>/dev/null; then
    echo "[launch] the start-up hook failed, so the editor will quit. Offending lines:"
    grep -aE "Could not load Python file|failed: errno|Python script executed with errors" "$LOG" | tail -5
    exit 1
  fi
  if ! pgrep -f "[U]nrealEditor.*gym_citynav" >/dev/null && [ "$i" -gt 4 ]; then
    echo "[launch] the editor process is gone; last 25 log lines:"; tail -25 "$LOG"; exit 1
  fi
  # Passive readiness only: the hook's own success line, the listening port, and a live process.
  if grep -aq "editor_play_simulate()" "$LOG" 2>/dev/null \
     && ss -tln 2>/dev/null | grep -q ":$PORT " \
     && pgrep -f "[U]nrealEditor.*gym_citynav" >/dev/null; then
    ready=$((ready + 1))
    if [ "$ready" -ge 2 ]; then
      echo "[launch] start hook ran, UnrealCV listening on $PORT, editor alive after $((i * 5))s"
      exit 0
    fi
  else
    ready=0
  fi
  sleep 5
done
echo "[launch] no working UnrealCV on $PORT after ${BOOT_TIMEOUT}s"
tail -25 "$LOG"
exit 1
