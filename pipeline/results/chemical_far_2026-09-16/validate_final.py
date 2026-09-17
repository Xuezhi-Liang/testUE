import os,json
from pathlib import Path
os.environ['OPENCV_IO_ENABLE_OPENEXR']='1'
import cv2,numpy as np
root=Path(__file__).resolve().parent;tag='zzfinal_taa2';d=root/tag;ep=d/'episode'/('chemical_far_'+tag)
st=json.loads((d/'engine_status.json').read_text());assert st['finished'] and st['frame']==1440 and st['write_failures']==0 and st['pending_writes']==0
assert st['history_mode']==4 and st['temporal_aa_enabled'] and st['warmup_frames']==32 and st['rgb_render_width']==2560 and st['rgb_render_height']==1440
cv=json.loads((d/'actual_cvars.json').read_text());assert cv['r.AntiAliasingMethod']==2 and cv['r.Fog']==1 and cv['r.VolumetricFog']==1 and cv['r.MipMapLODBias']==0
s=[json.loads(x) for x in (ep/'engine_states.jsonl').read_text().splitlines()];assert len(s)==1440
pose=max(max(abs(a-b) for a,b in zip(q['actual_location'],q['desired_location'])) for q in s)
rot=max(max(abs((a-b+180)%360-180) for a,b in zip(q['actual_rotation_pyr'],q['desired_rotation_pyr'])) for q in s)
assert pose<.01 and rot<.01
for directory,ext in [('rgb','jpg'),('depth','exr')]:
 names=sorted(q.name for q in (ep/directory).glob('*.'+ext));assert names==[f'{i:06d}.{ext}' for i in range(1440)]
rows=[]
for i in [0,1,35,100,191,360,600,720,900,1100,1300,1439]:
 z=cv2.imread(str(ep/'depth'/f'{i:06d}.exr'),cv2.IMREAD_UNCHANGED);assert z.shape==(720,1280,4) and np.isfinite(z).all()
 z=z[...,2];assert np.all((z==-1)|(z>0));im=cv2.imread(str(ep/'rgb'/f'{i:06d}.jpg'));assert im.shape==(720,1280,3)
 rows.append({'frame':i,'valid_fraction':float((z>0).mean()),'minimum_m':float(z[z>0].min()),'maximum_m':float(z.max())})
v=cv2.VideoCapture(str(ep/'rgb.mp4'));n=int(v.get(cv2.CAP_PROP_FRAME_COUNT));fps=v.get(cv2.CAP_PROP_FPS);assert n==1440 and fps==24;v.release()
r={'frames':n,'fps':fps,'duration_s':n/fps,'position_error_cm':pose,'rotation_error_deg':rot,'native':st,'aa_method':cv['r.AntiAliasingMethod'],'depth_samples':rows,'scope':'Render validation only. No new route/dataset acceptance; native depth remains unfiltered. Capture input retained descriptive taa=tsr from the source but its explicit cvar override and actual runtime AA method are 2 (TAA). Saved optional template has consistent taa=taa.'}
(root/'final_validation.json').write_text(json.dumps(r,indent=2));print(json.dumps({k:v for k,v in r.items() if k not in ['depth_samples','native']}))
