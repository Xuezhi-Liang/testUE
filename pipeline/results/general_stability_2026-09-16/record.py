import os,sys,json,hashlib,signal,time,shutil
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=['/home/ubuntu/UE5-Agent-Data/revisit_pipeline','/home/ubuntu/WM-Unreal-data-collection']
import engine,capture_engine as cape,unrealcv
root=Path(__file__).resolve().parent;name=sys.argv[1]
cfg=json.loads((root/'config.json').read_text());mc=cfg['maps'][name];out=root/name;out.mkdir(exist_ok=True)
orig=json.loads(Path(mc['source']).read_text());os.environ['MAP']=orig['map_id']
def alarm(*a):raise TimeoutError('single-connection handshake timed out')
signal.signal(signal.SIGALRM,alarm);signal.alarm(90)
cl=unrealcv.Client(('127.0.0.1',9208));cl.connect();assert cl.isconnected();assert cl.request('vget /unrealcv/status');cl.request('vrun setres 1280x720w');signal.alarm(0)
keys=sorted(set(k for v in cfg['variants'].values() for k in v['cvars']))
query='RESULT.update({k:unreal.SystemLibrary.get_console_variable_float_value(k) for k in '+repr(keys)+'})'
initial=engine.query(cl.request,query);assert all(k in initial for k in keys),initial
(out/'defaults.json').write_text(json.dumps(initial,indent=2))
for tag,v in cfg['variants'].items():
 if (out/tag/'done.json').exists():continue
 d=out/tag;d.mkdir(exist_ok=False);fz=json.loads(json.dumps(orig));n=cfg['frames'];fz['poses']=fz['poses'][:n];assert len(fz['poses'])==n
 fz.update(frames=n,duration_s=n/fz['fps'],episode_id=name+'_'+tag,diagnostic_only=True,source_route=mc['source']);fz['task']['duration_s']=fz['duration_s']
 rend=json.loads(Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline/tasks/longvideo_template.json').read_text())['render'];rend.update(supersample=v['ss'],skylight_factor=mc['fill'],taa='taa' if v['cvars']['r.AntiAliasingMethod']==2 else 'tsr')
 cv=dict(initial);cv.update(v['cvars']);rend['cvars']=[f'{k} {x:g}' for k,x in cv.items()];fz['task']['render']=rend
 fz.pop('sha256',None);fz['sha256']=hashlib.sha256(json.dumps(fz,indent=1).encode()).hexdigest();p=d/'frozen.json';p.write_text(json.dumps(fz,indent=1))
 def progress():
  ep=d/'episode'/fz['episode_id'];count=len(list((ep/'rgb').glob('*.jpg')))
  state={'map':name,'variant':tag,'frames':count,'total':n,'state':'录制中'}
  q=root/'progress.tmp';q.write_text(json.dumps(state,ensure_ascii=False));q.replace(root/'progress.json')
 print('START',name,tag,flush=True)
 ep=Path(cape.capture(p,out_root=d/'episode',ucv=SimpleNamespace(client=cl),on_poll=progress))
 st=engine.simworld(cl.request,'get_capture_status','');actual=engine.query(cl.request,query)
 assert st['finished'] and st['frame']==n and st['write_failures']==0 and st['history_mode']==4 and st['temporal_aa_enabled'] and st['warmup_frames']==32
 assert st['rgb_render_width']==1280*v['ss'] and st['rgb_render_height']==720*v['ss']
 assert all(abs(actual[k]-x)<1e-4 for k,x in v['cvars'].items()),actual
 (d/'runtime.json').write_text(json.dumps({'native':st,'actual_cvars':actual,'render':rend},indent=2))
 (d/'done.json').write_text(json.dumps({'episode':str(ep),'frames':n}));print('DONE',name,tag,flush=True)
print('MAP_COMPLETE',name,flush=True)
