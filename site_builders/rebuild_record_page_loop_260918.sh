#!/bin/bash
cd /home/ubuntu/WM-Unreal-data-collection/local_run
for i in $(seq 1 500); do
  python3 build_record_page.py 260918 >> logs_record_page_260918.log 2>&1
  n=$(aws s3 ls s3://pan-simworld/ue-record/260918/workers/ --recursive 2>/dev/null | grep -c finished.json)
  echo "$(date -u +%H:%M) rebuilt; videos=$(grep -c '<video' site/longvideo/record-260918/index.html); workers finished=$n" >> logs_record_page_260918.log
  [ "$n" -ge 30 ] && { python3 build_record_page.py 260918 >> logs_record_page_260918.log 2>&1; echo "ALL DONE" >> logs_record_page_260918.log; exit 0; }
  sleep 600
done
