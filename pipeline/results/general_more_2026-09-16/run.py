import json,subprocess,time,os,signal,sys
from pathlib import Path
root=Path(__file__).resolve().parent;cfg=json.loads((root/'config.json').read_text())
for name in (sys.argv[1:] or cfg['maps']):
 mc=cfg['maps'][name];fz=json.loads(Path(mc['source']).read_text());log=root/(name+'_editor.log')
 assert not subprocess.run(['pgrep','-x','UnrealEditor'],stdout=subprocess.DEVNULL).returncode==0,'Another editor is running'
 editor=subprocess.Popen(['enroot','exec','22964','env','GAME_MAP='+fz['map_id'],'UNREALCV_PORT=9208','bash','/home/ubuntu/WM-Unreal-data-collection/local_run/launch_ue_fast.sh'],stdout=log.open('w'),stderr=subprocess.STDOUT,start_new_session=True)
 try:
  deadline=time.time()+150
  while time.time()<deadline:
   assert editor.poll() is None,'Editor exited during startup'
   if 'Start listening on port 9208' in log.read_text(errors='replace'):break
   time.sleep(2)
  else:raise TimeoutError('UnrealCV listener never started')
  time.sleep(12)
  with (root/(name+'_capture.log')).open('w') as f:
   p=subprocess.run(['enroot','exec','22964','python3',str(root/'record.py'),name],stdout=f,stderr=subprocess.STDOUT,timeout=2400)
  assert p.returncode==0,'Capture failed: '+name
 finally:
  os.killpg(editor.pid,signal.SIGTERM)
  try:editor.wait(timeout=35)
  except subprocess.TimeoutExpired:os.killpg(editor.pid,signal.SIGKILL);editor.wait()
 print('EDITOR_CLOSED',name,flush=True)
