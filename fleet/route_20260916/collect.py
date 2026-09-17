import boto3,json,time,os,traceback,concurrent.futures
PASS_EPOCH=1789574000   # second pass launched 16:00 UTC 2026-09-16; pass-1 results and markers predate it
from pathlib import Path
F=Path(__file__).resolve().parent;R=Path('/home/ubuntu/ue_route_validation_20260916');B='pan-simworld';PREFIX='ue-route-validation/20260916/workers/'
s=boto3.Session(region_name='eu-north-1');s3=s.client('s3');ec=s.client('ec2');selected=json.loads((F/'selected_instances.json').read_text());assign=json.loads((F/'assignments.json').read_text());allowed={w:{m['slug'] for m in rows} for w,rows in assign.items()};seen={};pool=concurrent.futures.ThreadPoolExecutor(max_workers=6)
def load(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {}
def atomic(p,d):
 p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2));tmp.replace(p)
def fetch(obj):
 key=obj['Key'];parts=key[len(PREFIX):].split('/');w=parts[0]
 if w not in allowed:return
 rest=parts[1:]
 if rest[0]=='maps':
  if len(rest)<3 or rest[1] not in allowed[w]:return
  dest=R.joinpath(*rest)
 elif len(rest)==1:dest=F/'workers'/w/rest[0]
 else:return
 if '..' in rest:raise RuntimeError('Unexpected S3 key')
 signature=(obj['ETag'],obj['Size'])
 if seen.get(key)==signature and dest.exists():return
 dest.parent.mkdir(parents=True,exist_ok=True);tmp=dest.with_name(dest.name+'.s3download')
 try:s3.download_file(B,key,str(tmp));tmp.replace(dest);seen[key]=signature
 except OSError as e:
  if e.errno!=36:raise
  seen[key]=signature;print('skip over-long name',key[-80:],flush=True)
while True:
 try:
  objs=[]
  for page in s3.get_paginator('list_objects_v2').paginate(Bucket=B,Prefix=PREFIX):objs.extend(page.get('Contents',[]))
  for result in pool.map(fetch,objs):pass
  instances={x['InstanceId']:x for res in ec.describe_instances(InstanceIds=[x['InstanceId'] for x in selected])['Reservations'] for x in res['Instances']}
  workers=[]
  for i,x in enumerate(selected):
   w=f'{i:02}';q=load(F/'workers'/w/'queue.json');done=load(F/'workers'/w/'finished.json');instance=instances[x['InstanceId']]
   if w not in allowed:workers.append(dict(worker=w,instance_id=x['InstanceId'],instance_state=instance['State']['Name'],state='not_in_this_pass',active=None,queue_heartbeat_epoch=q.get('updated_epoch'),completed=0,total=0,private_ip=instance.get('PrivateIpAddress'),error=None));continue
   if done and q and done.get('finished_epoch',0)<q.get('started_epoch',0):done={}   # pass-1 finished.json predates this queue
   status='not_started' if not (F/f'launch_{w}.json').exists() else instance['State']['Name']
   if q:status=q.get('state',status)
   completed=0
   for slug in allowed[w]:
    r=load(R/'maps'/slug/'result.json')
    if r.get('state') in {'geometry_pass','coverage_review','route_failed','failed','blocked_asset','timeout'} and r.get('finished_epoch',0)>PASS_EPOCH:completed+=1
   if not q or q.get('started_epoch',0)<PASS_EPOCH:status='waiting_capacity' if instance['State']['Name']=='stopped' else instance['State']['Name'];q={};done={}
   if done:status='finished' if q.get('state')=='finished' and completed==len(allowed[w]) else 'incomplete'
   workers.append(dict(worker=w,instance_id=x['InstanceId'],instance_state=instance['State']['Name'],state=status,active=q.get('active'),queue_heartbeat_epoch=q.get('updated_epoch'),completed=completed,total=len(allowed[w]),private_ip=instance.get('PrivateIpAddress'),error=q.get('error')))
  atomic(R/'fleet.json',dict(run='20260916',updated_epoch=time.time(),expected_workers=10,workers=workers,remote_maps=sum(len(a) for a in allowed.values()),s3_prefix='s3://'+B+'/'+PREFIX))
  atomic(F/'collector_status.json',dict(updated_epoch=time.time(),objects=len(objs),cached_objects=len(seen)))
 except Exception:traceback.print_exc()
 time.sleep(15)
