#!/usr/bin/env python3
"""Compare aligned capture episodes for motion shimmer, static noise and data integrity.

Optical-flow residual includes flow errors and disocclusions; it is a proxy, not a proof of
flicker-free images. Inspect the synchronized videos too. Sharpness/brightness are reported to
expose apparent improvements achieved by blur or relighting. Depth is compared numerically.
"""
import argparse,json,os
from pathlib import Path
os.environ.setdefault('OPENCV_IO_ENABLE_OPENEXR','1')
import cv2
import numpy as np

def pose_states(ep):
    return [json.loads(x) for x in (ep/'engine_states.jsonl').read_text().splitlines()]

def image(ep,i):
    a=cv2.imread(str(ep/'rgb'/f'{i:06d}.jpg'))
    if a is None: raise ValueError(f'missing RGB {ep}/{i}')
    return a

def pairs(states, moving):
    out=[]
    for i,(a,b) in enumerate(zip(states,states[1:])):
        delta=np.linalg.norm(np.array(a['actual_location'])-b['actual_location'])
        rot=np.abs((np.array(a['actual_rotation_pyr'])-b['actual_rotation_pyr']+180)%360-180).max()
        selected = (delta>.5 or rot>.05) if moving else (delta<.01 and rot<.01)
        if selected:out.append(i)
    return out

def sample(ids,n):
    return [ids[i] for i in np.linspace(0,len(ids)-1,min(n,len(ids)),dtype=int)] if ids else []

def measure(ep,indices,static):
    residual=[];sparkle=[];noise=[];sharp=[];luma=[];black=[];blown=[]
    for i in indices:
        a=image(ep,i);b=image(ep,i+1)
        ga=cv2.cvtColor(a,cv2.COLOR_BGR2GRAY);gb=cv2.cvtColor(b,cv2.COLOR_BGR2GRAY)
        flow=cv2.calcOpticalFlowFarneback(ga,gb,None,.5,4,21,3,7,1.5,0)
        h,w=ga.shape;gx,gy=np.meshgrid(np.arange(w),np.arange(h))
        warp=cv2.remap(b,(gx+flow[...,0]).astype('float32'),(gy+flow[...,1]).astype('float32'),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT)
        d=np.abs(a.astype('float32')-warp.astype('float32')).mean(axis=2)[20:-20,20:-20]
        residual.append(float(d.mean()));sparkle.append(float((d>20).mean()*100))
        sharp.append(float(cv2.Laplacian(ga,cv2.CV_64F).var()));luma.append(float(ga.mean()));black.append(float((ga<=5).mean()*100));blown.append(float((ga>=250).mean()*100))
    for i in static:
        noise.append(float(np.abs(image(ep,i).astype('float32')-image(ep,i+1).astype('float32')).mean()))
    median=lambda v:float(np.median(v)) if v else None
    return dict(moving_pairs=len(indices),static_pairs=len(static),motion_residual_mean=median(residual),motion_sparkle_pct=median(sparkle),static_diff=median(noise),sharpness=median(sharp),mean_luma=median(luma),near_black_pct=median(black),blown_pct=median(blown))

def compare(base,candidates,n=40):
    bs=pose_states(base);ids=sample(pairs(bs,True),n);static=sample(pairs(bs,False),n)
    result={'baseline':str(base),'metric_note':'Flow-compensated residual includes optical-flow error and disocclusion. Review footage for ghosting and lost detail.','episodes':{}}
    for ep in [base,*candidates]:
        st=pose_states(ep);assert len(st)==len(bs),(ep,'frame count differs')
        assert [s['frame_id'] for s in st]==list(range(len(st)))
        maxpos=max(float(np.linalg.norm(np.array(a['actual_location'])-b['actual_location'])) for a,b in zip(bs,st))
        maxrot=max(float(np.max(np.abs((np.array(a['actual_rotation_pyr'])-b['actual_rotation_pyr']+180)%360-180))) for a,b in zip(bs,st))
        assert maxpos<1e-4 and maxrot<1e-4,(ep,maxpos,maxrot)
        depths=[]
        for i in sample(list(range(len(bs))),12):
            a=cv2.imread(str(base/'depth'/f'{i:06d}.exr'),cv2.IMREAD_UNCHANGED);b=cv2.imread(str(ep/'depth'/f'{i:06d}.exr'),cv2.IMREAD_UNCHANGED)
            assert a is not None and b is not None and a.shape==b.shape
            assert np.isfinite(b).all()
            assert b.shape[:2]==image(ep,i).shape[:2]
            da=a[...,2] if a.ndim==3 else a
            db=b[...,2] if b.ndim==3 else b
            depths.append({'frame':i,'exact_equal':bool(np.array_equal(da,db)),
                'equal_pixel_pct':float((da==db).mean()*100),
                'valid_mask_agreement_pct':float(((da>0)==(db>0)).mean()*100),
                'max_abs_m':float(np.abs(da-db).max()),
                'p99_abs_m':float(np.percentile(np.abs(da-db),99))})
        m=measure(ep,ids,static);m.update(frames=len(st),position_difference_cm=maxpos,rotation_difference_deg=maxrot,depth_samples=depths)
        result['episodes'][ep.parent.name]=m
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('episodes',nargs='+',type=Path);p.add_argument('--out',required=True,type=Path);p.add_argument('--samples',type=int,default=40);a=p.parse_args();assert len(a.episodes)>=2
    d=compare(a.episodes[0],a.episodes[1:],a.samples);a.out.write_text(json.dumps(d,indent=2));print(json.dumps({k:{x:v for x,v in m.items() if x!='depth_samples'} for k,m in d['episodes'].items()},indent=2))
if __name__=='__main__':main()
