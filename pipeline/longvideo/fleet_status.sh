#!/usr/bin/env bash
# What every long-video instance is doing, from its own beacon plus EC2 state.
set -uo pipefail
REGION=${REGION:-eu-north-1}
S=s3://pan-simworld/ue-revist-long-video/_status
aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag-key,Values=LongVideoShard" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,LaunchTime,Tags[?Key==`LongVideoShard`]|[0].Value]' \
  --output text | sort -k4 | while read -r iid state launched slug; do
  beacon=$(aws s3 cp "$S/${slug}.json" - 2>/dev/null || echo '{}')
  line=$(printf '%s' "$beacon" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
a=(d.get('attempts') or [{}])[-1]
print(f\"done={d.get('done')} attempt={a.get('attempt')} bound={a.get('length_bound_by')} \"
      f\"frames={a.get('frames')} hours={a.get('hours')} uploaded={len(d.get('uploaded') or [])} \"
      f\"ts={d.get('ts')}\")" 2>/dev/null || echo "no beacon")
  printf '%-20s %-9s %-52s %s\n' "$iid" "$state" "$slug" "$line"
done
