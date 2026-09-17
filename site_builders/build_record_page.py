#!/usr/bin/env python3
"""Review page /longvideo/record-260917/: every episode recorded by the 260917 fleet, with its video.
Pulls each episode's small files (acceptance, capture summary, trajectory summary, route plot) and the
rgb.mp4 from s3://pan-simworld/long-video-data-260917/ into the site, then renders one card per episode.
Rerunnable:   python3 build_record_page.py"""
import json, html, subprocess, time
from pathlib import Path
S3 = 's3://pan-simworld/long-video-data-260917'
SITE = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/record-260917'); SITE.mkdir(parents=True, exist_ok=True)
def s3ls(prefix):
    r = subprocess.run(['aws', 's3', 'ls', prefix], capture_output=True, text=True); return [l.split()[-1].rstrip('/') for l in r.stdout.splitlines() if l.strip()]
eps = []
for slug in s3ls(S3 + '/'):
    if slug.startswith('_'): continue
    for ep in s3ls(f'{S3}/{slug}/'):
        rejected = ep == '_rejected'
        for e in ([f'_rejected/{x}' for x in s3ls(f'{S3}/{slug}/_rejected/')] if rejected else [ep]):
            eps.append((slug, e))
cards = []
for slug, ep in eps:
    src = f'{S3}/{slug}/{ep}'; local = SITE / 'episodes' / ep.replace('_rejected/', 'rejected__'); local.mkdir(parents=True, exist_ok=True)
    if ep.startswith('_rejected/') and (slug, ep.split('/', 1)[1]) in eps: continue   # metadata already merged into the accepted path
    subprocess.run(['aws', 's3', 'sync', src, str(local), '--exclude', '*', '--include', '*.json', '--include', '*.png', '--include', '*.mp4', '--include', 'frames.csv', '--only-show-errors', '--exclude', 'rgb/*', '--exclude', 'depth/*'])
    def J(name):
        p = local / name
        try: return json.load(open(p))
        except Exception: return {}
    acc = J('acceptance.json'); cs = J('capture_summary.json'); tj = J('trajectory.json')
    gates = acc.get('gates') or []
    failed = [g.get('gate') for g in gates if isinstance(g, dict) and g.get('result') not in ('pass', 'skipped', 'skip')] if isinstance(gates, list) else [k for k, v in gates.items() if isinstance(v, dict) and v.get('ok') is False]
    frames = cs.get('frames') or tj.get('frames') or acc.get('frames'); fps = cs.get('fps') or tj.get('fps') or 24
    mix = (tj.get('action_mix') or {}).get('fraction') or cs.get('action_mix') or {}
    mp4 = next((n for n in ('rgb.mp4', 'review.mp4', 'preview.mp4', 'rgb_proxy.mp4') if (local / n).exists()), None)
    rv0 = cs.get('review_video') or ''; rv = (rv0.get('path') or rv0.get('file') or '' if isinstance(rv0, dict) else str(rv0)).split('/')[-1]
    if not mp4 and rv and (local / rv).exists(): mp4 = rv
    pngs = sorted(p.name for p in local.glob('*.png'))
    cards.append(dict(rejudged=bool(acc.get('rejudged')), slug=slug, ep=ep, local=local.name, accepted=acc.get('accepted'), failed=failed, frames=frames, minutes=round(frames / fps / 60, 1) if frames else None,
                      passes=(lambda ps: f"{ps.get('complete_passes')}/{ps.get('passes_requested')} 遍完成，各遍帧数 {ps.get('frames_per_pass_measured', [])[:2]}" if ps else None)(tj.get('passes_summary')), mix={k: round(v, 3) for k, v in mix.items()} if mix else None, mp4=mp4, pngs=pngs, spec=(tj.get('task') or {}).get('spec_version') or cs.get('spec_version'), retrace=(tj.get('retrace') or {}).get('events'), gb=None))
now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
css = "body{font-family:system-ui,sans-serif;margin:24px;max-width:1500px;color:#222}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:16px}.card{border:1px solid #e3e3e3;border-radius:8px;padding:12px;background:#fff}video{width:100%;border-radius:6px;background:#000}.mut{color:#777;font-size:12px}.ok{color:#1a7f37}.bad{color:#b42318}table{font-size:12px;border-collapse:collapse}td{padding:1px 6px}img{width:100%;border-radius:4px}"
h = [f"<!doctype html><meta charset=utf-8><title>录制结果 · 260917</title><style>{css}</style><h1>录制结果 · 260917 批次（规格 v1，每图 ≤30 分钟）</h1><p class=mut>数据：<code>s3://pan-simworld/long-video-data-260917/&lt;slug&gt;/&lt;episode&gt;/</code>（rgb/ 每帧 JPEG、depth/ 每帧 EXR、frames.csv、位姿、acceptance.json、capture_summary.json）。这里只拉了 mp4 预览和元数据。生成于 {now}。<a href='../spec/'>规格 v1</a> · <a href='../map-plan/'>规划页</a></p>", "<div class=grid>"]
for c in sorted(cards, key=lambda c: (c['accepted'] is not True, c['slug'])):
    v = f"<video controls preload='metadata' src='episodes/{c['local']}/{c['mp4']}'></video>" if c['mp4'] else "<div class=mut>（还没有 mp4：机器还在上传，或该集没生成预览）</div>"
    verdict = ('<span class=ok>验收通过</span>' + (' <span class=mut>(按 v1 容差回判)</span>' if c.get('rejudged') else '')) if c['accepted'] else (f"<span class=bad>验收未过：{', '.join(c['failed']) or '见 acceptance.json'}</span>" if c['accepted'] is False else '<span class=mut>未判</span>')
    mix = ' · '.join(f"{k} {v*100:.0f}%" for k, v in (c['mix'] or {}).items()) if c['mix'] else '—'
    pngs = ''.join(f"<a href='episodes/{c['local']}/{p}'><img src='episodes/{c['local']}/{p}' loading='lazy'></a>" for p in c['pngs'][:2])
    h.append(f"<div class=card><b>{html.escape(c['slug'].removeprefix('Game_'))}</b><div class=mut>{html.escape(c['ep'])}</div>{v}<table><tr><td>判定</td><td>{verdict}</td></tr><tr><td>时长</td><td>{c['minutes'] or '—'} min · {c['frames'] or '—'} 帧</td></tr><tr><td>遍数 / 折返</td><td>{html.escape(str(c['passes'])) if c['passes'] else '—'} / {c['retrace'] if c['retrace'] is not None else '—'}</td></tr><tr><td>配比</td><td>{mix}</td></tr><tr><td>规格</td><td>{c['spec'] or '—'}</td></tr></table>{pngs}</div>")
h.append("</div>")
if not cards: h.append("<p>还没有任何一集落地。</p>")
(SITE / 'index.html').write_text('\n'.join(h)); print('episodes on page:', len(cards))
