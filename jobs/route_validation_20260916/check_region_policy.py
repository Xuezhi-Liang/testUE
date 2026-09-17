import ast,json,sys
from pathlib import Path
R=Path(__file__).resolve().parent;P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline');sys.path.insert(0,str(P))
import coverage as C,survey_core as SC,plan_navmesh as pnm
fn=next(n for n in ast.walk(ast.parse((R/'worker.py').read_text())) if isinstance(n,ast.FunctionDef) and n.name=='content_network');ns=dict(C=C,SC=SC,original=C.build_network);exec(compile(ast.Module(body=[fn],type_ignores=[]),str(R/'worker.py'),'exec'),ns)
results=[]
for slug in ['Game_TokyoStylizedEnvironment_Maps_Tokyo','Game_ChemicalPlantEnv_Maps_Map_ChemicalPlant_2']:
 root=P/'frozen/nav';nav,_=pnm.load(str(root/f'{slug}.bin'),str(root/f'{slug}.json'))
 g,rep,region=ns['content_network'](nav,min_clear_cm=90);assert g.number_of_edges()>0
 assert rep['region_policy']=='content_region_unclipped';assert rep['min_clear_cm']==90
 results.append(dict(slug=slug,policy=rep['region_policy'],roads=g.number_of_edges(),centreline_m=rep['centreline_m'],core_m2=rep['core_m2'],note='Offline API/graph smoke only; cached navmesh, not an engine gate result'))
 print(results[-1],flush=True)
(R/'region_policy_smoke.json').write_text(json.dumps(results,indent=2))
