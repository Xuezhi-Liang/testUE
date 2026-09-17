"""Pull recording shards from the S3 queue until it is empty. One shard = one map = one episode of two
passes (spec v1). Per shard: sync content packs, run longvideo/driver.sh (editor + runner.py: freeze ->
capture -> package -> acceptance) with uploader.sh streaming accepted episodes to the job's S3 prefix,
then file the shard's state and logs under results/. Claims are heartbeated every 4 min.
    CTR_PID=<pid> python3 record_loop.py <worker-id>"""
import os, sys, json, time, socket, subprocess, traceback, threading
from pathlib import Path
import boto3, botocore
W = sys.argv[1]; B = 'pan-simworld'; PRE = 'ue-record/260917/'
F = Path('/home/ubuntu/ue_record_fleet_20260917'); P = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline'); L = P / 'longvideo'
DST = 's3://pan-simworld/long-video-data-260917'
CONTENT = Path('/home/ubuntu/prj/SimWorld/simworld_100maps_1/Content'); DELIVERY = 's3://pan-simworld/SimWorld/new_map/Projects'
STALE_S = 1800; SHARD_WALL_S = 4.5 * 3600
s3 = boto3.Session(region_name='eu-north-1').client('s3')
def _inm(request, **_): request.headers['If-None-Match'] = '*'
def put(key, obj, cond=False):
    body = json.dumps(obj, ensure_ascii=False, indent=1).encode()
    if not cond: s3.put_object(Bucket=B, Key=PRE + key, Body=body); return
    s3.meta.events.register_first('before-sign.s3.PutObject', _inm)
    try: s3.put_object(Bucket=B, Key=PRE + key, Body=body)
    finally: s3.meta.events.unregister('before-sign.s3.PutObject', _inm)
def get(key):
    try: return json.loads(s3.get_object(Bucket=B, Key=PRE + key)['Body'].read())
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', '404'): return None
        raise
def log(*a): print(time.strftime('%H:%M:%S', time.gmtime()), *a, flush=True)
class Pulse:
    def __init__(self, slug, claim): self.slug, self.claim, self.stop = slug, claim, threading.Event(); threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        while not self.stop.wait(240):
            try: put(f'claims/{self.slug}.json', dict(self.claim, heartbeat_epoch=time.time(), phase=self.claim.get('phase', 'running')))
            except Exception: pass
def sync_packs(task):
    for pack in task.get('packs_to_sync') or []:
        dest = CONTENT / pack
        if dest.exists() and any(dest.iterdir()) and not task.get('force_sync'): log('pack present:', pack); continue
        log('syncing pack', pack); t0 = time.time()
        r = subprocess.run(['aws', 's3', 'sync', f"{DELIVERY}/{task['project_dir']}/Content/{pack}", str(dest), '--only-show-errors'], capture_output=True, text=True, timeout=3600)
        if r.returncode: raise RuntimeError(f'pack sync failed for {pack}: {r.stderr[-300:]}')
        log(f'pack {pack} synced in {time.time() - t0:.0f}s')
def upload_result(sid, extra):
    for p in [L / f'state_{sid}.json', P / 'logs' / f'lv_{sid}_runner.log', P / 'logs' / f'lv_{sid}_uploader.log', L / f'uploaded_{sid}.txt']:
        if p.exists(): s3.upload_file(str(p), B, f'{PRE}results/{sid}/{p.name}')
    for p in (P / 'logs').glob(f'lv_{sid}_ue_*.log'):
        if p.stat().st_size < 60_000_000: s3.upload_file(str(p), B, f'{PRE}results/{sid}/{p.name}')
    put(f'results/{sid}/result.json', extra)
manifest = get('manifest.json'); assert manifest, 'no manifest'
me = dict(worker=W, host=socket.gethostname(), pid=os.getpid(), started_epoch=time.time()); done_here = []
log(f'record worker {W} up; {len(manifest)} shards in the queue')
while True:
    claimed = None
    fresh = get('manifest.json')
    if fresh and fresh != manifest: log(f'manifest changed: {len(fresh)} shards'); manifest = fresh
    for task in manifest:
        sid = task['shard_id']
        if get(f'results/{sid}/result.json') is not None: continue
        key = f'claims/{sid}.json'; ex = get(key)
        if ex:
            age = time.time() - ex.get('heartbeat_epoch', ex.get('started_epoch', 0))
            if age < STALE_S or ex.get('worker') == W: continue
            log(f'claim on {sid} is {age/60:.0f} min stale (worker {ex.get("worker")}); stealing'); s3.delete_object(Bucket=B, Key=PRE + key)
        try: put(key, dict(me, slug=sid, phase='claimed', heartbeat_epoch=time.time()), cond=True); claimed = task; break
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] in ('PreconditionFailed', 'ConditionalRequestConflict'): continue
            raise
    if claimed is None: log('queue drained'); break
    sid = claimed['shard_id']; slug = claimed['slug']; claim = dict(me, slug=sid); pulse = Pulse(sid, claim); t0 = time.time()
    log(f'START {sid} (est {claimed.get("estimated_hours")} h of footage, clearance {claimed.get("ground_clearance_cm")} cm)')
    result = dict(shard_id=sid, slug=slug, worker=W, started_epoch=t0)
    try:
        claim['phase'] = 'syncing content'; put(f'claims/{sid}.json', dict(claim, heartbeat_epoch=time.time())); sync_packs(claimed)
        for stale in [L / f'stop_uploads_{sid}']: stale.unlink(missing_ok=True)
        env = dict(os.environ, CTR_PID=os.environ['CTR_PID'], SHARD_ID=sid, SLUG=slug, MAP_ID=claimed['map_id'], DEADLINE_EPOCH=str(int(time.time() + SHARD_WALL_S)),
                   LV_DST=DST, LV_STATUS=f'{DST}/_status', PORT='9208')
        upl_log = open(P / 'logs' / f'lv_{sid}_uploader_stdout.log', 'a')
        upl = subprocess.Popen(['bash', str(L / 'uploader.sh')], env=env, stdout=upl_log, stderr=subprocess.STDOUT)
        claim['phase'] = 'recording'; put(f'claims/{sid}.json', dict(claim, heartbeat_epoch=time.time()))
        with open(P / 'logs' / f'lv_{sid}_driver.log', 'a') as dl:
            rc = subprocess.run(['bash', str(L / 'driver.sh')], env=env, stdout=dl, stderr=subprocess.STDOUT).returncode
        claim['phase'] = 'uploading'; put(f'claims/{sid}.json', dict(claim, heartbeat_epoch=time.time()))
        (L / f'stop_uploads_{sid}').touch()
        try: upl.wait(timeout=2 * 3600)
        except subprocess.TimeoutExpired: upl.kill(); log('uploader did not finish in 2 h; killed')
        st = json.loads((L / f'state_{sid}.json').read_text()) if (L / f'state_{sid}.json').exists() else {}
        uploaded = (L / f'uploaded_{sid}.txt').read_text().split() if (L / f'uploaded_{sid}.txt').exists() else []
        result.update(state=st.get('done') or 'unknown', attempts=st.get('attempts', []), uploaded=uploaded, driver_rc=rc, dest=f'{DST}/{slug}/', finished_epoch=time.time(), total_elapsed_s=round(time.time() - t0, 1))
        log(f'DONE {sid} {result["state"]} uploaded={len(uploaded)} in {(time.time() - t0)/60:.1f} min')
    except Exception as e:
        traceback.print_exc(); result.update(state='failed', error=f'{type(e).__name__}: {str(e)[:300]}', finished_epoch=time.time(), total_elapsed_s=round(time.time() - t0, 1))
    finally:
        pulse.stop.set()
        try: upload_result(sid, result)
        except Exception: traceback.print_exc()
        try: put(f'claims/{sid}.json', dict(claim, phase='done', heartbeat_epoch=time.time(), finished_epoch=time.time()))
        except Exception: pass
        done_here.append(dict(shard_id=sid, state=result.get('state'), elapsed_s=result.get('total_elapsed_s')))
put(f'workers/{W}/finished.json', dict(me, finished_epoch=time.time(), shards=done_here)); log(f'worker {W} finished {len(done_here)} shards')
