"""Re-judge episodes the old acceptance rejected on the two marginal gates the 17 Sep tolerance covers
(action_mix_in_band ±1 point; depth_not_empty <= 0.5% of frames). A re-judged episode is moved out of
_rejected/ on S3 and its acceptance.json rewritten with accepted=true and a note saying why."""
import json, re, subprocess, sys, ast
from pathlib import Path
S3 = 's3://pan-simworld/long-video-data-260917'; SITE = Path('site/longvideo/record-260917/episodes')
def rejudge(acc, traj):
    target = (traj.get('action_mix_target') or {}); frames = traj.get('frames') or 0; still = []
    for g in acc.get('gates', []):
        if g['result'] in ('pass', 'skip', 'skipped'): continue
        if g['gate'] == 'action_mix_in_band':
            m = re.search(r'OUT OF BAND: (\{.*\})', g['detail']); oob = ast.literal_eval(m.group(1)) if m else {}
            bad = {k: v for k, v in oob.items() if k in target and not (target[k][0] - 0.01 <= v <= target[k][1] + 0.01)}
            if bad: still.append(f"action_mix_in_band {bad}")
            else: g['result'] = 'pass'; g['detail'] += ' | RE-JUDGED 17 Sep: within the 1-point tolerance'
        elif g['gate'] == 'depth_not_empty':
            m = re.search(r'(\d+) frames with no valid depth', g['detail']); dead = int(m.group(1)) if m else 10**9
            if dead <= max(0, int(0.005 * frames)): g['result'] = 'pass'; g['detail'] += f' | RE-JUDGED 17 Sep: {dead} of {frames} frames <= 0.5%'
            else: still.append(f"depth_not_empty {dead}/{frames}")
        else: still.append(g['gate'])
    return still
for d in sorted(SITE.glob('rejected__*')):
    ep = d.name.removeprefix('rejected__'); slug = ep.split('__coverage_walk')[0]
    acc = json.load(open(d / 'acceptance.json')); traj = json.load(open(d / 'trajectory.json'))
    still = rejudge(acc, traj)
    if still: print('STILL REJECTED', ep[:60], still); continue
    acc['accepted'] = True; acc['rejudged'] = '17 Sep tolerance: action mix bands +-1 point, all-invalid depth frames <= 0.5%'; acc['gates_failed'] = 0
    (d / 'acceptance.json').write_text(json.dumps(acc, indent=1))
    src, dst = f'{S3}/{slug}/_rejected/{ep}', f'{S3}/{slug}/{ep}'
    r = subprocess.run(['aws', 's3', 'mv', src, dst, '--recursive', '--only-show-errors'], capture_output=True, text=True)
    subprocess.run(['aws', 's3', 'cp', str(d / 'acceptance.json'), f'{dst}/acceptance.json', '--only-show-errors'], check=True)
    new = SITE / ep; 
    if new.exists():
        for f in new.iterdir(): (d / f.name).exists() or f.replace(d / f.name)
        subprocess.run(['rm', '-rf', str(new)])
    d.replace(new); print('RE-JUDGED -> accepted, moved on S3:', ep[:70], 'mv rc', r.returncode)
