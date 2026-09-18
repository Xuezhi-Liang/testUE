"""Re-judge episodes rejected ONLY by speed_pinned_held, with the planar rule; move them out of _rejected."""
import json, csv, math, subprocess, sys
from pathlib import Path
RUN = sys.argv[1] if len(sys.argv) > 1 else '260918'; S3 = f's3://pan-simworld/long-video-data-{RUN}'; SITE = Path(f'site/longvideo/record-{RUN}/episodes')
for d in sorted(SITE.iterdir()):
    acc_p = d / 'acceptance.json'
    if not acc_p.exists() or not (d / 'frames.csv').exists(): continue
    acc = json.load(open(acc_p))
    if acc.get('accepted'): continue
    failed = [g for g in acc['gates'] if g['result'] not in ('pass', 'skip', 'skipped', 'warn')]
    # action_mix_in_band is advisory since 18 Sep: downgrade it to 'warn' here as well
    for g in failed:
        if g['gate'] == 'action_mix_in_band': g['result'] = 'warn'; g['detail'] += ' | RE-JUDGED 18 Sep: advisory'
    failed = [g for g in failed if g['result'] != 'warn']
    if not failed:
        acc['accepted'] = True; acc['gates_failed'] = 0; acc['rejudged'] = '18 Sep: action mix advisory'
        acc_p.write_text(json.dumps(acc, indent=1)); ep = d.name; slug = ep.split('__coverage_walk')[0]
        r = subprocess.run(['aws', 's3', 'mv', f'{S3}/{slug}/_rejected/{ep}', f'{S3}/{slug}/{ep}', '--recursive', '--only-show-errors'], capture_output=True, text=True)
        subprocess.run(['aws', 's3', 'cp', str(acc_p), f'{S3}/{slug}/{ep}/acceptance.json', '--only-show-errors'], check=True); print(d.name[:50], 'mix-only -> accepted, moved rc', r.returncode); continue
    if [g['gate'] for g in failed] != ['speed_pinned_held']: print('not speed-only:', d.name[:50], [g['gate'] for g in failed]); continue
    rows = list(csv.DictReader(open(d / 'frames.csv'))); fps = 24.0
    wk = [r for r in rows if r.get('phase') in ('forward', 'backward')]; v = []
    for a, b in zip(wk, wk[1:]):
        if int(b['frame_id']) != int(a['frame_id']) + 1: continue
        v.append(math.hypot(float(b['actual_x_cm']) - float(a['actual_x_cm']), float(b['actual_y_cm']) - float(a['actual_y_cm'])) / 100 * fps)
    v.sort(); med = v[len(v) // 2]; p95 = v[int(len(v) * 0.95)]
    ok = abs(med - 1.0) <= 0.02 and p95 <= 1.02
    print(d.name[:50], f'planar median {med:.3f} p95 {p95:.3f} ->', 'PASS' if ok else 'still fails')
    if not ok: continue
    for g in failed: g['result'] = 'pass'; g['detail'] += f' | RE-JUDGED planar: median {med:.3f}, p95 {p95:.3f} (the 3D step counted slope + vertical slew)'
    acc['accepted'] = True; acc['gates_failed'] = 0; acc['rejudged'] = '17 Sep: speed_pinned_held re-measured in the XY plane'
    acc_p.write_text(json.dumps(acc, indent=1))
    ep = d.name; slug = ep.split('__coverage_walk')[0]
    r = subprocess.run(['aws', 's3', 'mv', f'{S3}/{slug}/_rejected/{ep}', f'{S3}/{slug}/{ep}', '--recursive', '--only-show-errors'], capture_output=True, text=True)
    subprocess.run(['aws', 's3', 'cp', str(acc_p), f'{S3}/{slug}/{ep}/acceptance.json', '--only-show-errors'], check=True)
    print('  moved out of _rejected, rc', r.returncode)
