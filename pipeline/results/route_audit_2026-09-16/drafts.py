from pathlib import Path
import subprocess,json,concurrent.futures,time
R=Path(__file__).resolve().parent
rows=json.loads((R/'audit.json').read_text())['maps']
todo=[x['slug'] for x in rows if x.get('core') and x['route_kind'] in ['none','short_route']]
(R/'drafts').mkdir(exist_ok=True)
def run(slug):
 f=R/'drafts'/f'{slug}.json'
 if f.exists():return json.loads(f.read_text())
 try:
  p=subprocess.run(['/opt/pytorch/bin/python',str(R/'draft_one.py'),slug],capture_output=True,text=True,timeout=180)
  if not f.exists():f.write_text(json.dumps({'slug':slug,'state':'draft_failed','error':p.stderr[-1000:] or f'exit {p.returncode}'}))
 except subprocess.TimeoutExpired:
  f.write_text(json.dumps({'slug':slug,'state':'draft_failed','error':'Offline planning exceeded 180 seconds; requires separate review, not a claim that the map is unusable.'}))
 return json.loads(f.read_text())
print('TOTAL',len(todo),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 fs=[pool.submit(run,s) for s in todo]
 for i,f in enumerate(concurrent.futures.as_completed(fs),1):
  q=f.result();print(i,len(todo),q['slug'],q['state'],q.get('error',''),flush=True)
  (R/'draft_progress.json').write_text(json.dumps({'done':i,'total':len(todo),'last':q['slug']}))
