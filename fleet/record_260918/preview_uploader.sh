#!/bin/bash
# Side job: for each finished episode cut the first 5 min of rgb.mp4 and upload ONLY that clip to the
# queue's ops prefix (never into the dataset prefix). The review page plays these. Idempotent.
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline; DST=s3://pan-simworld/ue-record/260918/previews
LED=$P/logs/preview_uploaded.txt; touch $LED
# ffmpeg: the imageio_ffmpeg binary the pipeline itself uses (under /home/ubuntu, usable from the host).
export ENROOT_RUNTIME_PATH=/home/ubuntu/ue_record_fleet_20260918/enroot-runtime
ctr() { for c in $(pgrep -f 'sleep 999999999'); do if [ "$(cat /proc/$c/comm 2>/dev/null)" = sleep ] && grep -q '^Seccomp:.*2' /proc/$c/status 2>/dev/null; then echo $c; return; fi; done; }
while true; do
  for d in "$P"/episodes/*coverage_walk*lv_*__v1r2s00*/; do
    [ -d "$d" ] || continue; ep=$(basename "$d"); grep -qxF "$ep" $LED && continue
    [ -f "$d/acceptance.json" ] && [ -f "$d/rgb.mp4" ] || continue
    if [ ! -f "$d/preview5.mp4" ]; then FF=$(ls /home/ubuntu/.local/lib/python3.10/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-* 2>/dev/null | head -1); [ -n "$FF" ] && "$FF" -y -loglevel error -i "$d/rgb.mp4" -t 300 -c copy -movflags +faststart "$d/preview5.mp4" || { echo "$(date -u +%H:%M) ffmpeg failed $ep" >> $P/logs/preview_uploader.log; continue; }; fi
    aws s3 cp "$d/preview5.mp4" "$DST/$ep.mp4" --only-show-errors && echo "$ep" >> $LED && echo "$(date -u +%H:%M) preview uploaded $ep" >> $P/logs/preview_uploader.log
  done
  pgrep -f "[r]ecord_loop.py" >/dev/null || { sleep 90; pgrep -f "[r]ecord_loop.py" >/dev/null || exit 0; }
  sleep 120
done
