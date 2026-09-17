"""Diagnostic replay of the full frozen route using the selected production template."""
import os,sys,json,hashlib,math
from pathlib import Path
os.environ['OPENCV_IO_ENABLE_OPENEXR']='1'
sys.path[:0]=['/home/ubuntu/UE5-Agent-Data/revisit_pipeline','/home/ubuntu/WM-Unreal-data-collection']
import engine,capture_engine as cape
import cv2,numpy as np
root=Path('/home/ubuntu/ue_flicker_20260916/full_validation');root.mkdir(exist_ok=True)
src=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/frozen/Game_HwaseongHaenggung_Maps_Demo__nested_out_and_back__age35__seed25__batch7_hwaseong.json')
fz=json.loads(src.read_text());fz['diagnostic_source']={'path':str(src),'file_sha256':hashlib.sha256(src.read_bytes()).hexdigest()};fz['episode_id']='hwaseong_temporal_full';fz['task']['render']=json.loads(Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline/tasks/longvideo_template.json').read_text())['render'];fz['task']['render']['skylight_factor']=4
fz.pop('sha256',None);fz['sha256']=hashlib.sha256(json.dumps(fz,indent=1).encode()).hexdigest();f=root/'frozen.json';f.write_text(json.dumps(fz,indent=1));os.environ['MAP']=fz['map_id']
ucv=engine.connect(1280,720,timeout=180);assert ucv
ep=Path(cape.capture(f,out_root=root/'episode',ucv=ucv))
st=engine.simworld(ucv.client.request,'get_capture_status','');(root/'engine_status.json').write_text(json.dumps(st,indent=2))
states=[json.loads(x) for x in (ep/'engine_states.jsonl').read_text().splitlines()]
assert st['finished'] and st['frame']==fz['frames']==len(states) and st['write_failures']==0
assert len(list((ep/'rgb').glob('*.jpg')))==len(states)==len(list((ep/'depth').glob('*.exr')))
ids=np.linspace(0,len(states)-1,32,dtype=int).tolist();sample=[states[i] for i in ids]
body='''
rows=[]
for s in SAMPLE:
    p=s['actual_location']; pitch,yaw,roll=s['actual_rotation_pyr']
    rot=unreal.Rotator(pitch=pitch,yaw=yaw,roll=roll)
    fw=unreal.MathLibrary.get_forward_vector(rot)
    right=unreal.MathLibrary.get_right_vector(rot)
    up=unreal.MathLibrary.get_up_vector(rot)
    # Centre pixel (640,360) is half a pixel off the 1280x720 optical centre.
    # At 90 degree horizontal FOV, fx=fy=640. No jitter in the depth capture.
    v=fw+right*(0.5/640.0)-up*(0.5/640.0)
    hit=seg(*p,p[0]+v.x*100000,p[1]+v.y*100000,p[2]+v.z*100000)
    if hit:
        imp=hit['impact']; hit['planar_m']=sum((imp[i]-p[i])*x for i,x in enumerate([fw.x,fw.y,fw.z]))/100
    rows.append({'frame':s['frame_id'],'hit':hit})
RESULT['rows']=rows
'''.replace('SAMPLE',repr(sample))
r=engine.query(ucv.client.request,body,timeout=120)
assert 'rows' in r,r
for q in r['rows']:
 a=cv2.imread(str(ep/'depth'/f"{q['frame']:06d}.exr"),cv2.IMREAD_UNCHANGED)
 assert a.shape[:2]==(720,1280) and np.isfinite(a).all()
 d=float(a[360,640,2]);q['recorded_depth_m']=d;q['absolute_difference_m']=abs(d-q['hit']['planar_m']) if d>0 and q['hit'] else None
r.update(frames=len(states),rgb_files=len(list((ep/'rgb').glob('*.jpg'))),depth_files=len(list((ep/'depth').glob('*.exr'))),pose_tracking=json.loads((ep/'capture_summary.json').read_text())['pose_tracking'],note='Visibility collision and rendered geometry can differ. Ray comparison is independent validation, not a requirement that all assets have collision.')
(root/'validation.json').write_text(json.dumps(r,indent=2));print('FULL_VALIDATION_DONE',json.dumps(r),flush=True)
