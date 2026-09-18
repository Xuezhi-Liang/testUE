"""User's decision 18 Sep: nothing recorded is rejected. Move every _rejected/ episode of the run to the
normal prefix and mark its acceptance.json accepted with the original verdicts kept as the quality record."""
import json, subprocess, sys
from pathlib import Path
RUN = sys.argv[1] if len(sys.argv) > 1 else '260918'; S3 = f's3://pan-simworld/long-video-data-{RUN}'; SITE = Path(f'site/longvideo/record-{RUN}/episodes')
def ls(prefix):
    r = subprocess.run(['aws', 's3', 'ls', prefix], capture_output=True, text=True); return [l.split()[-1].rstrip('/') for l in r.stdout.splitlines() if 'PRE' in l]
for slug in ls(S3 + '/'):
    if slug.startswith('_'): continue
    for ep in ls(f'{S3}/{slug}/_rejected/'):
        src, dst = f'{S3}/{slug}/_rejected/{ep}', f'{S3}/{slug}/{ep}'
        local = SITE / ep; local.mkdir(parents=True, exist_ok=True)
        subprocess.run(['aws', 's3', 'cp', f'{src}/acceptance.json', str(local / 'acceptance.json'), '--only-show-errors'])
        acc = json.load(open(local / 'acceptance.json')) if (local / 'acceptance.json').exists() else {}
        acc['gates_would_have_failed'] = [g['gate'] for g in acc.get('gates', []) if g.get('result') == 'FAIL']
        acc['accepted'] = True; acc['verdict_mode'] = 'advisory'; acc['rejudged'] = "18 Sep, user's decision: nothing recorded is rejected; gate verdicts kept as the quality record"
        (local / 'acceptance.json').write_text(json.dumps(acc, indent=1))
        print('moving', ep[:70], 'would-have-failed:', acc['gates_would_have_failed'], flush=True)
        subprocess.run(['aws', 's3', 'mv', src, dst, '--recursive', '--only-show-errors'])
        subprocess.run(['aws', 's3', 'cp', str(local / 'acceptance.json'), f'{dst}/acceptance.json', '--only-show-errors'])
print('done')
