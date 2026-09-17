from pathlib import Path
import time,shutil,subprocess,json
r=Path(__file__).resolve().parent;s=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/general-stability');previous=None;worker=None;processed={};cfg=json.loads((r/'config.json').read_text())
while not (r/'STOP_WATCH').exists():
 if (r/'progress.json').exists():
  shutil.copy2(r/'progress.json',s/'progress.tmp');(s/'progress.tmp').replace(s/'progress.json')
 if worker and worker.poll() is not None:
  assert worker.returncode==0,'Metrics worker failed';worker=None
 for name in cfg['maps']:
  count=len(list((r/name).glob('*/done.json')))
  if worker is None and count>=2 and count!=processed.get(name):
   worker=subprocess.Popen(['/opt/pytorch/bin/python',str(r/'measure.py'),name],stdout=(r/(name+'_metrics.log')).open('w'),stderr=subprocess.STDOUT);processed[name]=count
 signature=(len(list(r.glob('*/*/done.json'))),tuple(p.stat().st_mtime_ns for p in sorted(r.glob('*/metrics.json'))))
 if signature!=previous:
  subprocess.run(['python3',str(r/'publish.py')],check=True);previous=signature
 time.sleep(3)
