import json,subprocess,concurrent.futures,hashlib,time
from pathlib import Path
F=Path(__file__).resolve().parent;rows=json.loads((F/'live_instances.json').read_text())
body="""from pathlib import Path
import json,hashlib,subprocess
r=Path('/home/ubuntu/ue_route_validation_20260916');f=Path('/home/ubuntu/ue_route_fleet_20260916');q=json.loads((r/'queue.json').read_text());results=[json.loads(p.read_text()) for p in (r/'maps').glob('*/result.json')]
hashes={n:hashlib.sha256((r/n).read_bytes()).hexdigest() for n in ['worker.py','runtime_guard.py','template.json']}
editors=subprocess.run(['pgrep','-x','UnrealEditor'],capture_output=True,text=True).stdout.split()
print(json.dumps(dict(queue_state=q['state'],active=q.get('active'),worlds_verified=sum(bool(d.get('world_ready')) for d in results),editors=len(editors),hashes=hashes,errors=[d.get('error') for d in results if d.get('state') in ['failed','timeout']],compiled='Result: Succeeded' in (f/'module_build.log').read_text())))
"""
command="python3 - <<'PY'\n"+body+"\nPY"
def check(x):
 p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=6','ubuntu@'+x['ip'],command],capture_output=True,text=True,timeout=20)
 return {'worker':x['worker'],'instance_id':x['id'],**json.loads(p.stdout)}
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:checks=list(pool.map(check,rows))
expected={n:hashlib.sha256((F/'bundle/runtime'/n).read_bytes()).hexdigest() for n in ['worker.py','runtime_guard.py','template.json']}
assert len(checks)==10 and all(c['compiled'] and c['worlds_verified']>0 and c['editors']<=1 and c['hashes']==expected for c in checks),checks
(F/'fleet_validation.json').write_text(json.dumps(dict(checked_epoch=time.time(),workers=checks,identical_worker_guard_template=True),indent=2))
for c in checks:print(c['worker'],c['queue_state'],'verified worlds',c['worlds_verified'],'editors',c['editors'],'errors',c['errors'])
print('All ten compiled, verified live UE worlds and matched current code/template hashes')
