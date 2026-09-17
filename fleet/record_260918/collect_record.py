"""Status page for the 260918 recording queue: /longvideo/record-260918/queue/ (rebuilt every 60 s)."""
import json, time, html, traceback
from pathlib import Path
import boto3
F = Path('/home/ubuntu/ue_record_fleet_20260918'); SITE = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/record-260918/queue'); SITE.mkdir(parents=True, exist_ok=True)
B, PRE = 'pan-simworld', 'ue-record/260918/'
s3 = boto3.Session(region_name='eu-north-1').client('s3'); ec = boto3.Session(region_name='eu-north-1').client('ec2')
def body(k):
    try: return json.loads(s3.get_object(Bucket=B, Key=PRE + k)['Body'].read())
    except Exception: return None
def listing(prefix):
    out, tok = {}, None
    while True:
        kw = dict(Bucket=B, Prefix=PRE + prefix); 
        if tok: kw['ContinuationToken'] = tok
        r = s3.list_objects_v2(**kw)
        for o in r.get('Contents', []): out[o['Key'][len(PRE):]] = o
        if not r.get('IsTruncated'): break
        tok = r['NextContinuationToken']
    return out
def once():
    man = body('manifest.json') or []
    claims = {k.split('/')[1][:-5]: body(k) for k in listing('claims/')}
    results = {k.split('/')[1]: body(k) for k in listing('results/') if k.endswith('/result.json')}
    inst = {}
    try:
        for res in ec.describe_instances(Filters=[{'Name': 'tag:RecordRun', 'Values': ['260918']}])['Reservations']:
            for i in res['Instances']:
                w = next((t['Value'] for t in i.get('Tags', []) if t['Key'] == 'RecordWorker'), None)
                if w: inst[w] = i['State']['Name']
    except Exception: pass
    rows = []
    for m in man:
        s = m['shard_id']; r = results.get(s) or {}; c = claims.get(s) or {}
        rows.append(dict(shard=s, slug=m['slug'], est_h=round(m['est_s'] / 3600, 1), lighting=m.get('lighting'), state=r.get('state'), frames=((r.get('attempts') or [{}])[-1]).get('frames'),
                         uploaded=len(r.get('uploaded') or []), elapsed=r.get('total_elapsed_s'), worker=c.get('worker'), phase=c.get('phase'), hb=c.get('heartbeat_epoch'), err=(r.get('error') or '')[:120]))
    done = [x for x in rows if x['state']]; running = [x for x in rows if not x['state'] and x['phase'] and x['phase'] != 'done']
    st = dict(updated=time.time(), total=len(rows), done=len(done), accepted=sum(1 for x in done if x['state'] == 'accepted'), rejected=sum(1 for x in done if x['state'] == 'rejected'),
              failed=sum(1 for x in done if x['state'] not in ('accepted', 'rejected')), running=len(running), instances=inst, rows=rows)
    (SITE / 'status.json').write_text(json.dumps(st, ensure_ascii=False))
    now = time.strftime('%H:%M UTC', time.gmtime()); up = sum(1 for v in inst.values() if v == 'running')
    def tr(x):
        cls = 'ok' if x['state'] == 'accepted' else 'mid' if x['state'] == 'rejected' else 'bad' if x['state'] else ('run' if x['phase'] and x['phase'] != 'done' else '')
        stt = x['state'] or (f"{x['phase']} · worker {x['worker']} · {int((time.time() - x['hb']) / 60)} min 前心跳" if x['phase'] and x['phase'] != 'done' else '排队')
        return f"<tr class='{cls}'><td>{html.escape(x['slug'].removeprefix('Game_'))}</td><td>{x['est_h']}</td><td>{x['lighting']}</td><td>{html.escape(str(stt))}</td><td>{x['frames'] or ''}</td><td>{x['uploaded'] or ''}</td><td>{round(x['elapsed'] / 60) if x['elapsed'] else ''}</td><td class=mut>{html.escape(x['err'])}</td></tr>"
    page = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=60><title>录制队列 · 260918</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#222}}table{{border-collapse:collapse;font-size:13px}}th,td{{border:1px solid #ddd;padding:3px 7px}}th{{background:#f3f3f3}}tr.ok td{{background:#e6f4ea}}tr.mid td{{background:#fff4d6}}tr.bad td{{background:#fde8e8}}tr.run td{{background:#e8f0fe}}.mut{{color:#777;font-size:12px}}</style>
<h1>录制队列 · 260918（规格 v1，两遍不封顶，起点周围 120 m 路网）</h1>
<p>{now} · 机器 {up} 台在线 · {st['total']} 张：完成 {st['done']}（通过 {st['accepted']}，被拒 {st['rejected']}，失败 {st['failed']}），在录 {st['running']}，排队 {st['total'] - st['done'] - st['running']} · <a href='../'>看视频</a> · <a href='../../map-plan/'>规划页</a></p>
<table><tr><th>地图</th><th>估计机时 h</th><th>光照</th><th>状态</th><th>帧数</th><th>已传集</th><th>耗时 min</th><th>错误</th></tr>{''.join(tr(x) for x in rows)}</table>"""
    (SITE / 'index.html').write_text(page); return st
if __name__ == '__main__':
    while True:
        try: st = once(); print(time.strftime('%H:%M:%S'), f"done {st['done']}/{st['total']} running {st['running']}", flush=True)
        except Exception: traceback.print_exc()
        time.sleep(60)
