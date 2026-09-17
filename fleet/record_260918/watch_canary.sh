#!/bin/bash
PRE=s3://pan-simworld/ue-record/260918
for i in $(seq 1 60); do sleep 120
  boots=0; fails=0
  for w in 00 01; do
    if aws s3 ls "$PRE/workers/$w/boot.log" >/dev/null 2>&1; then boots=$((boots+1)); aws s3 cp "$PRE/workers/$w/boot.log" - 2>/dev/null | grep -q 'BOOT FAILED' && { fails=$((fails+1)); echo "BOOT FAILED $w"; aws s3 cp "$PRE/workers/$w/boot.log" - 2>/dev/null | tail -4; }; fi
  done
  claims=$(aws s3 ls "$PRE/claims/" 2>/dev/null | wc -l); results=$(aws s3 ls "$PRE/results/" --recursive 2>/dev/null | grep -c result.json)
  echo "$(date -u +%H:%M) boots=$boots/2 fails=$fails claims=$claims results=$results"
  [ "$fails" -gt 0 ] && exit 2
  if [ "$results" -gt 0 ]; then for k in $(aws s3 ls "$PRE/results/" --recursive | awk '/result.json/{print $4}'); do aws s3 cp s3://pan-simworld/$k - 2>/dev/null | python3 -c "
import json,sys;d=json.load(sys.stdin);print('RESULT', d.get('shard_id','')[5:50], d.get('state'), '| err', (d.get('error') or '')[:120], '| attempts', [(a.get('result'), a.get('frames'), (a.get('refusal') or '')[:80]) for a in d.get('attempts',[])], '| uploaded', len(d.get('uploaded') or []), '|', round((d.get('total_elapsed_s') or 0)/60), 'min')"; done; exit 0; fi
done; echo "2 h without a result"; exit 1
