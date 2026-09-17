"""Mirror the S3 queue locally and publish a status page for the route-measurement fleet."""
import json, time, html, traceback, subprocess
from pathlib import Path
import boto3
F = Path('/home/ubuntu/ue_newroute_fleet_20260917'); R = Path('/home/ubuntu/ue_newroute_20260917')
SITE = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/route-queue-20260917'); SITE.mkdir(parents=True, exist_ok=True)
B, PRE = 'pan-simworld', 'ue-newroute/20260917/'
s3 = boto3.Session(region_name='eu-north-1').client('s3'); ec = boto3.Session(region_name='eu-north-1').client('ec2')
man = json.loads((R / 'manifest.json').read_text())
PACK = {m['slug']: (m.get('title') or '') for m in man}
STATE_CN = {'geometry_pass': '几何全过', 'coverage_review': '待审(保留率低)', 'route_failed': '路线失败',
            'failed': '起点/加载失败', 'timeout': '超时', 'blocked_asset': '资产未挂载'}


def body(key):
    try: return json.loads(s3.get_object(Bucket=B, Key=PRE + key)['Body'].read())
    except Exception: return None


def listing(prefix):
    out = {}
    tok = None
    while True:
        kw = dict(Bucket=B, Prefix=PRE + prefix)
        if tok: kw['ContinuationToken'] = tok
        r = s3.list_objects_v2(**kw)
        for o in r.get('Contents', []): out[o['Key'][len(PRE):]] = o
        if not r.get('IsTruncated'): break
        tok = r['NextContinuationToken']
    return out


def once():
    claims = {k.split('/')[1][:-5]: body(k) for k in listing('claims/')}
    resk = listing('results/')
    results = {}
    for k in resk:
        if k.endswith('/result.json'):
            slug = k.split('/')[1]
            r = body(k)
            if r: results[slug] = r
            dest = F / 'maps' / slug / 'result.json'; dest.parent.mkdir(parents=True, exist_ok=True)
            if r and (not dest.exists() or dest.stat().st_size != len(json.dumps(r))):
                dest.write_text(json.dumps(r, ensure_ascii=False, indent=2))
    inst = {}
    try:
        for res in ec.describe_instances(Filters=[{'Name': 'tag:NewRouteRun', 'Values': ['20260917']}])['Reservations']:
            for i in res['Instances']:
                w = next((t['Value'] for t in i.get('Tags', []) if t['Key'] == 'NewRouteWorker'), None)
                if w: inst[w] = i['State']['Name']
    except Exception: pass
    rows = []
    for m in man:
        s = m['slug']; r = results.get(s); c = claims.get(s)
        rows.append(dict(slug=s, title=m.get('title') or '', est=m['est_s'], why=m['why'], src=m['source'], state=(r or {}).get('state'),
                         core=((r or {}).get('core') or {}).get('core_m2'), elapsed=(r or {}).get('total_elapsed_s'),
                         kept=((r or {}).get('attempts') or [{}])[-1].get('kept_fraction'),
                         worker=(c or {}).get('worker'), phase=(c or {}).get('phase'),
                         hb=(c or {}).get('heartbeat_epoch')))
    done = [x for x in rows if x['state']]
    running = [x for x in rows if not x['state'] and x['phase'] and x['phase'] != 'done']
    status = dict(updated=time.time(), total=len(rows), done=len(done), running=len(running),
                  instances=inst, rows=rows)
    (SITE / 'status.json').write_text(json.dumps(status, ensure_ascii=False))
    build(status)
    return status


def build(st):
    now = time.strftime('%H:%M UTC', time.gmtime())
    rows = st['rows']
    done = [x for x in rows if x['state']]
    withcore = [x for x in done if x['core'] is not None]
    passed = [x for x in done if x['state'] == 'geometry_pass']
    up = sum(1 for v in st['instances'].values() if v == 'running')
    def tr(x):
        if x['state']:
            sc = STATE_CN.get(x['state'], x['state'])
            cls = 'ok' if x['state'] == 'geometry_pass' else 'mid' if x['state'] == 'coverage_review' else 'bad'
            extra = f"{x['elapsed'] / 60:.0f} min" if x['elapsed'] else ''
        elif x['phase'] and x['phase'] != 'done':
            sc, cls, extra = html.escape(x['phase']), 'run', f"w{x['worker']}"
        else:
            sc, cls, extra = '排队中', 'mut', f"预计 {x['est'] / 60:.0f} min"
        name = html.escape(x['title']) if x['title'] else html.escape(x['slug'].removeprefix('Game_').split('_Maps_')[0].split('_Map_')[0])
        return (f"<tr><td><b>{name}</b><div class='mut small'>{html.escape(x['slug'].removeprefix('Game_'))}</div>"
                f"<div class='mut small'>{html.escape(x['why'])}</div></td>"
                f"<td class='st {cls}'>{sc}</td><td class='r mono'>{'' if x['core'] is None else format(x['core'], ',.0f')}</td>"
                f"<td class='r mono'>{'' if x['kept'] is None else format(x['kept'], '.0%')}</td>"
                f"<td class='mut small'>{extra}</td></tr>")
    order = sorted(rows, key=lambda x: (x['state'] is not None, -(x['est'] or 0)))
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>路线信息补测队列</title><style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--cam:#8a5a0b;--bad:#c2402f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--bad:#ff8272}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif}}
header,.wrap{{max-width:1100px;margin:0 auto;padding:0 20px}}header{{padding-top:26px}}h1{{margin:0 0 6px;font-size:22px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a{{color:var(--move);text-decoration:none}}
.totals{{display:flex;gap:22px;flex-wrap:wrap;margin:16px 0;font-size:13px;color:var(--mut)}}
.totals div b{{display:block;font-size:21px;color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:8px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:500;font-size:11.5px}}td.r,th.r{{text-align:right}}.mono{{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}}
.mut{{color:var(--mut)}}.small{{font-size:12px}}td.st.ok{{color:var(--spd)}}td.st.mid{{color:var(--cam)}}td.st.bad{{color:var(--bad)}}td.st.run{{color:var(--move)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px;margin-bottom:14px}}
</style></head><body><header><h1>路线信息补测队列</h1>
<p class="sub"><a href="../inventory/">← 地图资源总表</a> · 10 台 g6.4xlarge 从共享队列拉任务 · {now} 自动刷新</p>
<div class="totals"><div>任务<b>{st['total']}</b></div><div>已完成<b>{len(done)}</b></div><div>进行中<b>{st['running']}</b></div>
<div>测出核心<b>{len(withcore)}</b></div><div>几何全过<b>{len(passed)}</b></div><div>在线机器<b>{up}</b></div></div></header>
<div class="wrap"><div class="card"><p class="sub">队列按预计耗时从长到短排；每台机器空闲时拿走第一个没人认领的任务（S3 条件写做原子认领），所以大图先开工、小图填空隙，十台机器一起收工。某台掉线超过 30 分钟，它的任务会被别的机器接管。</p></div>
<table><thead><tr><th>地图（资源包 / 关卡）</th><th>状态</th><th class="r">核心 m²</th><th class="r">保留率</th><th>耗时 / 机器</th></tr></thead>
<tbody>{''.join(tr(x) for x in order)}</tbody></table></div></body></html>"""
    (SITE / 'index.html').write_text(page)


if __name__ == '__main__':
    while True:
        try:
            st = once()
            print(time.strftime('%H:%M:%S', time.gmtime()), f"done {st['done']}/{st['total']} running {st['running']}", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(30)
