import os,sys,json,time,signal,traceback,hashlib
from pathlib import Path
from types import SimpleNamespace
R=Path(__file__).resolve().parent;slug=sys.argv[1]
mc=next(x for x in json.loads((R/'manifest.json').read_text()) if x['slug']==slug)
P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline')
sys.path[:0]=[str(P),'/home/ubuntu/WM-Unreal-data-collection']
os.environ['MAP']=mc['map_id'];os.environ['PORT']='9208'
import unrealcv,engine,freeze_coverage as F,coverage as C,survey_core as SC
import numpy as np
from runtime_guard import bounded_call,RpcTimeout
out=R/'maps'/slug;out.mkdir(parents=True,exist_ok=True)
t0=time.time();state={'slug':slug,'map_id':mc['map_id'],'state':'connecting','started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'attempts':[]}
previous=json.loads((out/'result.json').read_text()) if (out/'result.json').exists() else {}
state['retry_history']=previous.get('retry_history',[])
state['started_epoch']=previous.get('started_epoch',t0)
def save(phase=None):
 if phase:state['phase']=phase
 state['elapsed_s']=round(time.time()-t0,1);p=out/'result.tmp';p.write_text(json.dumps(state,ensure_ascii=False,indent=2));p.replace(out/'result.json');print('PHASE',phase,flush=True)
try:
 save('连接 UE（已确认运行就绪）')
 cl=unrealcv.Client(('127.0.0.1',9208))
 if not bounded_call('UnrealCV connection',cl.connect,timeout_s=30) or not cl.isconnected():
  raise ConnectionError('UnrealCV connection failed after PIE readiness')
 original_request=cl.request
 def request(command,*args,**kwargs):
  label=str(command)[:120]
  return bounded_call('UnrealCV '+label,original_request,command,*args,timeout_s=180,**kwargs)
 cl.request=request
 assert cl.request('vget /unrealcv/status'),'Empty UnrealCV status'
 cl.request('vrun setres 1280x720w')
 ucv=SimpleNamespace(client=cl);state['state']='running';save('起点与导航')
 state['world_ready']=engine.query(cl.request,"RESULT.update(world_name=w.get_name(),world_path=w.get_path_name())")
 if mc['map_id'].split('/')[-1] not in state['world_ready']['world_path']:
  raise RuntimeError('Unexpected active world: '+str(state['world_ready']))
 # The only client lasts for the whole editor session. Do not call engine.connect here.
 settings=json.loads((R/'template.json').read_text())['render']
 for cv in settings['cvars']:cl.request('vrun '+cv)
 keys=[x.split()[0] for x in settings['cvars']]
 state['actual_cvars']=engine.query(cl.request,'RESULT.update({k:unreal.SystemLibrary.get_console_variable_float_value(k) for k in '+repr(keys)+'})')
 for cv in settings['cvars']:
  k,v=cv.split();assert abs(state['actual_cvars'][k]-float(v))<1e-4
 # Use a private copy of starts, never replace the original 86-map catalog.
 spdir=out/'starts';spdir.mkdir(exist_ok=True);source=R/'catalog'/f'{slug}.json';sp=json.loads(source.read_text());valid=[x for x in sp.get('positions',[]) if all(isinstance(x.get(k),(int,float)) for k in ['x','y','z'])]
 if mc['recover_start']:
  recovery=engine.query(cl.request,"""
starts=[];meshes=[]
for a in unreal.GameplayStatics.get_all_actors_of_class(w,unreal.Actor):
 try:
  cls=a.get_class().get_name();name=a.get_name()
  if 'PlayerStart' in cls:
   p=a.get_actor_location();starts.append(dict(name='recovered_'+name,x=p.x,y=p.y,z=p.z))
  elif isinstance(a,unreal.StaticMeshActor):
   c,e=a.get_actor_bounds(True)
   if e.x>300 and e.y>300 and e.z<3000:
    meshes.append(dict(name='surface_'+name,x=c.x,y=c.y,z=c.z+e.z+300,area=e.x*e.y))
 except Exception:pass
RESULT.update(starts=starts,mesh_candidates=sorted(meshes,key=lambda x:-x['area'])[:16])
""")
  (out/'start_recovery_candidates.json').write_text(json.dumps(recovery,indent=2))
  candidates=recovery.get('starts',[])+valid+recovery.get('mesh_candidates',[])
  valid=[]
  for c in candidates[:28]:
   ground=engine.ground_z(cl.request,[(c['x'],c['y'])],c['z']+400)[0]
   if ground is not None:valid.append(dict(name=c['name'],x=c['x'],y=c['y'],z=ground+170))
  state['start_recovered']=True
  if not valid:raise RuntimeError('No candidate start has traceable ground; original assets/starts need repair')
 sp['positions']=valid;(spdir/f'{slug}.json').write_text(json.dumps(sp,indent=2));F.SP_DIR=spdir
 # Choose a content-rich connected REGION without hard clipping it to the core mask.
 # The original centerline, reroute and engine validation implementations remain unchanged.
 original=C.build_network
 if mc['policy']=='content_region':
  def content_network(nav,core_only=False,min_clear_cm=None,veto_xy=None,veto_radius_cm=150):
   sel=SC.best_core_region(nav,verbose=True)
   if sel['stats']['core_m2']<=0:
    g,rep,reg=original(nav,core_only=False,min_clear_cm=min_clear_cm,veto_xy=veto_xy,veto_radius_cm=veto_radius_cm);rep['region_policy']='longest_fallback_no_core';return g,rep,reg
   g,rep=C.build_centrelines(nav,sel['region'],min_clear_cm=min_clear_cm or 90,veto_xy=veto_xy,veto_radius_cm=veto_radius_cm)
   rep.update(region_policy='content_region_unclipped',core_m2=sel['stats']['core_m2'],selected_region_z_cm=sel['stats']['median_z_cm'],min_clear_cm=min_clear_cm or 90,veto_radius_cm=veto_radius_cm)
   return g,rep,sel['region']
  C.build_network=content_network
 # Phase instrumentation only; all original algorithms and gates are retained.
 def phased(obj,name,phase):
  fn=getattr(obj,name)
  def wrapped(*args,**kwargs):
   save(phase)
   return fn(*args,**kwargs)
  setattr(obj,name,wrapped)
 phased(C,'plan','生成完整路线与动作优化')
 phased(engine,'validate_path','引擎碰撞与近角点检查')
 phased(F,'probe_route','深度画面抽查')
 for attempt in range(2):
  task=json.loads((R/'template.json').read_text());task.update(task_id=f'routecheck_20260916_{slug}_{attempt}',map_id=mc['map_id'],seed=mc['seed']+attempt*1000,target_passes=1,max_duration_s=72000,allow_short_episode=True,min_frames=240,route_region_policy=mc['policy']);task['body']['ground_clearance_cm']=mc['ground_clearance_cm']
  taskpath=out/f'task_{attempt}.json';taskpath.write_text(json.dumps(task,indent=2));save(f'完整路线预检 · 第 {attempt+1} 次')
  try:
   fp,fz=F.freeze(task,ucv=ucv,out_dir=out/'frozen')
   failed=[k for k,v in fz['pre_gates'].items() if not v];kept=fz.get('head_clearance_pruning',{}).get('kept_fraction',0)
   q={'attempt':attempt,'seed':task['seed'],'frozen':str(fp),'frames':fz['frames'],'duration_s':fz['duration_s'],'failed_gates':failed,'pre_gates':fz['pre_gates'],'kept_fraction':kept,'road_network':fz['road_network'],'pruning':fz['head_clearance_pruning'],'collision':fz['collision'],'depth_probe':fz['depth_probe'],'coverage':fz['coverage'],'reroutes':fz['reroutes'],'policy':mc['policy'],'ground_clearance_cm':mc['ground_clearance_cm']}
   state['attempts'].append(q);save('路线检查结果已写入')
   if not failed:
    state.update(state='geometry_pass' if kept>=.75 else 'coverage_review',chosen_attempt=attempt)
    break
   state['state']='route_failed'
   if not set(failed).issubset({'collision_free','depth_probe_clear','action_mix_in_band'}):break
  except (TimeoutError,ConnectionError) as e:
   raise
  except Exception as e:
   traceback.print_exc();state['attempts'].append({'attempt':attempt,'error':type(e).__name__+': '+str(e)});state['state']='route_failed';break
 state['note']='Geometry preflight only. Frame path and sampled depth gates are checked; map-wide/visual coverage and TAA image detail still need preview review. No long video recorded.'
except Exception as e:
 traceback.print_exc();state.update(state='timeout' if isinstance(e,TimeoutError) else 'failed',error=type(e).__name__+': '+str(e),failure_kind='rpc_timeout' if isinstance(e,RpcTimeout) else ('query_timeout' if isinstance(e,TimeoutError) else 'worker_error'))
finally:
 save('本地图验证结束')
