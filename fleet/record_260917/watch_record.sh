#!/bin/bash
PRE=s3://pan-simworld/ue-record/260917
for i in $(seq 1 40); do sleep 180
  boots=0; fails=0
  for w in 00 01; do
    if aws s3 ls "$PRE/workers/$w/boot.log" >/dev/null 2>&1; then boots=$((boots+1)); aws s3 cp "$PRE/workers/$w/boot.log" - 2>/dev/null | grep -q 'BOOT FAILED' && { fails=$((fails+1)); echo "BOOT FAILED $w"; aws s3 cp "$PRE/workers/$w/boot.log" - 2>/dev/null | tail -4; }; fi
  done
  claims=$(aws s3 ls "$PRE/claims/" 2>/dev/null | wc -l); results=$(aws s3 ls "$PRE/results/" --recursive 2>/dev/null | grep -c result.json); eps=$(aws s3 ls "$PRE/../../long-video-data-260917/" 2>/dev/null | grep -c PRE)
  echo "$(date -u +%H:%M) boots=$boots/2 fails=$fails claims=$claims results=$results dest_slugs=$eps"
  [ "$fails" -gt 0 ] && exit 2; [ "$results" -gt 0 ] && exit 0
done; echo "2 h without a result"; exit 1
