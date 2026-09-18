#!/bin/bash
# Every 15 min: episodes rejected only by the old 3D speed gate get re-judged in the plane (idempotent).
cd /home/ubuntu/WM-Unreal-data-collection/local_run
for i in $(seq 1 300); do
  pgrep -f '[r]ejudge_speed_260918' >/dev/null || python3 rejudge_speed_260918.py 260918 >> rejudge_speed_260918.log 2>&1
  n=$(aws s3 ls s3://pan-simworld/ue-record/260918/workers/ --recursive 2>/dev/null | grep -c finished.json); [ "$n" -ge 30 ] && exit 0
  sleep 900
done
