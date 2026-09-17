import json,time,sys,shutil,traceback,collections,os
from pathlib import Path
R=Path(__file__).resolve().parent
SITE=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/route-validation')
SITE.mkdir(exist_ok=True);(SITE/'reports').mkdir(exist_ok=True);(SITE/'plots').mkdir(exist_ok=True)
P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline');sys.path.insert(0,str(P))
TERMINAL={'geometry_pass','coverage_review','route_failed','failed','blocked_asset','timeout'}
def load(p,default):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return default
def atomic(p,d):
 tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(d,ensure_ascii=False));tmp.replace(p)
def plot(slug,attempt):
 target=SITE/'plots'/f'{slug}_{attempt["attempt"]}.png'
 if target.exists():return str(target.relative_to(SITE))
 import numpy as np
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 from matplotlib.collections import PolyCollection
 import plan_navmesh as pnm
 fz=load(Path(attempt['frozen']),{});poses=fz.get('poses',[])
 if not poses:return None
 navroot=Path(attempt['frozen']).parent/'nav';nav,_=pnm.load(str(navroot/f'{slug}.bin'),str(navroot/f'{slug}.json'))
 fig,ax=plt.subplots(figsize=(9,7),facecolor='#101b29');ax.set_facecolor('#101b29')
 tris=nav.verts[nav.wtris][:,:,:2]/100
 ax.add_collection(PolyCollection(tris,facecolors='#354658',edgecolors='none',rasterized=True))
 step=max(1,len(poses)//16000);sample=poses[::step]+[poses[-1]];xy=np.array([[p['x_cm'],p['y_cm']] for p in sample])/100
 ax.plot(xy[:,0],xy[:,1],color='#64ddcf',lw=.7,alpha=.9);ax.scatter(xy[0,0],xy[0,1],c='#ffdc7a',s=45,label='Start',zorder=5)
 ax.autoscale();ax.set_aspect('equal');ax.tick_params(colors='#bacbdd');ax.set_xlabel('X / m',color='#bacbdd');ax.set_ylabel('Y / m',color='#bacbdd');ax.set_title(slug.removeprefix('Game_')+'\nEngine-checked planned route / same-session navmesh',color='white',fontsize=10);ax.legend();fig.tight_layout();fig.savefig(target,dpi=120);plt.close(fig)
 return str(target.relative_to(SITE))
def publish():
 rows=[]
 for mc in load(R/'manifest.json',[]):
  out=R/'maps'/mc['slug'];result=load(out/'result.json',{});row={**mc,**result};row.setdefault('state','queued')
  if row['state'] in {'running','connecting','starting'}:
   started=row.get('started_epoch')
   if not started and row.get('started_at'):
    import calendar
    started=calendar.timegm(time.strptime(row['started_at'],'%Y-%m-%dT%H:%M:%SZ'))
   if started:row['elapsed_s']=round(time.time()-started,1)
  if result:
   atomic(SITE/'reports'/f'{mc["slug"]}.json',result);row['report']='reports/'+mc['slug']+'.json'
  attempts=result.get('attempts',[]);a=next((a for a in attempts if a.get('attempt')==result.get('chosen_attempt')),attempts[-1] if attempts else {})
  row['latest']=a
  if a.get('frozen'):
   try:row['plot']=plot(mc['slug'],a)
   except Exception as e:row['plot_error']=str(e)
  log=out/'worker.log'
  if log.exists():
   with log.open('rb') as f:
    f.seek(max(0,log.stat().st_size-2500));row['log_tail']=f.read().decode(errors='replace')
  else:row['log_tail']=''
  if row['state']=='running':
   for line in row['log_tail'].splitlines():
    if 'PHASE ' in line:row['phase']=line.split('PHASE ',1)[1]
    elif '[coverage] mix tune' in line:row['phase']='生成完整路线与动作优化'
    elif '[freeze-cov] validating ' in line:row['phase']='引擎逐帧碰撞检查'
    elif '[freeze-cov] reroute attempt ' in line:row['phase']='发现局部障碍，重新规划路线'
    elif '[freeze-cov] collisions ' in line:row['phase']='深度画面抽查'
  rows.append(row)
 q=load(R/'queue.json',{});counts=dict(collections.Counter(x['state'] for x in rows));q['heartbeat_age_s']=round(time.time()-q.get('updated_epoch',time.time()),1)
 samples=[x['total_elapsed_s'] for x in rows if x.get('total_elapsed_s') and x['state'] in TERMINAL and x['state']!='blocked_asset']
 if len(samples)>=3:
  mean=sum(samples)/len(samples);q['eta_sample_maps']=len(samples);q['mean_map_s']=mean;q['rough_remaining_h']=mean*sum(x['state'] not in TERMINAL for x in rows)/3600
 atomic(SITE/'status.json',dict(updated_epoch=time.time(),queue=q,counts=counts,total=len(rows),completed=sum(counts.get(k,0) for k in TERMINAL),rows=rows))
if __name__=='__main__':
 while True:
  try:publish()
  except Exception:traceback.print_exc()
  if '--once' in sys.argv:break
  time.sleep(5)
