#!/usr/bin/env bash
# Per-instance boot for the route-measurement queue. Container up -> UnrealCV colour map patched and
# rebuilt -> our capture module rebuilt -> pull tasks until the queue is empty -> shut down.
set -u
W=$1
F=/home/ubuntu/ue_newroute_fleet_20260916
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
CONTENT=/home/ubuntu/prj/SimWorld/simworld_100maps_1/Content
UCV=/home/ubuntu/datasets/SimWorld/.local/share/enroot/ue_ps_run/home/ue4/simworld/Plugins/unrealcv/Source/UnrealCV/Private/Controller/ObjectAnnotator.cpp
CTRROOT=/home/ubuntu/datasets/SimWorld/.local/share/enroot/ue_ps_run
S3=s3://pan-simworld/ue-newroute/20260916/workers/$W
export AWS_DEFAULT_REGION=eu-north-1
# enroot defaults its runtime path to /run/user/$UID, which does not exist on a fresh boot when
# this runs from cloud-init rather than a login session: the container starts but every later
# `enroot exec` dies with "mkdir: cannot create /run/user/1000: Permission denied". Export it
# before start_enroot_container.sh so the container and every exec into it agree on one path.
export ENROOT_RUNTIME_PATH="$F/enroot-runtime"
mkdir -p "$ENROOT_RUNTIME_PATH"
exec >> "$F/boot.log" 2>&1
echo "==== boot worker $W $(date -u) ===="
upload_logs() { aws s3 cp "$F/boot.log" "$S3/boot.log" --only-show-errors 2>/dev/null || true
                [ -f "$F/module_build.log" ] && aws s3 cp "$F/module_build.log" "$S3/module_build.log" --only-show-errors 2>/dev/null || true; }
fail() { echo "BOOT FAILED: $*"; upload_logs; sudo shutdown -h +2 "route boot failed"; exit 1; }

# The image's pipeline predates this work; replace it wholesale. Content, project and DDC untouched.
tar czf "$F/prior_pipeline_sources.tar.gz" -C "$P" --wildcards '*.py' cpp 2>/dev/null || true
cp -a "$F/bundle/pipeline/." "$P/" || fail "pipeline copy"
cp "$F/bundle/launch_ue_fast.sh" /home/ubuntu/WM-Unreal-data-collection/local_run/launch_ue_fast.sh || fail "launcher copy"
mkdir -p "$CONTENT" || fail "content dir"

for i in $(seq 1 30); do nvidia-smi -L >/dev/null 2>&1 && break; sleep 10; done
nvidia-smi -L || fail "no GPU"

nohup bash /home/ubuntu/prj/SimWorld/launch_unreal_instance/start_enroot_container.sh "exec sleep 999999999" \
  > "$F/container_boot.log" 2>&1 &
# The parked pid must be the CONTAINERISED sleep: the host-side wrapper's command line contains the
# same marker string, and picking it gives an enroot exec that lands nowhere.
CTR_PID=""
for i in $(seq 1 90); do
  for cand in $(pgrep -f 'sleep 999999999'); do
    if [ "$(cat /proc/$cand/comm 2>/dev/null)" = sleep ] && grep -q '^Seccomp:.*2' /proc/$cand/status 2>/dev/null; then CTR_PID=$cand; break; fi
  done
  [ -n "$CTR_PID" ] && break
  sleep 5
done
[ -n "$CTR_PID" ] || fail "container never started"
export CTR_PID
echo "container pid $CTR_PID"
sleep 10

# ---- UnrealCV colour map: patch + rebuild on EVERY boot -------------------------------------
# The fix lives in the container rootfs, not in any git repo, so a fresh instance from the image
# starts unpatched and a big level (Dubai: 147393 actors vs a 32768-entry map) aborts the editor at
# BeginPlay. Patch is idempotent; the source is touched so UBT cannot decide it is up to date.
[ -f "$UCV" ] || fail "UnrealCV source not found at $UCV"
python3 "$F/bundle/patch_unrealcv.py" "$UCV" || fail "colour map patch did not apply cleanly"
grep -q SafeIndex "$UCV" || fail "patch verification failed"
touch "$UCV"
PATCH_EPOCH=$(date +%s)

echo "==== $(date -u +%H:%M:%S) installing capture module + building editor target ===="
enroot exec "$CTR_PID" python3 "$P/cpp/install.py" > "$F/module_build.log" 2>&1 || fail "cpp/install.py"
enroot exec "$CTR_PID" /home/ue4/UnrealEngine/Engine/Build/BatchFiles/Linux/Build.sh \
  gym_citynavEditor Linux Development -project=/home/ue4/simworld/gym_citynav.uproject \
  -waitmutex -MaxParallelActions=4 >> "$F/module_build.log" 2>&1
grep -q 'Result: Succeeded' "$F/module_build.log" || fail "editor build did not report success"
# A zero exit and a green log are not proof the patched module was rebuilt: if mtimes made UBT skip
# it, the .so still carries the old colour map. Judge by the artefact.
# The plugin links into Plugins/unrealcv/Binaries, not the project's own Binaries; ask for any
# UnrealCV .so newer than the patch rather than naming a directory that can move.
SO=$(find "$CTRROOT" -name 'libUnrealEditor-UnrealCV.so' -newermt "@$PATCH_EPOCH" 2>/dev/null | head -1)
[ -n "$SO" ] || fail "no UnrealCV .so newer than the patch; the module was not rebuilt"
echo "UnrealCV rebuilt: $SO $(date -u -r "$SO" +%H:%M:%S)"
grep -m1 'Total execution time' "$F/module_build.log"
upload_logs

echo "==== $(date -u +%H:%M:%S) joining the task queue ===="
python3 -u "$F/bundle/worker_loop.py" "$W" >> "$F/worker_loop.log" 2>&1
rc=$?
aws s3 cp "$F/worker_loop.log" "$S3/worker_loop.log" --only-show-errors || true
upload_logs
echo "worker_loop exited rc=$rc"
sudo shutdown -h +1 "route measurement done"
