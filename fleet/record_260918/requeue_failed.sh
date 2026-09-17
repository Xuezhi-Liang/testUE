#!/bin/bash
# Requeue shards that FAILED before capturing anything (no frames) - once each - so a fixed worker retries them.
cd /home/ubuntu/ue_record_fleet_20260918; touch requeued.txt
while true; do
  for k in $(aws s3 ls s3://pan-simworld/ue-record/260918/results/ --recursive 2>/dev/null | awk '/result.json/{print $4}'); do
    s=$(basename $(dirname $k)); grep -qx "$s" requeued.txt && continue
    info=$(aws s3 cp s3://pan-simworld/$k - 2>/dev/null | python3 -c "
import json,sys;d=json.load(sys.stdin);a=d.get('attempts') or [];fr=[x.get('frames') for x in a if x.get('frames')]
print('retry' if d.get('state') not in ('accepted','rejected') and not fr else 'keep', d.get('state'))")
    case "$info" in retry*)
      aws s3 rm s3://pan-simworld/ue-record/260918/results/$s/ --recursive --only-show-errors; aws s3 rm s3://pan-simworld/ue-record/260918/claims/$s.json --only-show-errors
      echo "$s" >> requeued.txt; echo "$(date -u +%H:%M) requeued $s ($info)";;
    esac
  done
  n=$(aws s3 ls s3://pan-simworld/ue-record/260918/workers/ --recursive 2>/dev/null | grep -c finished.json); [ "$n" -ge 30 ] && exit 0
  sleep 300
done
