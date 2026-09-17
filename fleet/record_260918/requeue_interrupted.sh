#!/bin/bash
# The two canary shards were interrupted under the old 45-min watchdog; once their (failed) results land, requeue them.
for i in $(seq 1 30); do
  left=0
  for s in Game_StonePineForest_Maps_Mountains_Map_LevelDesign__v1r2s00 Game_Brushify_Maps_Arctic_Arctic__v1r2s00; do
    if aws s3 ls s3://pan-simworld/ue-record/260918/results/$s/result.json >/dev/null 2>&1; then
      st=$(aws s3 cp s3://pan-simworld/ue-record/260918/results/$s/result.json - 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('state'))")
      if [ "$st" != accepted ] && [ "$st" != rejected ]; then
        aws s3 rm s3://pan-simworld/ue-record/260918/results/$s/ --recursive --only-show-errors; aws s3 rm s3://pan-simworld/ue-record/260918/claims/$s.json --only-show-errors
        echo "$(date -u +%H:%M) requeued $s (was $st)"
      else echo "$(date -u +%H:%M) $s finished as $st, left alone"; fi
    else left=$((left+1)); fi
  done
  [ $left -eq 0 ] && exit 0; sleep 60
done; echo "gave up waiting"; exit 1
