#!/usr/bin/env bash
# Start the stopped shard instances in RECOVERY mode: package the frames already on their disks
# and upload them, without recording anything.
#
#   bash recover_fleet.sh              # every stopped shard instance
#   bash recover_fleet.sh <shard_id>...
#
# The instances are `stopped`, not terminated, so their root volumes - and the frames on them -
# are intact. What they lack is the metadata capture_engine writes after the last frame, which is
# what recover.py builds.
set -uo pipefail
REGION=${REGION:-eu-north-1}
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline

if [ "$#" -gt 0 ]; then WANT="$*"; else WANT=""; fi

MIME=${MIME:-$(dirname "$0")/userdata_perboot.mime}
[ -f "$MIME" ] || { echo "missing $MIME"; exit 1; }
UD_B64=$(base64 -w0 "$MIME")

mapfile -t ROWS < <(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag-key,Values=LongVideoShard" "Name=instance-state-name,Values=stopped" \
  --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==`LongVideoShard`]|[0].Value]' \
  --output text)

n=0
for row in "${ROWS[@]}"; do
  IID=$(echo "$row" | awk '{print $1}')
  SID=$(echo "$row" | awk '{print $2}')
  [ -z "${SID:-}" ] && continue
  if [ -n "$WANT" ] && ! grep -qw -- "$SID" <<< "$WANT"; then continue; fi
  TAGS="Key=LongVideoRecover,Value=1"
  # SKIP_GATES=1 packages for delivery only: no acceptance gates, no review video. The verdict is
  # then computed from S3 by longvideo/readjudicate.py instead of on the instance.
  [ -n "${SKIP_GATES:-}" ] && TAGS="$TAGS Key=LongVideoSkipGates,Value=$SKIP_GATES"
  [ -n "${DELIVER_PARTIAL:-}" ] && TAGS="$TAGS Key=LongVideoDeliverPartial,Value=$DELIVER_PARTIAL"
  # RESYNC=1 uploads only - see boot.sh. Use it when a completeness check finds objects missing
  # from an episode that is otherwise packaged and correct.
  [ -n "${RESYNC:-}" ] && TAGS="$TAGS Key=LongVideoResync,Value=$RESYNC"
  aws ec2 create-tags --region "$REGION" --resources "$IID" \
    --tags $TAGS >/dev/null || { echo "TAG FAILED $SID"; continue; }
  # Replace the user-data with the per-boot MIME form before starting. Without this the start is
  # pointless: cloud-init's scripts-user module is per-instance, so the original shell user-data
  # ran once at first launch and a stop/start brings the machine up with NOTHING running on it.
  # Measured: 15 instances started this way sat at 0% CPU for 70 minutes, and the console showed
  # cloud-init's final stage finish 0.05 s after it began. The instance must be `stopped` for this
  # call, which it is - that is the only state this script starts from.
  aws ec2 modify-instance-attribute --region "$REGION" --instance-id "$IID" \
    --attribute userData --value "$UD_B64" >/dev/null \
    || { echo "USERDATA FAILED $SID"; continue; }
  aws ec2 start-instances --region "$REGION" --instance-ids "$IID" \
    --query 'StartingInstances[0].CurrentState.Name' --output text \
    | sed "s|^|$IID  $SID  -> |"
  n=$((n+1))
done
echo "started $n instance(s) in recovery mode"
