#!/usr/bin/env bash
# Keep trying the shards that could not be placed. g6.4xlarge capacity is released as other
# instances stop, and this job's own machines start stopping after ~6 h - so a shard that has no
# capacity now usually has some later. One attempt per shard per cycle, and it stops when every
# shard in the manifest has an instance or the deadline passes.
set -uo pipefail
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
AMI=${AMI:-ami-0f8108312d536a07e}
REGION=${REGION:-eu-north-1}
EVERY=${EVERY:-300}
UNTIL=${UNTIL:-$(( $(date +%s) + 6*3600 ))}
while [ "$(date +%s)" -lt "$UNTIL" ]; do
  HAVE=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag-key,Values=LongVideoShard" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].Tags[?Key==`LongVideoShard`]|[].Value' --output text \
    | tr '\t' '\n' | sort -u)
  MISSING=$(python3 -c "
import json, sys
have = set(sys.stdin.read().split())
todo = [s['shard_id'] for s in json.load(open('$P/longvideo_shards.json'))
        if s['shard_id'] not in have]
print(' '.join(todo))" <<< "$HAVE")
  if [ -z "${MISSING// }" ]; then echo "$(date '+%H:%M') every shard placed"; break; fi
  echo "$(date '+%H:%M') still unplaced: $(echo $MISSING | wc -w) -> $MISSING"
  bash "$P/longvideo/launch_fleet.sh" "$AMI" $MISSING 2>&1 | grep -vE "^launched" || true
  sleep "$EVERY"
done
echo "retry loop done"
