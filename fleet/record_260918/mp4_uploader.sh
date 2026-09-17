#!/bin/bash
# Side uploader: copy each finished episode's rgb.mp4 (and the QA video if any) to the job prefix, so
# the review page can play them after the instance is gone. Idempotent; exits when the worker is done.
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline; DST=s3://pan-simworld/long-video-data-260918
LED=$P/logs/mp4_uploaded.txt; touch $LED
while true; do
  for d in "$P"/episodes/*coverage_walk*lv_*__v1s00*/; do
    [ -d "$d" ] || continue; ep=$(basename "$d"); grep -qxF "$ep" $LED && continue
    [ -f "$d/acceptance.json" ] || continue
    slug=$(echo "$ep" | sed -E 's/__coverage_walk.*//')
    ok=1; for f in rgb.mp4 rgb_proxy.mp4 preview.mp4; do [ -f "$d/$f" ] && { aws s3 cp "$d/$f" "$DST/$slug/$ep/$f" --only-show-errors || ok=0; }; done
    [ $ok = 1 ] && echo "$ep" >> $LED && echo "$(date -u +%H:%M) mp4 uploaded $ep" >> $P/logs/mp4_uploader.log
  done
  pgrep -f "[r]ecord_loop.py" >/dev/null || { sleep 60; pgrep -f "[r]ecord_loop.py" >/dev/null || exit 0; }
  sleep 120
done
