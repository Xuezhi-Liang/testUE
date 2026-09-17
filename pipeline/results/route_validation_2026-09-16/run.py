import os,sys,json,time,signal,subprocess,fcntl,traceback,argparse
from pathlib import Path
from runtime_guard import editor_ready
R=Path(__file__).resolve().parent
args=argparse.ArgumentParser();args.add_argument('--retry',action='append',default=[]);args=args.parse_args()
lock=(R/'runner.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
(R/'runner.pid').write_text(str(os.getpid()))
manifest=json.loads((R/'manifest.json').read_text());assert set(args.retry)<={m['slug'] for m in manifest}
terminal={'geometry_pass','coverage_review','route_failed','failed','blocked_asset','timeout'}
active=None;editor=None;worker=None;stopping=False;draining=False;started=time.time();queue_error=None
STARTUP_S=600;WORKER_MAX_S=7200;PROGRESS_IDLE_S=1800

def write(p,d):
 tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2));tmp.replace(p)
def read(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {}
def heartbeat():
 write(R/'queue.json',dict(pid=os.getpid(),version=2,state='stopping' if stopping else ('draining' if draining else 'running'),started_epoch=started,updated_epoch=time.time(),active=active,total=len(manifest)))
def stop(p):
 if p is None:return
 try:os.killpg(p.pid,signal.SIGTERM)
 except ProcessLookupError:return
 try:p.wait(timeout=30)
 except subprocess.TimeoutExpired:
  try:os.killpg(p.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  p.wait()
def on_signal(*_):
 global stopping
 stopping=True
def on_drain(*_):
 global draining
 draining=True
signal.signal(signal.SIGTERM,on_signal);signal.signal(signal.SIGINT,on_signal);signal.signal(signal.SIGUSR1,on_drain)
def archive(out):
 previous=read(out/'result.json');history=previous.get('retry_history',[])
 if not any(out.iterdir()):return history
 dest=R/'history'/out.name/time.strftime('%Y%m%dT%H%M%S',time.gmtime());dest.mkdir(parents=True,exist_ok=False)
 for p in list(out.iterdir()):p.rename(dest/p.name)
 history.append(dict(archive=str(dest),state=previous.get('state'),error=previous.get('error'),total_elapsed_s=previous.get('total_elapsed_s')))
 return history
try:
 for mc in manifest:
  if stopping or draining:break
  slug=mc['slug'];out=R/'maps'/slug;out.mkdir(parents=True,exist_ok=True);rp=out/'result.json';previous=read(rp)
  if previous.get('state') in terminal and slug not in args.retry:continue
  active=slug;heartbeat();history=archive(out) if previous else []
  if not mc['map_id']:
   write(rp,dict(slug=slug,state='blocked_asset',error='Map asset not found in the mounted project',elapsed_s=0));continue
  for startup_attempt in range(2):
   if stopping:break
   mt=time.time();print('START',slug,'startup_attempt',startup_attempt+1,flush=True)
   if subprocess.run(['pgrep','-x','UnrealEditor'],stdout=subprocess.DEVNULL).returncode==0:
    raise RuntimeError('An editor is already running; refusing to start a second editor')
   write(rp,dict(slug=slug,map_id=mc['map_id'],state='starting',phase='等待地图运行就绪',started_epoch=mt,attempts=[],retry_history=history))
   stage='startup';retryable=False
   try:
    elog=out/'editor.log'
    with elog.open('w') as ef:
     editor=subprocess.Popen(['enroot','exec','22964','env','GAME_MAP='+mc['map_id'],'UNREALCV_PORT=9208','bash','/home/ubuntu/WM-Unreal-data-collection/local_run/launch_ue_fast.sh'],stdout=ef,stderr=subprocess.STDOUT,start_new_session=True)
    write(out/'process.json',dict(editor_pid=editor.pid,started_epoch=mt))
    native_log=Path('/proc/22964/root/home/ue4/simworld/Saved/Logs/gym_citynav.log')
    deadline=time.time()+STARTUP_S
    while time.time()<deadline:
     if stopping:raise InterruptedError('Queue stopped')
     if editor.poll() is not None:raise RuntimeError('Editor exited during startup')
     log_text=elog.read_text(errors='replace')
     if native_log.exists() and native_log.stat().st_mtime>=mt:
      log_text+='\n'+native_log.read_text(errors='replace')
     if editor_ready(log_text):break
     heartbeat();time.sleep(2)
    else:raise TimeoutError(f'Editor did not finish PIE startup within {STARTUP_S}s; no client connected')
    time.sleep(2)
    readiness=dict(ready_epoch=time.time(),startup_s=round(time.time()-mt,1),method='UnrealCV listener + PIE startup completion in fresh native UE log (stdout may be buffered)',client_connected=False)
    write(out/'readiness.json',readiness)
    stage='worker';wlog=out/'worker.log'
    with wlog.open('w') as wf:
     worker=subprocess.Popen(['enroot','exec','22964','env','PYTHONUNBUFFERED=1','OPENCV_IO_ENABLE_OPENEXR=1','python3',str(R/'worker.py'),slug],stdout=wf,stderr=subprocess.STDOUT,start_new_session=True)
    write(out/'process.json',dict(editor_pid=editor.pid,worker_pid=worker.pid,started_epoch=mt))
    deadline=time.time()+WORKER_MAX_S;last_progress=time.time();last_size=-1
    while worker.poll() is None:
     if stopping:raise InterruptedError('Queue stopped')
     size=wlog.stat().st_size
     if size!=last_size:last_size=size;last_progress=time.time()
     if time.time()>deadline:raise TimeoutError(f'Route preflight exceeded {WORKER_MAX_S}s total worker limit')
     if time.time()-last_progress>PROGRESS_IDLE_S:raise TimeoutError(f'No route-preflight log progress for {PROGRESS_IDLE_S}s')
     if editor.poll() is not None:raise RuntimeError('Editor exited during route preflight')
     heartbeat();time.sleep(3)
    result=read(rp)
    if worker.returncode or result.get('state') not in terminal:
     raise RuntimeError('Worker exited without a completed result; see worker.log')
    retryable=result.get('failure_kind') in {'rpc_timeout','query_timeout'} or (result.get('state')=='failed' and not result.get('world_ready'))
   except Exception as e:
    traceback.print_exc();result=read(rp)
    result.update(state='interrupted' if stopping else ('timeout' if isinstance(e,TimeoutError) else 'failed'),error=type(e).__name__+': '+str(e),failure_kind=stage+'_timeout' if isinstance(e,TimeoutError) else stage+'_error')
    write(rp,result);retryable=stage=='startup'
   finally:
    stop(worker);worker=None;stop(editor);editor=None
    native_log=Path('/proc/22964/root/home/ue4/simworld/Saved/Logs/gym_citynav.log')
    if native_log.exists() and native_log.stat().st_mtime>=mt:
     (out/'native_editor.log').write_bytes(native_log.read_bytes())
    result=read(rp);result.update(total_elapsed_s=round(time.time()-mt,1),finished_epoch=time.time(),retry_history=history,runner_version=2);write(rp,result)
    print('DONE',slug,result.get('state'),result['total_elapsed_s'],flush=True);heartbeat()
   if stopping or not retryable or startup_attempt==1:break
   print('RETRY_FRESH_EDITOR',slug,flush=True);history=archive(out)
except Exception as e:
 queue_error=type(e).__name__+': '+str(e);traceback.print_exc()
finally:
 stop(worker);stop(editor)
 write(R/'queue.json',dict(pid=os.getpid(),version=2,state='failed' if queue_error else ('stopped' if stopping or draining else 'finished'),error=queue_error,started_epoch=started,updated_epoch=time.time(),active=None,total=len(manifest)))
