import os
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['OMP_NUM_THREADS']='1'
from pathlib import Path
import sys,json,time
import numpy as np
import cv2
cv2.setNumThreads(1)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection,LineCollection

R=Path(__file__).resolve().parent
P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline')
sys.path.insert(0,str(P))
import coverage as C
import plan_navmesh as N
slug=sys.argv[1];t=time.time();out=R/'drafts';out.mkdir(exist_ok=True)
try:
 nav,meta=N.load(str(P/'frozen/nav'/f'{slug}.bin'),str(P/'frozen/nav'/f'{slug}.json'))
 G,rep,region=C.build_network(nav,core_only=False,min_clear_cm=90)
 steps,left=C.coverage_walk(G,np.random.default_rng(8600))
 if left:raise RuntimeError(f'{left} roads left unwalked')
 poly,_=C.walk_polyline(G,steps)
 path,axis=C.resample(poly,step_cm=100)
 z=np.median(nav.verts[nav.wtris[region]][:,:,2])
 fig,ax=plt.subplots(figsize=(8,7),facecolor='#111b25');ax.set_facecolor('#111b25')
 ax.add_collection(PolyCollection(nav.verts[nav.wtris][:,:,:2]/100,facecolors='#303c47',edgecolors='none',rasterized=True))
 ax.add_collection(PolyCollection(nav.verts[nav.wtris[region]][:,:,:2]/100,facecolors='#677580',edgecolors='none',rasterized=True))
 ax.add_collection(LineCollection([np.asarray(d['poly'])[:,:2]/100 for _,_,d in G.edges(data=True)],colors='#78ecdf',linewidths=.8))
 ax.scatter(*(path[0,:2]/100),c='#76ff83',s=55,zorder=5);ax.scatter(*(path[-1,:2]/100),c='#ff8e9c',s=45,marker='x',zorder=5)
 ax.update_datalim(nav.verts[:,:2]/100);ax.autoscale_view();ax.set_aspect('equal');ax.tick_params(colors='white');ax.set_xlabel('UE X (m)',color='white');ax.set_ylabel('UE Y (m)',color='white');ax.set_title(slug.removeprefix('Game_').replace('_Maps_',' / ')+'\nOFFLINE DRAFT - collision / camera / depth NOT checked',color='white',fontsize=9);fig.tight_layout();fig.savefig(out/f'{slug}.png',dpi=125);plt.close(fig)
 data={'slug':slug,'state':'draft_ready','seed':8600,'method':'Existing build_network(core_only=False, min_clear_cm=90) and coverage_walk on cached navmesh; no UE, collision pruning, camera timing, depth probes or frame gates run. Not a frozen capture task.','network':rep,'nav_regions':len(nav.regions),'selected_region_z_cm':float(z),'selected_region_area_m2':float(nav.tri_areas(region).sum()/1e4),'walk_m':float(axis[-1]/100),'polyline_xy_cm':path[:,:2].tolist(),'roads_total':G.number_of_edges(),'unwalked_roads':left,'elapsed_s':round(time.time()-t,2)}
except Exception as e:
 data={'slug':slug,'state':'draft_failed','error':type(e).__name__+': '+str(e),'elapsed_s':round(time.time()-t,2)}
(out/f'{slug}.json').write_text(json.dumps(data,separators=(',',':')))
print(json.dumps({k:v for k,v in data.items() if k not in ['polyline_xy_cm','network']}),flush=True)
