"""Render the frames the depth probe called 'all sky' on an indoor map, plus a few ordinary ones.

Runs inside the container against a live editor on 9208 (one client for the whole session).
For each frame: RGB PNG (level auto-exposure) and depth at 200 m and 1000 m range, so a frame
that is empty at 200 m but full at 1000 m is 'out of range', not 'no geometry'.
"""
import json,sys,os,time
from pathlib import Path
P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline');sys.path[:0]=[str(P),'/home/ubuntu/WM-Unreal-data-collection']
R=Path('/home/ubuntu/ue_route_validation_20260916');OUT=Path(__file__).resolve().parent/'sky'
slug=sys.argv[1];out=OUT/slug;out.mkdir(parents=True,exist_ok=True)
import unrealcv,engine
r=json.loads((R/'maps'/slug/'result.json').read_text());a=r['attempts'][-1];dp=a['depth_probe']
fz=json.loads(Path(a['frozen']).read_text());poses=fz['poses'];cam=fz.get('camera') or {'fov_deg':90.0}
frames=[('sky',e['frame']) for e in dp['all_sky_examples'][:5]]+[('thin',e['frame']) for e in dp.get('thin_depth_examples',[])[:2]]
skyset={f for _,f in frames}
normal=[i for i in range(0,len(poses),max(1,len(poses)//40)) if poses[i]['pitch_deg']==0 and i not in skyset][:3]
frames+=[('normal',i) for i in normal]
cl=unrealcv.Client(('127.0.0.1',9208));cl.connect();assert cl.isconnected()
req=cl.request;assert req('vget /unrealcv/status')
world=engine.query(req,"RESULT.update(world_path=w.get_path_name())");print('world',world,flush=True)
rows=[]
for kind,i in frames:
    p=poses[i];xyz=(p['x_cm'],p['y_cm'],p['z_cm'])
    rgb=engine.rgb_capture(req,xyz,p['yaw_deg'],p['pitch_deg'],cam['fov_deg'],640,360,str(out/f'{kind}_{i}.png'))
    d200=engine.depth_capture(req,xyz,p['yaw_deg'],p['pitch_deg'],cam['fov_deg'],320,180,str(out/f'{kind}_{i}_200.exr'),max_range_m=200.0)
    d1k=engine.depth_capture(req,xyz,p['yaw_deg'],p['pitch_deg'],cam['fov_deg'],320,180,str(out/f'{kind}_{i}_1000.exr'),max_range_m=1000.0)
    ground=engine.ground_z(req,[(p['x_cm'],p['y_cm'])],p['z_cm']+50)[0]
    row=dict(kind=kind,frame=i,pose=p,ground_z_cm=ground,rgb=rgb,depth_200=d200,depth_1000=d1k);rows.append(row)
    print(kind,i,'pitch',p['pitch_deg'],'ground',ground,'| 200m invalid',d200.get('invalid_fraction'),'min',d200.get('min'),'| 1000m invalid',d1k.get('invalid_fraction'),'min',d1k.get('min'),'max',d1k.get('max'),'| rgb p50',rgb.get('p50'),flush=True)
(out/'probe.json').write_text(json.dumps(dict(world=world,rows=rows),indent=1,default=str))
print('DONE',slug,flush=True)
