#!/bin/bash
# Handle shards that ended without an accepted/rejected episode, once each:
#   lighting calibration / fill hang  -> append <sid>_lvl (author lighting) to the S3 manifest
#   depth-probe / collision refusal    -> bump the seed in the manifest and requeue the same shard
#   anything else (no start, crash)    -> requeue the same shard
cd /home/ubuntu/ue_record_fleet_20260918; touch requeued.txt
while true; do
  for k in $(aws s3 ls s3://pan-simworld/ue-record/260918/results/ --recursive 2>/dev/null | awk '/result.json/{print $4}'); do
    s=$(basename $(dirname $k)); [ "$(grep -cx "$s" requeued.txt)" -ge 2 ] && continue   # at most two handlings per shard
    cls=$(aws s3 cp s3://pan-simworld/$k - 2>/dev/null | python3 -c "
import json,sys;d=json.load(sys.stdin);a=d.get('attempts') or [];fr=[x.get('frames') for x in a if x.get('frames')]
if d.get('state') in ('accepted','rejected') or fr: print('keep'); sys.exit()
st=d.get('state') or ''; err=' '.join([d.get('error') or '']+[(x.get('refusal') or '')+(x.get('result') or '') for x in a])
print('lighting' if ('sky-light' in err or 'apply_fill' in err) else 'reseed' if ('depth_probe' in st or 'collision' in st or 'depth' in err or 'collision' in err) else 'retry')")
    [ "$cls" = keep ] && { echo "$s" >> requeued.txt; continue; }
    if [ "$cls" = lighting ] && ! aws s3 cp s3://pan-simworld/ue-record/260918/results/$s/lv_${s}_runner.log - 2>/dev/null | grep -q 'sky-light\|apply_fill'; then cls=retry; fi
    python3 - "$s" "$cls" <<'PY'
import json, sys, subprocess
s, cls = sys.argv[1], sys.argv[2]
man = json.loads(subprocess.run(['aws','s3','cp','s3://pan-simworld/ue-record/260918/manifest.json','-'], capture_output=True, text=True).stdout)
me = next((m for m in man if m['shard_id'] == s), None)
if not me: sys.exit()
if cls == 'lighting':
    if me.get('lighting') != 'level' and not any(m['shard_id'] == s + '_lvl' for m in man):
        man.append(dict(me, shard_id=s + '_lvl', lighting='level', seed=int(me['seed']) + 100, fallback_of=s, why='fill lighting failed; author lighting'))
        print('appended', s + '_lvl')
    else: cls = 'keep'
elif cls == 'reseed':
    me['seed'] = int(me['seed']) + 1000; me['why'] = 'route refused (depth/collision); new seed'; print('reseeded', s)
if cls != 'keep':
    json.dump(man, open('/tmp/man_260918.json', 'w'), indent=1); subprocess.run(['aws','s3','cp','/tmp/man_260918.json','s3://pan-simworld/ue-record/260918/manifest.json','--only-show-errors'], check=True)
if cls in ('reseed', 'retry'):
    subprocess.run(['aws','s3','rm',f's3://pan-simworld/ue-record/260918/results/{s}/','--recursive','--only-show-errors']); subprocess.run(['aws','s3','rm',f's3://pan-simworld/ue-record/260918/claims/{s}.json','--only-show-errors'])
    print('requeued', s)
PY
    echo "$s" >> requeued.txt; echo "$(date -u +%H:%M) handled $s as $cls"
  done
  n=$(aws s3 ls s3://pan-simworld/ue-record/260918/workers/ --recursive 2>/dev/null | grep -c finished.json); [ "$n" -ge 30 ] && exit 0
  sleep 300
done
