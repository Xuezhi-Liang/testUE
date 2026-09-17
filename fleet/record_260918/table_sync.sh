#!/bin/bash
# Keep the local shard table in step with the S3 queue manifest (entries appended or edited by the
# controller - lighting fallbacks, seed bumps). gen_task.py reads the local table; manifest wins.
P=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline
while true; do
  aws s3 cp s3://pan-simworld/ue-record/260918/manifest.json /tmp/manifest_260918.json --only-show-errors 2>/dev/null && python3 - <<'PY'
import json
P='/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline'
man=json.load(open('/tmp/manifest_260918.json')); tbl=json.load(open(f'{P}/longvideo_shards.json'))
by={t['shard_id']:t for t in tbl}; changed=0
for m in man:
    e={k:v for k,v in m.items() if k not in ('packs_to_sync','project_dir','est_s','core_m2','centreline_m')}
    if by.get(m['shard_id'])!=e: by[m['shard_id']]=e; changed+=1
if changed:
    json.dump(list(by.values()), open(f'{P}/longvideo_shards.json','w'), indent=1); print('table_sync: updated', changed)
PY
  sleep 60
done
