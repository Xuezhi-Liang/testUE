from pathlib import Path
import json,csv,io,concurrent.futures,traceback
import boto3
from botocore.config import Config

r=Path(__file__).resolve().parent
s=boto3.client('s3',config=Config(max_pool_connections=8,read_timeout=45,retries={'max_attempts':2}))
eps=json.loads(Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/status.json').read_text())['episodes']
(r/'cloud').mkdir(exist_ok=True)
def meta(e):
 p=r/'cloud'/e['episode_id'];p.mkdir(exist_ok=True);key=e['prefix'].split('/',3)[3]
 for name in ['trajectory.json','acceptance.json']:
  f=p/name
  if not f.exists():f.write_bytes(s.get_object(Bucket='pan-simworld',Key=key+name)['Body'].read())
 return e
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
 for f in concurrent.futures.as_completed([pool.submit(meta,e) for e in eps]):
  try:f.result()
  except Exception as ex:print('METADATA ERROR',str(ex),flush=True)
groups={}
for e in eps:
 p=r/'cloud'/e['episode_id']/'trajectory.json'
 if p.exists():groups.setdefault(e['map_id'],[]).append(e)
selected=[]
for mapid,es in groups.items():
 # Latest seed among accepted runs: explicitly one representative, not the union of all shards.
 e=max(es,key=lambda e:(bool(e.get('accepted')),int(e.get('seed') or 0)))
 selected.append(e)
(r/'selected_cloud.json').write_text(json.dumps(selected,indent=2))
print('METADATA',len(eps),'REPRESENTATIVES',len(selected),flush=True)
def route(e):
 p=r/'cloud'/e['episode_id'];out=p/'route_sample.json'
 if out.exists():return e['map_id']
 key=e['prefix'].split('/',3)[3]+'frames.csv'
 body=s.get_object(Bucket='pan-simworld',Key=key)['Body']
 rows=[];count=0;last=None
 for q in csv.DictReader(io.TextIOWrapper(body,encoding='utf-8')):
  count+=1
  last=[int(q['frame_id']),float(q['actual_x_cm']),float(q['actual_y_cm']),float(q['actual_z_cm']),float(q['actual_yaw_deg'])]
  if last[0]%12==0:rows.append(last)
 if last and rows[-1]!=last:rows.append(last)
 out.write_text(json.dumps({'episode':e,'csv_frames_read':count,'sample_stride':12,'columns':['frame','x_cm','y_cm','z_cm','yaw_deg'],'poses':rows},separators=(',',':')))
 return e['map_id']
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 for f in concurrent.futures.as_completed([pool.submit(route,e) for e in selected]):
  try:print('ROUTE',f.result(),flush=True)
  except Exception as ex:print('ROUTE ERROR',str(ex),flush=True)
