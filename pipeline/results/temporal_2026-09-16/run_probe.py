import os,sys,json,time,copy
from pathlib import Path
os.environ['OPENCV_IO_ENABLE_OPENEXR']='1'
sys.path[:0]=['/home/ubuntu/UE5-Agent-Data/revisit_pipeline','/home/ubuntu/WM-Unreal-data-collection']
import engine,capture_engine as cape
root=Path('/home/ubuntu/ue_flicker_20260916');name=os.environ.get('PROBE_MAP','hwaseong')
frozen={'hwaseong':'Game_HwaseongHaenggung_Maps_Demo__nested_out_and_back__age35__seed25__batch7_hwaseong','chemical':'Game_ChemicalPlantEnv_Maps_Map_ChemicalPlant_2__nested_out_and_back__age35__seed20__batch7_chemplant2'}[name]
src=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/frozen')/(frozen+'.json');fz=json.loads(src.read_text());fz['poses']=fz['poses'][:876];fz['frames']=len(fz['poses']);fz['duration_s']=fz['frames']/fz['fps'];fz['task']['duration_s']=fz['duration_s'];fz['episode_id']=name+'_probe';fz['task']['render']={'exposure':'auto_instant','lighting':'fill','skylight_factor':4 if name=='hwaseong' else 1,'local_exposure':{'shadow_scale':.65,'detail_strength':1},'history_mode':3,'supersample':2};fz['diagnostic_source']=str(src)
(root/name).mkdir(exist_ok=True);f=root/name/'frozen.json';f.write_text(json.dumps(fz));os.environ['MAP']=fz['map_id']
ucv=engine.connect(1280,720,timeout=180);assert ucv
variants=[('fresh_ss2',3,2,4),('temporal_ss1',4,1,4),('temporal_ss2',4,2,4),('taa_ss2',4,2,2),('temporal_fixed',4,2,4),('taa_fixed',4,2,2)]
if os.environ.get('PROBE_VARIANTS'):variants=[v for v in variants if v[0] in os.environ['PROBE_VARIANTS'].split(',')]
for tag,history,ss,aa in variants:
 os.environ['CAPTURE_HISTORY']=str(history);os.environ['CAPTURE_SUPERSAMPLE']=str(ss);os.environ['CAPTURE_CVARS']=f'r.AntiAliasingMethod {aa}'+(';'+';'.join(c+' 0' for c in ['r.Lumen.ScreenProbeGather.FixedJitterIndex','r.LumenScene.Radiosity.Temporal.FixedJitterIndex','r.Lumen.ReSTIRGather.FixedJitterIndex']) if tag.endswith('_fixed') else '')
 out=root/name/tag
 assert not out.exists(),out
 ep=cape.capture(f,out_root=out,ucv=ucv)
 st=engine.simworld(ucv.client.request,'get_capture_status','');(out/'engine_status.json').write_text(json.dumps(st,indent=2));print('PROBE_DONE',tag,st,flush=True)
print('ALL_PROBES_DONE',name,flush=True)
