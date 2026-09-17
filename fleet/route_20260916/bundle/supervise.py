import json,time,os,subprocess,sys,signal
from pathlib import Path
W=sys.argv[1];F=Path('/home/ubuntu/ue_route_fleet_20260916');R=Path('/home/ubuntu/ue_route_validation_20260916');S3=f's3://pan-simworld/ue-route-validation/20260916/workers/{W}'
def aws(*a):
 p=subprocess.run(['aws','s3',*a,'--only-show-errors'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=600)
 if p.returncode:print('upload failed',p.stderr.decode()[-500:],flush=True)
 return p.returncode==0
def sync():
 ok=True
 for f in ['queue.json','runner.log']:
  if (R/f).exists():ok=aws('cp',str(R/f),S3+'/'+f) and ok
 if (R/'maps').exists():
  ok=aws('sync',str(R/'maps'),S3+'/maps','--exclude','*','--include','*/result.json','--include','*/readiness.json','--include','*/worker.log','--include','*/frozen/*') and ok
 return ok
with (R/'runner.log').open('a') as log:
 retry=[a for m in json.loads((R/'manifest.json').read_text()) for a in ('--retry',m['slug'])];p=subprocess.Popen(['python3','-u',str(R/'run.py'),*retry],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 while p.poll() is None:
  try:sync()
  except Exception as e:print(type(e).__name__,str(e),flush=True)
  time.sleep(15)
 for _ in range(3):
  try:
   if sync():break
  except Exception as e:print(type(e).__name__,str(e),flush=True)
  time.sleep(15)
 else:raise RuntimeError('Final result upload did not complete')
 # Full logs and retry history are uploaded once, after the queue is finished.
 aws('sync',str(R/'maps'),S3+'/maps','--exclude','*/starts/*','--exclude','*/frozen/*')
 if (R/'history').exists():aws('sync',str(R/'history'),S3+'/history')
 summary={'worker':W,'state':'finished','runner_exit':p.returncode,'finished_epoch':time.time()};(F/'finished.json').write_text(json.dumps(summary));aws('cp',str(F/'finished.json'),S3+'/finished.json')
