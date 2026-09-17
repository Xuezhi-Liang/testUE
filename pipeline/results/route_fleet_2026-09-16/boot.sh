#!/bin/bash
set -euo pipefail
W=$1
F=/home/ubuntu/ue_route_fleet_20260916
R=/home/ubuntu/ue_route_validation_20260916
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
export AWS_DEFAULT_REGION=eu-north-1
export ENROOT_RUNTIME_PATH="$F/enroot-runtime"
mkdir -p "$ENROOT_RUNTIME_PATH"
S3=s3://pan-simworld/ue-route-validation/20260916/workers/$W
exec >> "$F/boot.log" 2>&1
trap 'rc=$?; if [ "$rc" -ne 0 ]; then printf "boot failed rc=%s\n" "$rc" >> "$F/boot.log"; aws s3 cp "$F/boot.log" "$S3/boot.log" --only-show-errors || true; sudo shutdown -h +2 "route validation boot failed"; fi' EXIT
mkdir -p "$R" "$P"
cp -a "$F/bundle/runtime/." "$R/"
cp -a "$F/bundle/catalog" "$R/"
python3 - "$F/bundle/assignments.json" "$R/manifest.json" "$W" <<'PY'
import json,sys
from pathlib import Path
all=json.loads(Path(sys.argv[1]).read_text());Path(sys.argv[2]).write_text(json.dumps(all[sys.argv[3]],ensure_ascii=False,indent=2))
PY
# Keep prior source code and all old recordings intact.
tar czf "$F/prior_pipeline_sources.tar.gz" -C "$P" --wildcards '*.py' cpp 2>/dev/null || true
cp -a "$F/bundle/pipeline/." "$P/"
cp "$F/bundle/launch_ue_fast.sh" "$P/../launch_ue_fast.sh"
python3 "$R/test_runtime_guard.py"
for i in $(seq 1 30); do nvidia-smi -L >/dev/null 2>&1 && break; sleep 5; done
nvidia-smi -L
nohup bash /home/ubuntu/prj/SimWorld/launch_unreal_instance/start_enroot_container.sh "exec sleep 999999999" > "$F/container_boot.log" 2>&1 &
CTR_PID=""
for i in $(seq 1 90); do
 for cand in $(pgrep -f 'sleep 999999999'); do
  if [ "$(cat /proc/$cand/comm 2>/dev/null)" = sleep ] && grep -q '^Seccomp:.*2' /proc/$cand/status 2>/dev/null; then CTR_PID=$cand; break; fi
 done
 [ -n "$CTR_PID" ] && break
 sleep 5
done
[ -n "$CTR_PID" ] || { echo 'No container process'; exit 1; }
export CTR_PID
printf 'container pid %s\n' "$CTR_PID"
enroot exec "$CTR_PID" python3 "$P/cpp/install.py" > "$F/module_build.log" 2>&1
enroot exec "$CTR_PID" /home/ue4/UnrealEngine/Engine/Build/BatchFiles/Linux/Build.sh gym_citynavEditor Linux Development -project=/home/ue4/simworld/gym_citynav.uproject -waitmutex >> "$F/module_build.log" 2>&1
# Compiler success must be explicit, not only a zero shell exit code.
grep -q 'Result: Succeeded' "$F/module_build.log"
aws s3 cp "$F/module_build.log" "$S3/module_build.log" --only-show-errors
aws s3 cp "$F/boot.log" "$S3/boot.log" --only-show-errors
python3 -u "$F/bundle/supervise.py" "$W"
sudo shutdown -h +1 'route validation batch finished; results preserved'
