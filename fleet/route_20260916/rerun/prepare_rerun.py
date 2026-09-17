"""Second pass of the 20260916 route validation: bundle the rerun set for the nine stopped workers.

Mirrors prepare.py, with three differences that are the whole point of the pass:
  * the manifest now carries 45 cm ground clearance / a 10 deg look-up cap where the plan says so,
  * run.py is invoked with --retry for every assigned slug (the workers still hold pass-1 results),
  * the per-map worker limit is 14400 s (three maps hit the 7200 s limit while still making progress).
"""
import json,shutil,sys
from pathlib import Path
F=Path('/home/ubuntu/ue_route_fleet_20260916');R=Path('/home/ubuntu/ue_route_validation_20260916');P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline');S=F/'bundle'
WORKERS=['00','02','03','04','05','06','07','08','09']          # 01 is still finishing pass 1
manifest=json.loads((R/'manifest.json').read_text());plan=json.loads((F/'rerun/rerun_plan.json').read_text())
todo=[mc for mc in manifest if plan.get(mc['slug'],{}).get('category') in ('clearance45','lookup10','retry')]
def prev_elapsed(slug):
    p=R/'maps'/slug/'result.json'
    return (json.loads(p.read_text()).get('total_elapsed_s') or 0) if p.exists() else 0
todo.sort(key=lambda mc:-prev_elapsed(mc['slug']))             # slowest first so no worker ends with a 4 h tail
assignments={w:[] for w in WORKERS}
for i,mc in enumerate(todo):assignments[WORKERS[i%len(WORKERS)]].append(mc)
assert sum(len(v) for v in assignments.values())==len(todo)
(F/'assignments.json').write_text(json.dumps(assignments,ensure_ascii=False,indent=2))
for sub in ['runtime','pipeline/cpp','catalog']:(S/sub).mkdir(parents=True,exist_ok=True)
for name in ['worker.py','runtime_guard.py','template.json','baseline_template.json','source_hashes.json','test_runtime_guard.py']:
    shutil.copy2(R/name,S/'runtime'/name)
s=(R/'run.py').read_text().replace("from runtime_guard import editor_ready","from runtime_guard import editor_ready\nCTR_PID=os.environ['CTR_PID']")
s=s.replace("'enroot','exec','22964'","'enroot','exec',CTR_PID").replace("Path('/proc/22964/root/home/ue4/simworld/Saved/Logs/gym_citynav.log')","Path('/proc')/CTR_PID/'root/home/ue4/simworld/Saved/Logs/gym_citynav.log'")
assert 'STARTUP_S=600;WORKER_MAX_S=7200;' in s
s=s.replace('STARTUP_S=600;WORKER_MAX_S=7200;','STARTUP_S=1200;WORKER_MAX_S=14400;');(S/'runtime/run.py').write_text(s)
s=(S/'runtime/worker.py').read_text().replace("source=P.parent.parent/'batch_inference/start_positions'/f'{slug}.json'","source=R/'catalog'/f'{slug}.json'");(S/'runtime/worker.py').write_text(s)
sv=(S/'supervise.py').read_text()
old="p=subprocess.Popen(['python3','-u',str(R/'run.py')],"
if old in sv:
    sv=sv.replace(old,"retry=[a for m in json.loads((R/'manifest.json').read_text()) for a in ('--retry',m['slug'])];p=subprocess.Popen(['python3','-u',str(R/'run.py'),*retry],")
    (S/'supervise.py').write_text(sv)
assert "'--retry',m['slug']" in (S/'supervise.py').read_text()
for p in P.glob('*.py'):shutil.copy2(p,S/'pipeline'/p.name)
for p in (P/'cpp').iterdir():
    if p.is_file() and p.suffix in {'.cpp','.h','.py'}:shutil.copy2(p,S/'pipeline/cpp'/p.name)
for p in (P.parent.parent/'batch_inference/start_positions').glob('*.json'):shutil.copy2(p,S/'catalog'/p.name)
shutil.copy2(P.parent/'launch_ue_fast.sh',S/'launch_ue_fast.sh');shutil.copy2(F/'assignments.json',S/'assignments.json')
print('Prepared',len(todo),'maps across',{w:len(v) for w,v in assignments.items()})
for w,v in assignments.items():print(w,[f"{m['slug'][5:34]}:{m['ground_clearance_cm']}" for m in v])
