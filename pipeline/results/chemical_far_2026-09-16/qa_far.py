"""Fixed user-selected left tower region, first 8 s; same masks/pairs in every variant."""
import os,sys,json
from pathlib import Path
os.environ['OPENCV_IO_ENABLE_OPENEXR']='1'
import cv2,numpy as np
root=Path(__file__).resolve().parent
base=root/'baseline/episode/chemical_far_baseline'
def img(ep,i):
 a=cv2.imread(str(ep/'rgb'/f'{i:06d}.jpg'));assert a is not None;return a
bs=[json.loads(x) for x in (base/'engine_states.jsonl').read_text().splitlines()]
ids=list(range(1,191,4));flow={};masks={};gx,gy=np.meshgrid(np.arange(1280),np.arange(720));roi=(gx<500)&(gy<360)
for i in ids:
 a=img(base,i);b=img(base,i+1)
 z=cv2.imread(str(base/'depth'/f'{i:06d}.exr'),cv2.IMREAD_UNCHANGED)[...,2]
 masks[i]=roi&(z>35)&(z<80)
 # Shared flow from the baseline prevents a smoother candidate gaining by easier flow estimation.
 ga=cv2.cvtColor(a,cv2.COLOR_BGR2GRAY);gb=cv2.cvtColor(b,cv2.COLOR_BGR2GRAY)
 flow[i]=cv2.calcOpticalFlowFarneback(ga,gb,None,.5,4,21,3,7,1.5,0)
result={'region':'First 8 seconds; x<500,y<360 and baseline depth 35..80m, chosen from user pointing at left distant tower.','note':'Flow residual is a proxy including flow error/occlusions. Shared baseline flow and masks. Laplacian sharpness includes noise; inspect cropped video.','variants':{}}
if (root/'far_metrics.json').exists():result['variants'].update(json.loads((root/'far_metrics.json').read_text())['variants'])
for tag in sys.argv[1:]:
 ep=root/tag/'episode'/('chemical_far_'+tag);s=[json.loads(x) for x in (ep/'engine_states.jsonl').read_text().splitlines()];assert len(s)>=192
 assert all(a['actual_location']==b['actual_location'] and a['actual_rotation_pyr']==b['actual_rotation_pyr'] for a,b in zip(bs,s))
 rows=[]
 for i in ids:
  a=img(ep,i);b=img(ep,i+1);f=flow[i];m=masks[i]
  warped=cv2.remap(b,(gx+f[...,0]).astype('float32'),(gy+f[...,1]).astype('float32'),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT)
  diff=np.abs(a.astype('float32')-warped.astype('float32')).mean(axis=2)[m]
  raw=np.abs(a.astype('float32')-b.astype('float32')).mean(axis=2)[m]
  g=cv2.cvtColor(a,cv2.COLOR_BGR2GRAY);moving=bs[i]['actual_location']!=bs[i+1]['actual_location'] or bs[i]['actual_rotation_pyr']!=bs[i+1]['actual_rotation_pyr']
  rows.append({'frame':i,'moving':moving,'pixels':int(m.sum()),'residual':float(diff.mean()),'sparkle10_pct':float((diff>10).mean()*100),'sparkle20_pct':float((diff>20).mean()*100),'raw_diff':float(raw.mean()),'sharpness':float(cv2.Laplacian(g,cv2.CV_64F)[m].var()),'luma':float(g[m].mean())})
 def agg(rs):return {k:float(np.median([q[k] for q in rs])) for k in ['residual','sparkle10_pct','sparkle20_pct','raw_diff','sharpness','luma']}
 result['variants'][tag]={'frames':len(s),'moving':agg([q for q in rows if q['moving']]),'static':agg([q for q in rows if not q['moving']]),'pairs':rows}
(root/'far_metrics.tmp').write_text(json.dumps(result,indent=2));(root/'far_metrics.tmp').replace(root/'far_metrics.json');print(json.dumps({k:{x:v for x,v in q.items() if x!='pairs'} for k,q in result['variants'].items()},indent=2))
