"""Assemble the bundle, publish the task queue to S3, and record what went out.

The queue is the manifest: ordered longest-estimate first so the pull loop behaves as an LPT
schedule. Nothing here assigns a map to a machine.
"""
import json, shutil, hashlib, tarfile, subprocess
from pathlib import Path
F = Path('/home/ubuntu/ue_newroute_fleet_20260917'); S = F / 'bundle'
R = Path('/home/ubuntu/ue_newroute_20260917')
P = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline')
NEW = Path('/home/ubuntu/ue_newmap_validation_20260916')
B, PRE = 'pan-simworld', 'ue-newroute/20260917/'

man = json.loads((R / 'manifest.json').read_text())
newman = {m['slug']: m for m in json.loads((NEW / 'manifest.json').read_text())}
for m in man:                       # pack sync needs the delivery project directory name
    m['project_dir'] = newman.get(m['slug'], {}).get('project_dir')
    m['packs_to_sync'] = [p for p in (m.get('packs_to_sync') or []) if m['project_dir']]
(R / 'manifest.json').write_text(json.dumps(man, ensure_ascii=False, indent=1))

(S / 'runtime/catalog').mkdir(parents=True, exist_ok=True)
(S / 'pipeline/cpp').mkdir(parents=True, exist_ok=True)
for p in P.glob('*.py'): shutil.copy2(p, S / 'pipeline' / p.name)
for p in (P / 'cpp').iterdir():
    if p.is_file() and p.suffix in {'.cpp', '.h', '.py'}: shutil.copy2(p, S / 'pipeline/cpp' / p.name)
shutil.copy2(P.parent / 'launch_ue_fast.sh', S / 'launch_ue_fast.sh')
# start points: the catalogue's own file where there is one, the empty stub the new maps use
# otherwise. Every task runs with recover_start, so a stub still yields PlayerStart / mesh /
# landscape / union candidates in the engine.
CAT = P.parent.parent / 'batch_inference/start_positions'
n_real = n_stub = 0
for m in man:
    s = m['slug']; src = CAT / f'{s}.json'
    if src.exists(): shutil.copy2(src, S / 'runtime/catalog' / f'{s}.json'); n_real += 1
    elif (NEW / 'catalog' / f'{s}.json').exists(): shutil.copy2(NEW / 'catalog' / f'{s}.json', S / 'runtime/catalog' / f'{s}.json'); n_stub += 1
    else: (S / 'runtime/catalog' / f'{s}.json').write_text(json.dumps({'map_id': m['map_id'], 'positions': []})); n_stub += 1
shutil.rmtree(S / 'catalog', ignore_errors=True)

tar = F / 'bundle.tar.gz'
with tarfile.open(tar, 'w:gz') as t: t.add(S, arcname='bundle')
digest = hashlib.sha256(tar.read_bytes()).hexdigest()
key = f'{PRE}ops/bundle-{digest[:16]}.tar.gz'
subprocess.run(['aws', 's3', 'cp', str(tar), f's3://{B}/{key}', '--only-show-errors'], check=True)
subprocess.run(['aws', 's3', 'cp', str(R / 'manifest.json'), f's3://{B}/{PRE}manifest.json', '--only-show-errors'], check=True)
(F / 'bundle_manifest.json').write_text(json.dumps({'key': key, 'sha256': digest, 'bytes': tar.stat().st_size,
                                                    'tasks': len(man)}, indent=1))
print(f'bundle {tar.stat().st_size / 1e6:.1f} MB -> s3://{B}/{key}')
print(f'queue: {len(man)} tasks, {sum(m["est_s"] for m in man) / 3600:.1f} h estimated, '
      f'{sum(1 for m in man if m["packs_to_sync"])} need a content pack; starts {n_real} real / {n_stub} stub')
