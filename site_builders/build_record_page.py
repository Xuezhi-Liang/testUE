#!/usr/bin/env python3
"""Review page /longvideo/record-260917/: every episode recorded by the 260917 fleet, with its video.
Pulls each episode's small files (acceptance, capture summary, trajectory summary, route plot) and the
rgb.mp4 from s3://pan-simworld/long-video-data-260917/ into the site, then renders one card per episode.
Rerunnable:   python3 build_record_page.py"""
import json, html, subprocess, time
from pathlib import Path
import sys
RUN = sys.argv[1] if len(sys.argv) > 1 else '260917'
S3 = f's3://pan-simworld/long-video-data-{RUN}'
SITE = Path(f'/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/record-{RUN}'); SITE.mkdir(parents=True, exist_ok=True)
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
# Episodes whose metadata + mp4 exist locally but whose S3 prefix is gone (17 Sep: four episodes
# vanished from the bucket around 16:35 UTC; cause unknown). Shown with a warning, not hidden.
(SITE / 'episodes').mkdir(parents=True, exist_ok=True)
present = {ep.split('/', 1)[-1] for _, ep in eps}
for d in sorted((SITE / 'episodes').iterdir()):
    if d.is_dir() and d.name not in present and (d / 'acceptance.json').exists():
        eps.append((d.name.split('__coverage_walk')[0], 'LOCALONLY/' + d.name))
for slug, ep in eps:
    if ep.startswith('LOCALONLY/'):
        ep = ep.split('/', 1)[1]; local = SITE / 'episodes' / ep; lost = True
    else:
        lost = False
    if not lost:
        src = f'{S3}/{slug}/{ep}'; local = SITE / 'episodes' / ep.split('/', 1)[-1]; local.mkdir(parents=True, exist_ok=True)   # _rejected/<ep> and <ep> share one local dir
    if not lost and not ep.startswith('_rejected/') and (slug, f'_rejected/{ep}') in eps: continue   # the rejected entry carries the metadata; the plain one only the mp4
    if False and ep.startswith('_rejected/'):
        subprocess.run(['aws', 's3', 'sync', f'{S3}/{slug}/{ep.split("/", 1)[1]}', str(local), '--exclude', '*', '--exclude', '*.mp4', '--only-show-errors'])
    if not lost: subprocess.run(['aws', 's3', 'sync', src, str(local), '--exclude', '*', '--include', '*.json', '--include', '*.png', '--exclude', '*.mp4', '--include', 'frames.csv', '--only-show-errors', '--exclude', 'rgb/*', '--exclude', 'depth/*'])
    def J(name):
        p = local / name
        try: return json.load(open(p))
        except Exception: return {}
    acc = J('acceptance.json'); cs = J('capture_summary.json'); tj = J('trajectory.json')
    gates = acc.get('gates') or []
    failed = [g.get('gate') for g in gates if isinstance(g, dict) and g.get('result') not in ('pass', 'skipped', 'skip')] if isinstance(gates, list) else [k for k, v in gates.items() if isinstance(v, dict) and v.get('ok') is False]
    frames = cs.get('frames') or tj.get('frames') or acc.get('frames'); fps = cs.get('fps') or tj.get('fps') or 24
    mix = (tj.get('action_mix') or {}).get('fraction') or cs.get('action_mix') or {}
    pv = local / 'preview5.mp4'
    if not pv.exists() and not lost: subprocess.run(['aws', 's3', 'cp', f's3://pan-simworld/ue-record/{RUN}/previews/{ep.split("/", 1)[-1]}.mp4', str(pv), '--only-show-errors'], capture_output=True)
    mp4 = next((n for n in ('preview5.mp4', 'rgb.mp4', 'review.mp4', 'preview.mp4', 'rgb_proxy.mp4') if (local / n).exists()), None)
    rv0 = cs.get('review_video') or ''; rv = (rv0.get('path') or rv0.get('file') or '' if isinstance(rv0, dict) else str(rv0)).split('/')[-1]
    if not mp4 and rv and (local / rv).exists(): mp4 = rv
    pngs = sorted(p.name for p in local.glob('*.png'))
    cards.append(dict(lost=lost, rejudged=bool(acc.get('rejudged')), slug=slug, ep=ep, local=local.name, accepted=acc.get('accepted'), failed=failed, frames=frames, minutes=round(frames / fps / 60, 1) if frames else None,
                      passes=(lambda ps: f"{ps.get('complete_passes')}/{ps.get('passes_requested')} 遍完成，各遍帧数 {ps.get('frames_per_pass_measured', [])[:2]}" if ps else None)(tj.get('passes_summary')), mix={k: round(v, 3) for k, v in mix.items()} if mix else None, mp4=mp4, pngs=pngs, spec=(tj.get('task') or {}).get('spec_version') or cs.get('spec_version'), retrace=(tj.get('retrace') or {}).get('events'), gb=None))
now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
css = "body{font-family:system-ui,sans-serif;margin:24px;max-width:1500px;color:#222}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:16px}.card{border:1px solid #e3e3e3;border-radius:8px;padding:12px;background:#fff}video{width:100%;border-radius:6px;background:#000}.mut{color:#777;font-size:12px}.ok{color:#1a7f37}.bad{color:#b42318}table{font-size:12px;border-collapse:collapse}td{padding:1px 6px}img{width:100%;border-radius:4px}"
h = [f"<!doctype html><meta charset=utf-8><title>录制结果 · {RUN}</title><style>{css}</style><h1>录制结果 · {RUN} 批次（规格 v1）</h1><p class=mut>数据：<code>s3://pan-simworld/long-video-data-{RUN}/&lt;slug&gt;/&lt;episode&gt;/</code>（rgb/ 每帧 JPEG、depth/ 每帧 EXR、frames.csv、位姿、acceptance.json、capture_summary.json，不含 mp4）。这里的视频是每集前 5 分钟的预览片，存在队列的 ops 前缀下。生成于 {now}。<a href='../spec/'>规格 v1</a> · <a href='../map-plan/'>规划页</a></p>", "<div class=grid>"]
for c in sorted(cards, key=lambda c: (c['accepted'] is not True, c['slug'])):
    v = f"<video controls preload='metadata' src='episodes/{c['local']}/{c['mp4']}'></video>" if c['mp4'] else "<div class=mut>（还没有 mp4：机器还在上传，或该集没生成预览）</div>"
    verdict = ('<span class=bad>S3 上的原始帧已不在（约 16:35 UTC 消失，原因待查）；这里的视频和元数据是本机留存</span><br>' if c.get('lost') else '') + ('<span class=ok>验收通过</span>' + (' <span class=mut>(按 v1 容差回判)</span>' if c.get('rejudged') else '')) if c['accepted'] else (f"<span class=bad>验收未过：{', '.join(c['failed']) or '见 acceptance.json'}</span>" if c['accepted'] is False else '<span class=mut>未判</span>')
    mix = ' · '.join(f"{k} {v*100:.0f}%" for k, v in (c['mix'] or {}).items()) if c['mix'] else '—'
    pngs = ''.join(f"<a href='episodes/{c['local']}/{p}'><img src='episodes/{c['local']}/{p}' loading='lazy'></a>" for p in c['pngs'][:2])
    h.append(f"<div class=card><b>{html.escape(c['slug'].removeprefix('Game_'))}</b><div class=mut>{html.escape(c['ep'])}</div>{v}<table><tr><td>判定</td><td>{verdict}</td></tr><tr><td>时长</td><td>{c['minutes'] or '—'} min · {c['frames'] or '—'} 帧</td></tr><tr><td>遍数 / 折返</td><td>{html.escape(str(c['passes'])) if c['passes'] else '—'} / {c['retrace'] if c['retrace'] is not None else '—'}</td></tr><tr><td>配比</td><td>{mix}</td></tr><tr><td>规格</td><td>{c['spec'] or '—'}</td></tr></table>{pngs}</div>")
h.append("</div>")
if not cards: h.append("<p>还没有任何一集落地。</p>")
(SITE / 'index.html').write_text('\n'.join(h)); print('episodes on page:', len(cards))
