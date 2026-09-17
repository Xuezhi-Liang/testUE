"""Single-connection adaptive UE render diagnostics; job JSON arrives between captures."""
import os,sys,json,time,signal,hashlib
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=['/home/ubuntu/UE5-Agent-Data/revisit_pipeline','/home/ubuntu/WM-Unreal-data-collection']
import engine,capture_engine as cape,unrealcv
root=Path(__file__).resolve().parent;jobs=root/'jobs';jobs.mkdir(exist_ok=True)
orig=json.loads(Path('/home/ubuntu/ue_latest_two_maps_20260916_0639/chemical/frozen.json').read_text());os.environ['MAP']=orig['map_id']
def timeout(*a):raise TimeoutError('UnrealCV handshake timeout')
signal.signal(signal.SIGALRM,timeout);signal.alarm(90);cl=unrealcv.Client(('127.0.0.1',9208));cl.connect();assert cl.isconnected();assert cl.request('vget /unrealcv/status');cl.request('vrun setres 1280x720w');signal.alarm(0);ucv=SimpleNamespace(client=cl)
keys=list(json.loads((root/'initial_cvars.json').read_text()))+['r.AntiAliasingMethod','r.TSR.Visualize','r.Lumen.DiffuseIndirect.Allow','r.Lumen.Reflections.Allow','r.Lumen.ScreenProbeGather.DownsampleFactor','r.Lumen.ScreenProbeGather.Temporal.MaxFramesAccumulated','r.MipMapLODBias','ShowFlag.Specular','r.MaxAnisotropy','r.TSR.ShadingRejection.SampleCount']
query='RESULT.update({k:unreal.SystemLibrary.get_console_variable_float_value(k) for k in '+repr(keys)+'})'
def read():return engine.query(cl.request,query)
initial=read();(root/'adaptive_defaults.json').write_text(json.dumps(initial,indent=2));print('ADAPTIVE_READY',initial,flush=True)
while not (jobs/'STOP').exists():
 pending=[p for p in sorted(jobs.glob('*.json')) if not (root/p.stem/'done.json').exists()]
 if not pending:time.sleep(1);continue
 job=pending[0];cfg=json.loads(job.read_text());tag=job.stem;out=root/tag;out.mkdir(exist_ok=False);n=cfg.get('frames',288)
 fz=json.loads(json.dumps(orig));fz['poses']=fz['poses'][:n];fz['frames']=n;fz['duration_s']=n/fz['fps'];fz['episode_id']='chemical_far_'+tag;fz['task']['duration_s']=fz['duration_s'];fz['diagnostic_only']=True;fz['source_route']='latest chemical full frozen route, prefix preserved'
 r=fz['task']['render'];r['skylight_factor']=1.5;r['supersample']=cfg.get('ss',2)
 cv=dict(initial);cv.update(cfg.get('cvars',{}));r['cvars']=[f'{k} {v:g}' for k,v in cv.items()]
 fz.pop('sha256',None);fz['sha256']=hashlib.sha256(json.dumps(fz,indent=1).encode()).hexdigest();f=out/'frozen.json';f.write_text(json.dumps(fz,indent=1));print('JOB',tag,cfg,flush=True)
 ep=Path(cape.capture(f,out_root=out/'episode',ucv=ucv))
 st=engine.simworld(cl.request,'get_capture_status','');assert st['finished'] and st['frame']==n and st['write_failures']==0
 (out/'engine_status.json').write_text(json.dumps(st,indent=2));actual=read();(out/'actual_cvars.json').write_text(json.dumps(actual,indent=2));assert all(abs(actual[k]-v)<1e-4 for k,v in cfg.get('cvars',{}).items())
 (out/'done.json').write_text(json.dumps({'episode':str(ep),'frames':n}));print('DONE',tag,flush=True)
print('ADAPTIVE_STOP',flush=True)
