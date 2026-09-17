#!/usr/bin/env bash
# After a recovery round: which shards are NOT in S3, and re-run those.
#
# The instances in the current round carry an uploader that breaks out of its loop on the stop
# marker even if a sync had just failed, so a single transient failure can leave an episode on a
# disk that is then powered off. That is fixed for later rounds; this reconciles the current one.
#
#   bash reconcile.sh            # report only
#   bash reconcile.sh --rerun    # report, then start the shards that are missing
set -uo pipefail
REGION=${REGION:-eu-north-1}
S3=s3://pan-simworld/ue-revist-long-video
HERE=$(cd "$(dirname "$0")" && pwd)

mapfile -t SHARDS < <(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag-key,Values=LongVideoRecover" \
  --query 'Reservations[].Instances[].[Tags[?Key==`LongVideoShard`]|[0].Value]' \
  --output text | tr '\t' '\n' | grep . | grep -v '^None$' | sort -u)

echo "本轮分片: ${#SHARDS[@]}"
# Episode prefixes by walking the delimiter, NOT `ls --recursive`. Recursive means enumerating
# every delivered frame - past 6 million keys once the long episodes landed - which turned this
# check into a hang. Two levels of cheap listings answer the same question.
IN_S3=""
for m in $(aws s3 ls "$S3/" | awk '/PRE/{print $2}' | grep -v '^_ops/$\|^_status/$'); do
  for lvl in "$S3/$m" "$S3/${m}_rejected/"; do
    IN_S3="$IN_S3
$(aws s3 ls "$lvl" 2>/dev/null | awk '/PRE/{print $2}')"
  done
done

MISSING=()
for sid in "${SHARDS[@]}"; do
  if grep -q "lv_${sid}/" <<< "$IN_S3"; then
    printf "  %-52s ✓ 已入库\n" "$sid"
  else
    printf "  %-52s ✗ 缺失\n" "$sid"
    MISSING+=("$sid")
  fi
done
echo
echo "缺失 ${#MISSING[@]} 个"
[ "${#MISSING[@]}" -eq 0 ] && exit 0
printf '  %s\n' "${MISSING[@]}"
if [ "${1:-}" = "--rerun" ]; then
  echo "重跑缺失的分片"
  SKIP_GATES=1 bash "$HERE/recover_fleet.sh" "${MISSING[@]}"
fi
