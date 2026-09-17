import os,json,sys
from pathlib import Path
os.environ['OPENCV_IO_ENABLE_OPENEXR']='1'
import cv2,numpy as np
cv2.setNumThreads(4)
root=Path(__file__).resolve().parent;name=sys.argv[1];mr=root/name
tags=[p.parent.name for p in mr.glob('*/done.json')];assert 'baseline' in tags
def ep(tag):return Path(json.loads((mr/tag/'done.json').read_text())['episode'])
def states(tag):return [json.loads(x) for x in (ep(tag)/'engine_states.jsonl').read_text().splitlines()]
def im(tag,i):
 a=cv2.imread(str(ep(tag)/'rgb'/f'{i:06d}.jpg'));assert a is not None;return a
base=states('baseline');groups={'static':[],'moving':[],'turn':[]}
for i,(a,b) in enumerate(zip(base,base[1:])):
 pos=np.linalg.norm(np.array(a['actual_location'])-b['actual_location']);rot=np.max(np.abs((np.array(a['actual_rotation_pyr'])-b['actual_rotation_pyr']+180)%360-180))
 key='turn' if rot>.05 else ('moving' if pos>.5 else 'static');groups[key].append(i)
groups={k:[v[i] for i in np.linspace(0,len(v)-1,min(len(v),16 if k!='static' else 8),dtype=int)] if v else [] for k,v in groups.items()}
roi=json.loads((root/'building_regions.json').read_text()).get(name)
opening=list(range(*(roi.get('frames',[1,191,8]) if roi else [1,191,8])));ids=sorted(set(opening+[i for v in groups.values() for i in v]));gx,gy=np.meshgrid(np.arange(1280),np.arange(720));full=(gx>20)&(gx<1260)&(gy>20)&(gy<700);flow={};mask={}
for i in ids:
 a=im('baseline',i);b=im('baseline',i+1);flow[i]=cv2.calcOpticalFlowFarneback(cv2.cvtColor(a,cv2.COLOR_BGR2GRAY),cv2.cvtColor(b,cv2.COLOR_BGR2GRAY),None,.5,4,21,3,7,1.5,0)
 z=cv2.imread(str(ep('baseline')/'depth'/f'{i:06d}.exr'),cv2.IMREAD_UNCHANGED)[...,2]
 mask[i]={'full':full,'far':full&(z>35)&(z<300)}
 if roi:
  x0,y0,x1,y1,z0,z1=roi['bounds'];mask[i]['building']=(gx>=x0)&(gx<x1)&(gy>=y0)&(gy<y1)&(z>z0)&(z<z1)
result={'map':name,'note':'Same baseline flow and depth masks; motion residual includes occlusion/flow error. Edge and Laplacian measures include noise and cannot establish detail preservation alone. Compare videos. Repeated world simulation is not bit-identical.','pair_groups':groups,'variants':{}}
for tag in tags:
 ss=states(tag);assert len(ss)==len(base)
 assert all(a['actual_location']==b['actual_location'] and a['actual_rotation_pyr']==b['actual_rotation_pyr'] for a,b in zip(base,ss))
 assert len(list((ep(tag)/'rgb').glob('*.jpg')))==len(ss)==len(list((ep(tag)/'depth').glob('*.exr')))
 rows=[]
 for i in ids:
  a=im(tag,i);b=im(tag,i+1);f=flow[i];warp=cv2.remap(b,(gx+f[...,0]).astype('float32'),(gy+f[...,1]).astype('float32'),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT)
  diff=np.abs(a.astype('float32')-warp.astype('float32')).mean(axis=2);raw=np.abs(a.astype('float32')-b.astype('float32')).mean(axis=2);gray=cv2.cvtColor(a,cv2.COLOR_BGR2GRAY)
  lap=cv2.Laplacian(gray,cv2.CV_32F);edge=cv2.magnitude(cv2.Sobel(gray,cv2.CV_32F,1,0),cv2.Sobel(gray,cv2.CV_32F,0,1))
  row={'frame':i}
  for region,m in mask[i].items():
   if m.sum()<100:row[region]=None;continue
   row[region]={'residual':float(diff[m].mean()),'sparkle20':float((diff[m]>20).mean()*100),'raw_diff':float(raw[m].mean()),'edge':float(edge[m].mean()),'lap':float(lap[m].var()),'luma':float(gray[m].mean())}
  rows.append(row)
 def agg(indices,region):
  rs=[q[region] for q in rows if q['frame'] in indices and q[region]]
  return {k:float(np.median([v[k] for v in rs])) for k in rs[0]} if rs else None
 depths=[]
 for i in np.linspace(0,len(ss)-1,8,dtype=int):
  z=cv2.imread(str(ep(tag)/'depth'/f'{i:06d}.exr'),cv2.IMREAD_UNCHANGED);assert z.shape==(720,1280,4) and np.isfinite(z).all();z=z[...,2];assert ((z==-1)|(z>0)).all();depths.append({'frame':int(i),'valid_fraction':float((z>0).mean())})
 building_moving=[i for i in opening if base[i]['actual_location']!=base[i+1]['actual_location']]
 building_static=[i for i in opening if base[i]['actual_location']==base[i+1]['actual_location'] and base[i]['actual_rotation_pyr']==base[i+1]['actual_rotation_pyr']]
 result['variants'][tag]={'frames':len(ss),**{k:agg(v,'full') for k,v in groups.items()},'far':{k:agg(v,'far') for k,v in groups.items()},'building':{'moving':agg(building_moving,'building'),'static':agg(building_static,'building'),'region':roi} if roi else None,'depth_samples':depths,'rows':rows}
p=mr/'metrics.json.tmp';p.write_text(json.dumps(result,indent=2));p.replace(mr/'metrics.json');print(json.dumps({k:{x:v for x,v in q.items() if x not in ['rows','depth_samples']} for k,q in result['variants'].items()},indent=2))
