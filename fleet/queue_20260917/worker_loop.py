"""Pull tasks from the shared S3 queue until it is empty, then shut this instance down.

There is no static shard. Every worker walks the same manifest, which is ordered longest-estimate
first, and takes the first task nobody has claimed; S3 conditional writes (If-None-Match) make the
claim atomic, so two workers can never take the same map. Longest-first + pull is the classic LPT
schedule: the big maps start immediately and the small ones fill the gaps behind them, which is
what makes ten machines finish together instead of nine idling while one grinds.

A worker that dies mid-map leaves a claim whose heartbeat stops; after STALE_S another worker
deletes it and re-claims it conditionally, so the map is measured rather than silently dropped.

    CTR_PID=<pid> python3 worker_loop.py <worker-id>
"""
import os, sys, json, time, socket, subprocess, traceback, threading
from pathlib import Path
import boto3, botocore

W = sys.argv[1]
B = 'pan-simworld'
PRE = 'ue-newroute/20260917/'
F = Path('/home/ubuntu/ue_newroute_fleet_20260917')
RUNTIME = F / 'bundle/runtime'
CONTENT = Path('/home/ubuntu/prj/SimWorld/simworld_100maps_1/Content')
DELIVERY = 's3://pan-simworld/SimWorld/new_map/Projects'
STALE_S = int(os.environ.get('STALE_S', 1800))
s3 = boto3.Session(region_name='eu-north-1').client('s3')
(F / 'maps').mkdir(parents=True, exist_ok=True)


def _if_none_match(request, **_):
    request.headers['If-None-Match'] = '*'


def put(key, obj, cond=False):
    """Write a queue object; with cond=True the write only succeeds if the key does not exist.

    The header goes on through botocore's event system rather than put_object's IfNoneMatch
    parameter: the image ships an older botocore whose model has no such parameter, and it rejects
    the call before it ever reaches S3. The header is understood by S3 regardless of SDK age, so
    the claim stays atomic on whatever boto3 the instance happens to have.
    """
    body = json.dumps(obj, ensure_ascii=False, indent=1).encode()
    if not cond:
        s3.put_object(Bucket=B, Key=PRE + key, Body=body); return
    s3.meta.events.register_first('before-sign.s3.PutObject', _if_none_match)
    try:
        s3.put_object(Bucket=B, Key=PRE + key, Body=body)
    finally:
        s3.meta.events.unregister('before-sign.s3.PutObject', _if_none_match)


def get(key):
    try: return json.loads(s3.get_object(Bucket=B, Key=PRE + key)['Body'].read())
    except s3.exceptions.NoSuchKey: return None
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', '404'): return None
        raise


def log(*a):
    print(time.strftime('%H:%M:%S', time.gmtime()), *a, flush=True)


def sync_packs(task):
    """Put the map's content pack on this host before the editor opens the level.

    Only packs the image lacks are listed, and only the pack's own top-level folder is copied -
    never a generic name like Meshes or Materials, which would land assets in the project root.
    """
    for pack in task.get('packs_to_sync') or []:
        dest = CONTENT / pack
        if dest.exists() and any(dest.iterdir()) and not task.get('force_sync'):
            log('pack present:', pack); continue
        # force_sync: the image carries this pack but not every level in it (four levels opened as
        # an empty Untitled world on the first pass). s3 sync only fetches what differs, so
        # re-running it over a present pack is cheap and fills the gaps.
        proj = task['project_dir']
        log('syncing pack', pack, 'from', proj)
        t0 = time.time()
        r = subprocess.run(['aws', 's3', 'sync', f'{DELIVERY}/{proj}/Content/{pack}', str(dest), '--only-show-errors'],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode:
            raise RuntimeError(f'pack sync failed for {pack}: {r.stderr[-300:]}')
        log(f'pack {pack} synced in {time.time() - t0:.0f}s')


def upload_result(slug, out):
    for name in ('result.json', 'worker.log', 'editor.log', 'core.png'):
        p = out / name
        if p.exists():
            s3.upload_file(str(p), B, f'{PRE}results/{slug}/{name}')
    nav = out / 'frozen' / 'nav'
    if nav.exists():
        for p in nav.iterdir():
            if p.is_file() and p.stat().st_size < 60_000_000:
                s3.upload_file(str(p), B, f'{PRE}results/{slug}/nav/{p.name}')


def heartbeat(slug, claim, phase):
    claim.update(phase=phase, heartbeat_epoch=time.time())
    try: put(f'claims/{slug}.json', claim)
    except Exception: pass


class Pulse:
    """Refresh the claim every 4 min while a task runs. Before 17 Sep the claim was only touched
    on phase changes, so a 3-hour map looked 30 min stale to every other worker and was stolen -
    the same level measured twice, both results uploaded, one machine's hours thrown away."""
    def __init__(self, slug, claim):
        self.slug, self.claim, self.stop = slug, claim, threading.Event()
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        while not self.stop.wait(240):
            try: put(f'claims/{self.slug}.json', dict(self.claim, heartbeat_epoch=time.time()))
            except Exception: pass


manifest = get('manifest.json')
assert manifest, 'no manifest in the queue'
me = dict(worker=W, host=socket.gethostname(), pid=os.getpid(), started_epoch=time.time())
done_here = []
log(f'worker {W} up; {len(manifest)} tasks in the queue')

while True:
    claimed = None
    fresh = get('manifest.json')               # re-read: tasks can be appended or edited while the fleet runs
    if fresh and fresh != manifest:            # any change (17 Sep: a length-only test missed per-task budget edits)
        log(f'manifest changed: {len(fresh)} tasks'); manifest = fresh
    for task in manifest:                      # longest estimate first
        slug = task['slug']
        if get(f'results/{slug}/result.json') is not None:
            continue
        key = f'claims/{slug}.json'
        existing = get(key)
        if existing:
            age = time.time() - existing.get('heartbeat_epoch', existing.get('started_epoch', 0))
            if age < STALE_S or existing.get('worker') == W:
                continue
            log(f'claim on {slug} is {age / 60:.0f} min stale (worker {existing.get("worker")}); stealing')
            s3.delete_object(Bucket=B, Key=PRE + key)
        try:
            put(key, dict(me, slug=slug, phase='claimed', heartbeat_epoch=time.time()), cond=True)
            claimed = task; break
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] in ('PreconditionFailed', 'ConditionalRequestConflict'):
                continue                        # someone beat us to it, keep walking
            raise
    if claimed is None:
        log('queue drained'); break

    slug = claimed['slug']; out = F / 'maps' / slug
    claim = dict(me, slug=slug)
    log(f'START {slug} (est {claimed.get("est_s", 0) / 60:.0f} min, {claimed.get("why", "")})')
    t0 = time.time()
    pulse = Pulse(slug, claim)
    try:
        heartbeat(slug, claim, 'syncing content')
        sync_packs(claimed)
        (out).mkdir(parents=True, exist_ok=True)
        tf = out / 'task.json'; tf.write_text(json.dumps(claimed, ensure_ascii=False, indent=1))
        rc = None
        for attempt in range(2):
            heartbeat(slug, claim, f'measuring (editor {attempt + 1})')
            env = dict(os.environ, CTR_PID=os.environ['CTR_PID'])
            with (out / 'runone.log').open('a') as lg:
                rc = subprocess.run(['python3', str(RUNTIME / 'runone.py'), str(tf), str(out)],
                                    stdout=lg, stderr=subprocess.STDOUT, env=env).returncode
            if rc == 0: break
            log(f'{slug} unusable on attempt {attempt + 1}; retrying on a fresh editor')
        heartbeat(slug, claim, 'uploading')
        upload_result(slug, out)
        r = json.loads((out / 'result.json').read_text())
        log(f'DONE {slug} {r.get("state")} core={(r.get("core") or {}).get("core_m2")} in {(time.time() - t0) / 60:.1f} min')
        done_here.append(dict(slug=slug, state=r.get('state'), core=(r.get('core') or {}).get('core_m2'),
                              elapsed_s=round(time.time() - t0, 1)))
    except Exception as e:
        traceback.print_exc()
        err = dict(slug=slug, state='failed', error=f'{type(e).__name__}: {str(e)[:300]}',
                   worker=W, finished_epoch=time.time(), total_elapsed_s=round(time.time() - t0, 1))
        (out).mkdir(parents=True, exist_ok=True); (out / 'result.json').write_text(json.dumps(err, ensure_ascii=False, indent=2))
        try: upload_result(slug, out)
        except Exception: pass
        done_here.append(dict(slug=slug, state='failed', core=None, elapsed_s=round(time.time() - t0, 1)))
    finally:
        pulse.stop.set()
        try: put(f'claims/{slug}.json', dict(claim, phase='done', heartbeat_epoch=time.time(), finished_epoch=time.time()))
        except Exception: pass

put(f'workers/{W}/finished.json', dict(me, finished_epoch=time.time(), maps=done_here))
log(f'worker {W} finished {len(done_here)} maps; shutting down')
subprocess.run(['sudo', 'shutdown', '-h', '+1', 'route measurement queue drained'])
