#!/usr/bin/env bash
# Recording worker boot (spec v1, 17 Sep): container up -> UnrealCV colour map patched and rebuilt ->
# capture module rebuilt -> pull recording shards from the S3 queue until empty -> shut down.
set -u
W=$1
F=/home/ubuntu/ue_record_fleet_20260918
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
SP=/home/ubuntu/WM-Unreal-data-collection/batch_inference/start_positions
UCV=/home/ubuntu/datasets/SimWorld/.local/share/enroot/ue_ps_run/home/ue4/simworld/Plugins/unrealcv/Source/UnrealCV/Private/Controller/ObjectAnnotator.cpp
CTRROOT=/home/ubuntu/datasets/SimWorld/.local/share/enroot/ue_ps_run
S3=s3://pan-simworld/ue-record/260918/workers/$W
export AWS_DEFAULT_REGION=eu-north-1
export ENROOT_RUNTIME_PATH="$F/enroot-runtime"; mkdir -p "$ENROOT_RUNTIME_PATH"
exec >> "$F/boot.log" 2>&1
echo "==== boot record worker $W $(date -u) ===="
upload_logs() { aws s3 cp "$F/boot.log" "$S3/boot.log" --only-show-errors 2>/dev/null || true; [ -f "$F/module_build.log" ] && aws s3 cp "$F/module_build.log" "$S3/module_build.log" --only-show-errors 2>/dev/null || true; }
fail() { echo "BOOT FAILED: $*"; upload_logs; sudo shutdown -h +2 "record boot failed"; exit 1; }
tar czf "$F/prior_pipeline_sources.tar.gz" -C "$P" --wildcards '*.py' cpp longvideo 2>/dev/null || true
cp -a "$F/bundle/pipeline/." "$P/" || fail "pipeline copy"
cp "$F/bundle/launch_ue_fast.sh" /home/ubuntu/WM-Unreal-data-collection/local_run/launch_ue_fast.sh || fail "launcher copy"
mkdir -p "$SP" "$P/logs" "$P/episodes" "$P/frozen"
cp "$F"/bundle/starts/*.json "$SP/"   # overwrite: the image ships empty placeholder stubs (name "", 0,0,0) for maps it never ran
for i in $(seq 1 30); do nvidia-smi -L >/dev/null 2>&1 && break; sleep 10; done
nvidia-smi -L || fail "no GPU"
nohup bash /home/ubuntu/prj/SimWorld/launch_unreal_instance/start_enroot_container.sh "exec sleep 999999999" > "$F/container_boot.log" 2>&1 &
CTR_PID=""
for i in $(seq 1 90); do
  for cand in $(pgrep -f 'sleep 999999999'); do
    if [ "$(cat /proc/$cand/comm 2>/dev/null)" = sleep ] && grep -q '^Seccomp:.*2' /proc/$cand/status 2>/dev/null; then CTR_PID=$cand; break; fi
  done
  [ -n "$CTR_PID" ] && break; sleep 5
done
[ -n "$CTR_PID" ] || fail "container never started"
export CTR_PID; echo "container pid $CTR_PID"; sleep 10
[ -f "$UCV" ] || fail "UnrealCV source not found"
python3 "$F/bundle/patch_unrealcv.py" "$UCV" || fail "colour map patch"
grep -q SafeIndex "$UCV" || fail "patch verification"; touch "$UCV"; PATCH_EPOCH=$(date +%s)
echo "==== $(date -u +%H:%M:%S) installing capture module + building editor target ===="
enroot exec "$CTR_PID" python3 "$P/cpp/install.py" > "$F/module_build.log" 2>&1 || fail "cpp/install.py"
enroot exec "$CTR_PID" /home/ue4/UnrealEngine/Engine/Build/BatchFiles/Linux/Build.sh gym_citynavEditor Linux Development -project=/home/ue4/simworld/gym_citynav.uproject -waitmutex -MaxParallelActions=4 >> "$F/module_build.log" 2>&1
grep -q 'Result: Succeeded' "$F/module_build.log" || fail "editor build did not report success"
SO=$(find "$CTRROOT" -name 'libUnrealEditor-UnrealCV.so' -newermt "@$PATCH_EPOCH" 2>/dev/null | head -1)
[ -n "$SO" ] || fail "UnrealCV module was not rebuilt"
echo "UnrealCV rebuilt: $SO"; upload_logs
echo "==== $(date -u +%H:%M:%S) joining the recording queue ===="
sudo systemd-run --unit=preview --collect --setenv=HOME=/home/ubuntu --uid=ubuntu --gid=ubuntu -p WorkingDirectory=/home/ubuntu /bin/bash "$F/bundle/preview_uploader.sh"   # 5-min preview clips only, to the ops prefix
python3 -u "$F/bundle/record_loop.py" "$W" >> "$F/record_loop.log" 2>&1; rc=$?
aws s3 cp "$F/record_loop.log" "$S3/record_loop.log" --only-show-errors || true; upload_logs
echo "record_loop exited rc=$rc"; sudo shutdown -h +1 "recording queue drained"
