"""Replay a full validated route with the exact latest render profile, publishing progress."""
import os,sys,json,hashlib,time,shutil
from pathlib import Path
os.environ['OPENCV_IO_ENABLE_OPENEXR']='1'
sys.path[:0]=['/home/ubuntu/UE5-Agent-Data/revisit_pipeline','/home/ubuntu/WM-Unreal-data-collection']
import engine,capture_engine as cape
import cv2,numpy as np
ROOT=Path(__file__).resolve().parent
SITE=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/latest-config-20260916')
name=sys.argv[1]
suffix={'downtown':'Game_Downtown_West_Maps_Demo_Environment__nested_out_and_back__age35__seed1__batch5_downtown_west','chemical':'Game_ChemicalPlantEnv_Maps_Map_ChemicalPlant_2__nested_out_and_back__age35__seed20__batch7_chemplant2'}[name]
src=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/frozen')/(suffix+'.json')
def status(**kw):
 p=SITE/'status.json';d=json.loads(p.read_text());d['maps'][name].update(kw);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2));os.replace(tmp,p)
def run():
 out=ROOT/name;out.mkdir(exist_ok=False)
 fz=json.loads(src.read_text());fz['source_route']={'path':str(src),'file_sha256':hashlib.sha256(src.read_bytes()).hexdigest()};fz['episode_id']=name+'_latest_20260916';fz['task']['render']=json.loads(Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline/tasks/longvideo_template.json').read_text())['render']
 fz.pop('sha256',None);fz['sha256']=hashlib.sha256(json.dumps(fz,indent=1).encode()).hexdigest();f=out/'frozen.json';f.write_text(json.dumps(fz,indent=1));os.environ['MAP']=fz['map_id']
 # No experimental environment overrides: the template is the profile actually being tested.
 for key in ['CAPTURE_HISTORY','CAPTURE_SUPERSAMPLE','CAPTURE_CVARS','EXPOSURE_MODE','EXPOSURE_AUTO','EXPOSURE_BIAS_EV','SKYLIGHT_FACTOR','LIGHTING_MODE','LIGHTING_RIG','LE_SHADOW','LE_HIGHLIGHT','LE_DETAIL','AE_SPEED','AE_RANGE']:
  assert key not in os.environ,(key,os.environ[key])
 assert fz['frames']==2844 and fz['fps']==24
 status(state='连接本机 UE，随后自动校准补光',frame=0,total=fz['frames'])
 # One connection for this editor lifetime; synchronous setup avoids the legacy
 # wrapper's two fire-and-forget startup commands. Its request timeout is ignored
 # by the installed client, so bound the handshake with an actual process alarm.
 import signal,unrealcv
 from types import SimpleNamespace
 def handshake_timeout(*a):raise TimeoutError('UnrealCV handshake exceeded 90 seconds')
 signal.signal(signal.SIGALRM,handshake_timeout);signal.alarm(90)
 client=unrealcv.Client(('127.0.0.1',9208));client.connect()
 assert client.isconnected()
 assert client.request('vget /unrealcv/status')
 client.request('vrun setres 1280x720w')
 signal.alarm(0);ucv=SimpleNamespace(client=client)
 status(state="正在按地图自动校准补光")
 ep=out/'episode'/fz['episode_id'];last=[0]
 def progress():
  now=time.time()
  if now-last[0]<8:return
  last[0]=now
  count=len(list((ep/'rgb').glob('*.jpg')))
  if count>5:
   poster=ep/'rgb'/f'{max(0,count-4):06d}.jpg'
   if poster.exists():shutil.copy2(poster,SITE/(name+'_preview.jpg'))
  status(state='正在录制' if count else '正在自动校准补光 / 预热',frame=count,preview=(name+'_preview.jpg') if count>5 else None)
 ep=Path(cape.capture(f,out_root=out/'episode',ucv=ucv,on_poll=progress))
 st=engine.simworld(ucv.client.request,'get_capture_status','');(out/'engine_status.json').write_text(json.dumps(st,indent=2))
 status(state='录制完成，校验 RGB / 深度 / 位姿',frame=st['frame'])
 states=[json.loads(x) for x in (ep/'engine_states.jsonl').read_text().splitlines()]
 assert st['finished'] and st['frame']==fz['frames']==len(states) and st['write_failures']==0
 assert len(list((ep/'rgb').glob('*.jpg')))==len(states)==len(list((ep/'depth').glob('*.exr')))
 assert [s['frame_id'] for s in states]==list(range(len(states)))
 assert st['history_mode']==4 and st['temporal_aa_enabled'] and st['warmup_frames']==32
 assert st['rgb_render_width']==2560 and st['rgb_render_height']==1440
 assert st['exposure_manual'] is False and st['exposure_bias_ev']==0
 summary=json.loads((ep/'capture_summary.json').read_text());assert summary['pose_tracking']['pos_error_cm_max']==0 and summary['pose_tracking']['yaw_error_deg_max']==0
 sample=[states[i] for i in np.linspace(0,len(states)-1,32,dtype=int).tolist()]
 body='''
rows=[]
for s in SAMPLE:
 p=s['actual_location'];pitch,yaw,roll=s['actual_rotation_pyr']
 rot=unreal.Rotator(pitch=pitch,yaw=yaw,roll=roll)
 fw=unreal.MathLibrary.get_forward_vector(rot);right=unreal.MathLibrary.get_right_vector(rot);up=unreal.MathLibrary.get_up_vector(rot)
 v=fw+right*(0.5/640.0)-up*(0.5/640.0)
 h=seg(*p,p[0]+v.x*100000,p[1]+v.y*100000,p[2]+v.z*100000)
 if h:h['planar_m']=sum((h['impact'][i]-p[i])*x for i,x in enumerate([fw.x,fw.y,fw.z]))/100
 rows.append({'frame':s['frame_id'],'hit':h})
RESULT['rows']=rows
'''.replace('SAMPLE',repr(sample))
 traces=engine.query(ucv.client.request,body,timeout=120);assert 'rows' in traces,traces
 hist=[]
 for q in traces['rows']:
  i=q['frame'];a=cv2.imread(str(ep/'depth'/f'{i:06d}.exr'),cv2.IMREAD_UNCHANGED);rgb=cv2.imread(str(ep/'rgb'/f'{i:06d}.jpg'))
  assert a.shape[:2]==rgb.shape[:2]==(720,1280) and np.isfinite(a).all()
  depth=a[...,2];assert ((depth==-1)|(depth>0)).all()
  d=float(depth[360,640]);q['recorded_depth_m']=d;q['absolute_difference_m']=abs(d-q['hit']['planar_m']) if d>0 and q['hit'] else None
  gray=cv2.cvtColor(rgb,cv2.COLOR_BGR2GRAY);hist.append({'frame':i,'luma':float(gray.mean()),'black_pct':float((gray<=5).mean()*100),'blown_pct':float((gray>=250).mean()*100)})
 diffs=[q['absolute_difference_m'] for q in traces['rows'] if q['absolute_difference_m'] is not None]
 report={'map':fz['map_id'],'episode':str(ep),'frames':len(states),'duration_s':len(states)/fz['fps'],'runtime':st,'render':fz['task']['render'],'pose_tracking':summary['pose_tracking'],'lighting':summary['lighting'],'image_samples':hist,'depth_traces':traces['rows'],'trace_summary':{'comparable':len(diffs),'within_15cm':sum(x<.15 for x in diffs),'max_error_cm':max(diffs)*100 if diffs else None},'note':'Collision and visible geometry can differ; trace mismatches are retained. This is a recording test, not full dataset acceptance.'}
 (out/'report.json').write_text(json.dumps(report,indent=2));shutil.copy2(out/'report.json',SITE/(name+'_report.json'));shutil.copy2(ep/'rgb.mp4',SITE/(name+'.mp4'));shutil.copy2(ep/'rgb/000100.jpg',SITE/(name+'_poster.jpg'))
 calib=summary['lighting'].get('calibration') or {}
 status(state='完成',frame=len(states),video=name+'.mp4',poster=name+'_poster.jpg',report=name+'_report.json',duration_s=len(states)/fz['fps'],factor=calib.get('chosen_factor'),calibration_bar_missed=calib.get('quality_bar_missed'),pose_error_cm=summary['pose_tracking']['pos_error_cm_max'],trace_summary=report['trace_summary'],mean_luma=float(np.median([h['luma'] for h in hist])),black_pct=float(np.median([h['black_pct'] for h in hist])),blown_pct=float(np.median([h['blown_pct'] for h in hist])))
 print('DONE',name,json.dumps(report['trace_summary']),flush=True)
try:run()
except Exception as e:
 status(state='遇到问题，正在检查',error=str(e));raise
