import os,sys,json,time,signal,traceback,hashlib
from pathlib import Path
from types import SimpleNamespace
"""Measure one map: route freeze + engine gates + core size. Driven by the pull queue.

argv: <task.json> <out_dir>. The task carries map_id, seed, policy, ground clearance, and the
optional per-map knobs (region_z_cm, pitch_limit_up_deg) the 09-16 validation added.

Everything the route needs is the same code the 86-map validation ran. What is new here is that
the CORE SIZE is measured from the navmesh in the same pass, in a finally block, so a map whose
route fails still comes back with the number the ranking page needs - that split is exactly why
8 of the maps in this queue have a route result and no core size.
"""
R=Path(__file__).resolve().parent
mc=json.loads(Path(sys.argv[1]).read_text());slug=mc['slug']
P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline')
sys.path[:0]=[str(P),'/home/ubuntu/WM-Unreal-data-collection']
os.environ['MAP']=mc['map_id'];os.environ['PORT']='9208'
import unrealcv,engine,freeze_coverage as F,coverage as C,survey_core as SC
import numpy as np
from runtime_guard import bounded_call,RpcTimeout
out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
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
 # The FIRST request is bounded far more generously than the rest. On an image that has never
 # opened this level, the game thread is still compiling shaders and building the asset registry
 # when the port starts answering, and a 180 s bound turns a cold map into a fake editor wedge:
 # 13 of 42 maps "timed out" that way on the first pass.
 assert bounded_call('UnrealCV first status',original_request,'vget /unrealcv/status',timeout_s=int(os.environ.get('FIRST_RPC_S',1500))),'Empty UnrealCV status'
 cl.request=request
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
# Landscape levels (Landscapes Pack, terrains) have neither a PlayerStart nor a StaticMeshActor
# big enough: the ground is a LandscapeProxy. Sample a 3x3 grid over each landscape's bounds at
# three heights, so the downward trace (40 m reach) finds the surface on hilly terrain too.
lands=[]
for a in unreal.GameplayStatics.get_all_actors_of_class(w,unreal.LandscapeProxy):
 try:
  c,e=a.get_actor_bounds(True)
  for fx in (-0.5,0.0,0.5):
   for fy in (-0.5,0.0,0.5):
    for zz,tag in ((c.z+e.z+300,'top'),(c.z+400,'mid'),(c.z-e.z*0.5+400,'low')):
     lands.append(dict(name=f'landscape_{a.get_name()}_{fx:+.1f}_{fy:+.1f}_{tag}',x=c.x+fx*e.x,y=c.y+fy*e.y,z=zz,area=e.x*e.y))
 except Exception:pass
# Last resort for levels built from many small tiles (ModularCourtyard: no PlayerStart, no single
# mesh over 3 m): a 3x3 grid over the union bounds of every static mesh, traced from the top.
allc=[];tops=[]
for a in unreal.GameplayStatics.get_all_actors_of_class(w,unreal.StaticMeshActor):
 try:
  c,e=a.get_actor_bounds(True);allc.append((c.x-e.x,c.y-e.y,c.x+e.x,c.y+e.y));tops.append(c.z+e.z)
 except Exception:pass
union=[]
if allc:
 x0=min(b[0] for b in allc);y0=min(b[1] for b in allc);x1=max(b[2] for b in allc);y1=max(b[3] for b in allc);zt=sorted(tops)[int(len(tops)*0.9)]
 for fx in (0.25,0.5,0.75):
  for fy in (0.25,0.5,0.75):
   union.append(dict(name=f'union_{fx}_{fy}',x=x0+(x1-x0)*fx,y=y0+(y1-y0)*fy,z=zt+300,area=(x1-x0)*(y1-y0)))
RESULT.update(starts=starts,mesh_candidates=sorted(meshes,key=lambda x:-x['area'])[:16],landscape_candidates=lands[:60],union_candidates=union)
""")
  (out/'start_recovery_candidates.json').write_text(json.dumps(recovery,indent=2))
  candidates=recovery.get('starts',[])+valid+recovery.get('mesh_candidates',[])+recovery.get('landscape_candidates',[])+recovery.get('union_candidates',[])
  valid=[]
  for c in candidates[:90]:
   if len(valid)>=12:break
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
 # Optional per-map region height. Dungeon_Demo_00's longest region and CommandCenter's
 # content region were both the ROOF of the building (probe frames: black void / open sky at
 # pitch 0, floor trace 6.8 m below the route). Regions whose median z is not within 150 cm of
 # the hint are removed before either policy chooses; the policies themselves are unchanged.
 if mc.get('region_z_cm') is not None:
  chosen=C.build_network
  def by_height(nav,*a,**k):
   import numpy as _np
   hint=float(mc['region_z_cm']);before=len(nav.regions)
   nav.regions=[r for r in nav.regions if abs(float(_np.median(nav.verts[nav.wtris[r]][:,:,2]))-hint)<=150.0]
   print(f'[worker] region_z_cm {hint:.0f}: {len(nav.regions)} of {before} regions kept',flush=True)
   if not nav.regions:raise RuntimeError(f'no navmesh region within 150 cm of z={hint:.0f}')
   return chosen(nav,*a,**k)
  C.build_network=by_height
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
  task=json.loads((R/'template.json').read_text());task.update(task_id=f'routecheck_20260916_{slug}_{attempt}',map_id=mc['map_id'],seed=mc['seed']+attempt*1000,target_passes=1,max_duration_s=72000,allow_short_episode=True,min_frames=240,route_region_policy=mc['policy']);task['body']['ground_clearance_cm']=mc['ground_clearance_cm'];task['camera']['pitch_limit_up_deg']=float(mc.get('pitch_limit_up_deg') or task['camera']['pitch_limit_up_deg'])  # the key is present and null for most maps, so a .get default never fires
  taskpath=out/f'task_{attempt}.json';taskpath.write_text(json.dumps(task,indent=2));save(f'完整路线预检 · 第 {attempt+1} 次')
  try:
   fp,fz=F.freeze(task,ucv=ucv,out_dir=out/'frozen')
   failed=[k for k,v in fz['pre_gates'].items() if not v];kept=fz.get('head_clearance_pruning',{}).get('kept_fraction',0)
   q={'attempt':attempt,'seed':task['seed'],'frozen':str(fp),'frames':fz['frames'],'duration_s':fz['duration_s'],'failed_gates':failed,'pre_gates':fz['pre_gates'],'kept_fraction':kept,'road_network':fz['road_network'],'pruning':fz['head_clearance_pruning'],'collision':fz['collision'],'depth_probe':fz['depth_probe'],'coverage':fz['coverage'],'reroutes':fz['reroutes'],'policy':mc['policy'],'ground_clearance_cm':mc['ground_clearance_cm'],'pitch_limit_up_deg':task['camera']['pitch_limit_up_deg'],'region_z_cm':mc.get('region_z_cm')}
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


# ---- core size, measured from this run's navmesh whatever the route did ----------------------
# survey_core.measure reads the exported .bin/.json and nothing from the engine, so it runs even
# after a failed route. If freeze never got as far as exporting, export one here: a start point
# that projects is enough for the navmesh, and the navmesh is enough for the core.
try:
    navdir = out / 'frozen' / 'nav'
    if not (navdir / f'{slug}.bin').exists() and 'cl' in dir() and cl.isconnected():
        save('导航网导出（路线未完成，仍测核心）')
        navdir.mkdir(parents=True, exist_ok=True)
        cands = json.loads((out / 'starts' / f'{slug}.json').read_text())['positions'] if (out / 'starts' / f'{slug}.json').exists() else []
        if cands:
            c = cands[0]
            boot = engine.nav_ensure(cl.request, (c['x'], c['y']), c['z'])
            if boot.get('ok'):
                engine.nav_export(cl.request, str(navdir / f'{slug}.bin'), str(navdir / f'{slug}.json'))
    if (navdir / f'{slug}.bin').exists():
        save('核心大小')
        rec = SC.measure(slug, navdir, out / 'core.png')
        state['core'] = {k: rec[k] for k in ('core_m2', 'core_strict_m2', 'region_walkable_m2', 'region_safe_m2',
                                             'core_frac_of_region_safe', 'core_clearance_median_m',
                                             'core_margin_to_export_bounds_m', 'export_extent_m', 'nav_regions')}
        print('CORE', slug, rec['core_m2'], 'm2', flush=True)
    else:
        state['core_error'] = 'no navmesh export from this run'
except Exception as e:
    traceback.print_exc();state['core_error'] = f'{type(e).__name__}: {str(e)[:200]}'
state['elapsed_s'] = round(time.time() - t0, 1)
(out / 'result.tmp').write_text(json.dumps(state, ensure_ascii=False, indent=2));(out / 'result.tmp').replace(out / 'result.json')
print('RESULT', slug, state.get('state'), 'core', (state.get('core') or {}).get('core_m2'), flush=True)
