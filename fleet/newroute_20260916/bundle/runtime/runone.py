"""Measure one map on this host: start an editor on it, run mapworker inside the container, stop.

    CTR_PID=<pid> python3 runone.py <task.json> <out_dir>

One editor per host and one UnrealCV connection per editor lifetime, both non-negotiable on this
build. The editor is restarted for every map: a map switch in a live editor keeps the previous
level's streaming state and navmesh, which is how you get a route planned against the wrong world.

Exit code is 0 whenever a result was written (including a recorded failure) and 1 if the map could
not be judged at all, which is what the queue uses to decide whether to retry on a fresh editor.
"""
import os, sys, json, time, signal, subprocess, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_guard import editor_ready

CTR = os.environ['CTR_PID']
P = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline')
LAUNCH = '/home/ubuntu/WM-Unreal-data-collection/local_run/launch_ue_fast.sh'
task = json.loads(Path(sys.argv[1]).read_text()); out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
STARTUP_S = int(os.environ.get('STARTUP_S', 1500))
WORKER_MAX_S = int(os.environ.get('WORKER_MAX_S', 7200))
IDLE_S = int(os.environ.get('PROGRESS_IDLE_S', 1800))
NATIVE = Path('/proc') / CTR / 'root/home/ue4/simworld/Saved/Logs/gym_citynav.log'
TERMINAL = {'geometry_pass', 'coverage_review', 'route_failed', 'failed', 'timeout', 'blocked_asset'}


def write(p, d):
    t = p.with_suffix('.tmp'); t.write_text(json.dumps(d, ensure_ascii=False, indent=2)); t.replace(p)


def read(p):
    try: return json.loads(p.read_text())
    except (OSError, ValueError): return {}


def stop(proc):
    if proc is None or proc.poll() is not None: return
    try: os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError: return
    try: proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait()


def kill_stray_editors():
    # by PID, never `pkill -f`: the pattern would match this process's own command line
    for pid in subprocess.run(['pgrep', '-x', 'UnrealEditor'], capture_output=True, text=True).stdout.split():
        try: os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError): pass
    time.sleep(3)


rp = out / 'result.json'
editor = worker = None
mt = time.time()
stage = 'startup'
try:
    kill_stray_editors()
    write(rp, dict(slug=task['slug'], map_id=task['map_id'], state='starting', phase='等待地图运行就绪',
                   started_epoch=mt, attempts=[]))
    elog = out / 'editor.log'
    with elog.open('w') as ef:
        editor = subprocess.Popen(['enroot', 'exec', CTR, 'env', 'GAME_MAP=' + task['map_id'],
                                   'UNREALCV_PORT=9208', 'bash', LAUNCH],
                                  stdout=ef, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + STARTUP_S
    while time.time() < deadline:
        if editor.poll() is not None: raise RuntimeError('Editor exited during startup')
        text = elog.read_text(errors='replace')
        if NATIVE.exists() and NATIVE.stat().st_mtime >= mt:
            text += '\n' + NATIVE.read_text(errors='replace')
        if editor_ready(text): break
        time.sleep(2)
    else:
        raise TimeoutError(f'Editor did not finish PIE startup within {STARTUP_S}s')
    # PIE being up is not the game thread being free. On a heavy map it keeps loading for minutes
    # afterwards, and a client that connects into that window gets its first request swallowed:
    # the server logs the connection and the thread, never a `Request: 0:vget /unrealcv/status`,
    # and answers nothing (CyberpunkVillage: 9812 meshes, 5.5 min from PIE to WorldController).
    # A map that connects after the log goes quiet has its status answered within a millisecond.
    # So wait for quiet rather than for a marker, and cap the wait so a chatty map still proceeds.
    QUIET_S = int(os.environ.get('QUIET_S', 45)); QUIET_MAX_S = int(os.environ.get('QUIET_MAX_S', 900))
    qdead = time.time() + QUIET_MAX_S; last_size = -1; last_change = time.time()
    while time.time() < qdead:
        size = elog.stat().st_size + (NATIVE.stat().st_size if NATIVE.exists() else 0)
        if size != last_size: last_size, last_change = size, time.time()
        elif time.time() - last_change >= QUIET_S: break
        if editor.poll() is not None: raise RuntimeError('Editor exited while settling')
        time.sleep(3)
    print(f'settled after {time.time() - mt:.0f}s from launch', flush=True)
    stage = 'worker'
    wlog = out / 'worker.log'
    with wlog.open('w') as wf:
        worker = subprocess.Popen(['enroot', 'exec', CTR, 'env', 'PYTHONUNBUFFERED=1', 'OPENCV_IO_ENABLE_OPENEXR=1',
                                   'python3', str(Path(__file__).resolve().parent / 'mapworker.py'),
                                   str(Path(sys.argv[1]).resolve()), str(out.resolve())],
                                  stdout=wf, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + WORKER_MAX_S; last = time.time(); size = -1
    while worker.poll() is None:
        s = wlog.stat().st_size
        if s != size: size, last = s, time.time()
        if time.time() > deadline: raise TimeoutError(f'exceeded the {WORKER_MAX_S}s budget for one map')
        if time.time() - last > IDLE_S: raise TimeoutError(f'no log progress for {IDLE_S}s')
        if editor.poll() is not None: raise RuntimeError('Editor exited during measurement')
        # The host-side `enroot exec` can outlive the editor it started, so polling it is not
        # enough: a UE crash inside the container then shows up as the RPC bound expiring twenty
        # minutes later instead of as a dead editor. Ask for the process itself.
        if not subprocess.run(['pgrep', '-x', 'UnrealEditor'], stdout=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError('UnrealEditor process is gone; it crashed or was killed')
        time.sleep(3)
    r = read(rp)
    if worker.returncode or r.get('state') not in TERMINAL:
        raise RuntimeError('mapworker exited without a completed result; see worker.log')
except Exception as e:
    traceback.print_exc()
    r = read(rp)
    r.update(state='timeout' if isinstance(e, TimeoutError) else 'failed',
             error=f'{type(e).__name__}: {e}', failure_kind=f'{stage}_{"timeout" if isinstance(e, TimeoutError) else "error"}')
    write(rp, r)
finally:
    stop(worker); stop(editor); kill_stray_editors()
    if NATIVE.exists() and NATIVE.stat().st_mtime >= mt:
        (out / 'native_editor.log').write_bytes(NATIVE.read_bytes())
    r = read(rp); r.update(total_elapsed_s=round(time.time() - mt, 1), finished_epoch=time.time()); write(rp, r)
    print('DONE', task['slug'], r.get('state'), r.get('total_elapsed_s'), 'core',
          (r.get('core') or {}).get('core_m2'), flush=True)
sys.exit(0 if read(rp).get('state') in TERMINAL else 1)
