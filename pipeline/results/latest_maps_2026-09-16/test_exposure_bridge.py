"""Exercise the real capture entrypoint with fake engine IO, without touching UE."""
import sys,json,ast,os,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,'/home/ubuntu/UE5-Agent-Data/revisit_pipeline')
import capture_engine as c,lighting_calibrate,lighting_rig
src=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/frozen/Game_Downtown_West_Maps_Demo_Environment__nested_out_and_back__age35__seed1__batch5_downtown_west.json')
base=json.loads(src.read_text());args=[]
st={'ok':True,'total':1,'frame':1,'finished':True,'elapsed_s':1,'fps':1,'render_ms':1,'readback_ms':1,'write_ms':1,'history_mode':4,'temporal_aa_enabled':True,'warmup_frames':32}
def fake(req,fn,arg):
 if fn=='start_capture':args.append(ast.literal_eval('['+arg+']'))
 return st
with tempfile.TemporaryDirectory() as tmp,patch.object(c,'ensure_dynamic_sky',return_value={}),patch.object(lighting_calibrate,'calibrate',return_value={'chosen_factor':3,'chosen_ev':11.5}),patch.object(c.engine,'query',return_value={'ok':True}),patch.object(c.engine,'simworld',side_effect=fake),patch.object(c,'finalise',side_effect=lambda ep,*a,**kw:ep),patch.dict(os.environ,{},clear=True):
 for i,(mode,bias,env,want) in enumerate([('fixed',None,None,11.5),('auto_instant',None,None,0),('auto_instant',1,None,1),('fixed',7,None,7),('auto_instant',None,2,2)]):
  if env is None:os.environ.pop('EXPOSURE_BIAS_EV',None)
  else:os.environ['EXPOSURE_BIAS_EV']=str(env)
  fz=json.loads(json.dumps(base));fz['episode_id']=str(i);fz['task']['render']={'exposure':mode,'exposure_bias_ev':bias,'lighting':'fill','skylight_factor':'auto','history_mode':4,'supersample':2};p=Path(tmp)/f'{i}.json';p.write_text(json.dumps(fz));c.capture(p,Path(tmp)/'out',ucv=SimpleNamespace(client=SimpleNamespace(request=lambda *a:None)))
  assert args[-1][9]==want,(mode,args[-1]);assert args[-1][10]==(mode=='fixed')
  if env is None:assert 'EXPOSURE_BIAS_EV' not in os.environ
print('PASS: manual calibration is local, automatic exposure keeps level bias, explicit overrides survive (5 cases).')
