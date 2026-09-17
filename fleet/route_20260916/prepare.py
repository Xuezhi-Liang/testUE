from pathlib import Path
import json,shutil,tarfile,hashlib
F=Path(__file__).resolve().parent;R=Path('/home/ubuntu/ue_route_validation_20260916');P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline');S=F/'bundle';S.mkdir(exist_ok=True)
manifest=json.loads((R/'manifest.json').read_text());q=json.loads((R/'queue.json').read_text());excluded=[];todo=[]
for m in manifest:
 p=R/'maps'/m['slug']/'result.json';r=json.loads(p.read_text()) if p.exists() else {}
 if r.get('state') in {'geometry_pass','coverage_review'} or m['slug']==q.get('active'):excluded.append(m['slug'])
 else:todo.append(m)
assert len(excluded)==3 and len(todo)==83,(excluded,len(todo))
# Keep priority order, distribute an equal number of maps per host.
assignments={f'{i:02}':todo[i::10] for i in range(10)}
(F/'assignments.json').write_text(json.dumps(assignments,ensure_ascii=False,indent=2));(F/'excluded_local.json').write_text(json.dumps(excluded,indent=2))
for sub in ['runtime','pipeline/cpp','catalog']:(S/sub).mkdir(parents=True,exist_ok=True)
for name in ['worker.py','runtime_guard.py','template.json','baseline_template.json','source_hashes.json','test_runtime_guard.py']:
 shutil.copy2(R/name,S/'runtime'/name)
s=(R/'run.py').read_text().replace("from runtime_guard import editor_ready","from runtime_guard import editor_ready\nCTR_PID=os.environ['CTR_PID']")
s=s.replace("'enroot','exec','22964'","'enroot','exec',CTR_PID").replace("Path('/proc/22964/root/home/ue4/simworld/Saved/Logs/gym_citynav.log')","Path('/proc')/CTR_PID/'root/home/ue4/simworld/Saved/Logs/gym_citynav.log'")
# Cold workers can compile shaders; require readiness instead of a short fixed wait.
s=s.replace('STARTUP_S=600;','STARTUP_S=1200;');(S/'runtime/run.py').write_text(s)
s=(S/'runtime/worker.py').read_text().replace("source=P.parent.parent/'batch_inference/start_positions'/f'{slug}.json'","source=R/'catalog'/f'{slug}.json'");(S/'runtime/worker.py').write_text(s)
for p in P.glob('*.py'):shutil.copy2(p,S/'pipeline'/p.name)
for p in (P/'cpp').iterdir():
 if p.is_file() and p.suffix in {'.cpp','.h','.py'}:shutil.copy2(p,S/'pipeline/cpp'/p.name)
for p in (P.parent.parent/'batch_inference/start_positions').glob('*.json'):shutil.copy2(p,S/'catalog'/p.name)
shutil.copy2(P.parent/'launch_ue_fast.sh',S/'launch_ue_fast.sh');shutil.copy2(F/'assignments.json',S/'assignments.json')
print('Prepared',len(todo),'maps across',[len(v) for v in assignments.values()])
