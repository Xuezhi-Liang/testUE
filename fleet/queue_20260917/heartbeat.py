"""Side heartbeat for a running worker_loop: every 4 min refresh the claim of the task this worker
is measuring, so a 3-hour map is not mistaken for a dead worker and stolen. worker_loop only
touched the claim on phase changes; this closes that gap without restarting it."""
import json, re, time, sys, boto3, botocore
from pathlib import Path
F = Path('/home/ubuntu/ue_newroute_fleet_20260917'); LOG = F / 'worker_loop.log'
B, PRE = 'pan-simworld', 'ue-newroute/20260917/'
s3 = boto3.Session(region_name='eu-north-1').client('s3')
W = re.search(r'worker (\d+) up', LOG.read_text()).group(1)
while True:
    try:
        lines = LOG.read_text().splitlines()
        active = None
        for ln in reversed(lines):
            if ' DONE ' in ln or 'queue drained' in ln or 'finished' in ln: break
            m = re.search(r' START (\S+)', ln)
            if m: active = m.group(1); break
        if active:
            key = PRE + f'claims/{active}.json'
            try: c = json.loads(s3.get_object(Bucket=B, Key=key)['Body'].read())
            except botocore.exceptions.ClientError: c = None
            if c is None or c.get('worker') == W:
                c = c or dict(worker=W, slug=active, phase='measuring')
                c['heartbeat_epoch'] = time.time(); c['heartbeat_by'] = 'side'
                s3.put_object(Bucket=B, Key=key, Body=json.dumps(c, indent=1).encode())
    except Exception as e:
        print(time.strftime('%H:%M:%S'), 'heartbeat error', e, flush=True)
    time.sleep(240)
