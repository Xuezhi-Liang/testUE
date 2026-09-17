#!/usr/bin/env bash
# Launch one instance per shard from longvideo_shards.json.
#   bash launch_fleet.sh <ami-id> ALL
#   bash launch_fleet.sh <ami-id> <shard_id> [<shard_id> ...]
#
# Tries every subnet in turn. 31 g6.4xlarge in one AZ is more than eu-north-1a had:
# `InsufficientInstanceCapacity ... You can currently get g6.4xlarge capacity by not specifying
# an Availability Zone or choosing eu-north-1b`. And no `set -e` around the loop - one shard that
# cannot be placed must not abort the other thirty.
set -uo pipefail
AMI=$1; shift
REGION=${REGION:-eu-north-1}
SUBNETS=${SUBNETS:-"subnet-07533226d7f2f6a60 subnet-091e3e174ae078c27 subnet-04247ba7f6340fd6b"}
SG=${SG:-sg-00067e01b6655e01b}
KEY=${KEY:-Stockholm-pan-data}
TYPE=${TYPE:-g6.4xlarge}
BUDGET_H=${BUDGET_H:-7}
# EXTRA_TAGS='{Key=LongVideoProbe,Value=40:90+60:90}' appends tags; boot.sh reads them to pick a mode.
EXTRA_TAGS=${EXTRA_TAGS:-}
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
FAILED_FILE=${FAILED_FILE:-$P/longvideo_launch_failed.txt}; : > "$FAILED_FILE"
M=$P/longvideo_shards.json
UD=$(base64 -w0 $P/longvideo/userdata.sh)

if [ "${1:-}" = "ALL" ]; then
  IDS=$(python3 -c "
import json
for s in json.load(open('$M')): print(s['shard_id'])")
else
  IDS="$*"
fi

ok=0; fail=0
for SID in $IDS; do
  MAP_ID=$(python3 -c "
import json,sys
r=[s for s in json.load(open('$M')) if s['shard_id']=='$SID']
if not r: sys.exit(1)
print(r[0]['map_id'])" 2>/dev/null)
  if [ -z "${MAP_ID:-}" ]; then echo "SKIP  $SID  (not in the shard manifest)"; fail=$((fail+1)); continue; fi
  placed=""
  for SUB in $SUBNETS; do
    IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$TYPE" \
      --key-name "$KEY" --subnet-id "$SUB" --security-group-ids "$SG" \
      --user-data "$UD" \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=ue-lv-$SID},{Key=LongVideoShard,Value=$SID},{Key=LongVideoMapId,Value=$MAP_ID},{Key=LongVideoBudgetHours,Value=$BUDGET_H}${EXTRA_TAGS:+,$EXTRA_TAGS}]" \
      --query 'Instances[0].InstanceId' --output text 2>/dev/null)
    if [ -n "${IID:-}" ] && [ "$IID" != "None" ]; then placed="$IID $SUB"; break; fi
  done
  if [ -n "$placed" ]; then
    echo "$placed  $SID"
    ok=$((ok+1))
  else
    echo "NO CAPACITY  $SID  (tried: $SUBNETS)"
    # Failures go to a file as well as stdout. A retry list built by grepping a launcher's
    # stdout lost two of three failed shards when that stdout had been piped through `tail`;
    # the launcher knows what failed, so it says so where nothing can truncate it.
    echo "$SID" >> "${FAILED_FILE:-$P/longvideo_launch_failed.txt}"
    fail=$((fail+1))
  fi
done
echo "launched $ok, failed $fail"
