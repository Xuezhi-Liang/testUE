"""Pull frozen route + nav from the workers for every finished map and draw the network/route plot.
Rerunnable: skips maps already plotted. Plots land in the site's route-queue-20260917/plots/."""
import json, glob, subprocess, sys
from pathlib import Path
F = Path('/home/ubuntu/ue_newroute_fleet_20260917')
SITE = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/route-queue-20260917'); (SITE / 'plots').mkdir(parents=True, exist_ok=True)
sys.path.insert(0, '/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline')
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
import plan_navmesh as pnm
st = json.load(open(SITE / 'status.json')); w_of = {r['slug']: r['worker'] for r in st['rows'] if r['worker']}
out = subprocess.run(['aws','ec2','describe-instances','--region','eu-north-1','--filters','Name=tag:NewRouteRun,Values=20260917','Name=instance-state-name,Values=running','--query','Reservations[].Instances[].[Tags[?Key==`NewRouteWorker`].Value|[0],PrivateIpAddress]','--output','text'],capture_output=True,text=True).stdout
ips = dict(line.split() for line in out.strip().splitlines() if line.strip())
done = 0
for f in sorted(glob.glob(str(F / 'maps/*/result.json'))):
    slug = Path(f).parent.name; r = json.load(open(f)); a = (r.get('attempts') or [{}])[-1]
    target = SITE / 'plots' / f'{slug}.png'
    if target.exists() or not a.get('frozen'): continue
    ip = ips.get(w_of.get(slug))
    if not ip: print('no instance for', slug); continue
    local = F / 'maps' / slug / 'frozen'; local.mkdir(parents=True, exist_ok=True)
    fz_remote = a['frozen']; nav_remote = str(Path(fz_remote).parent / 'nav')
    subprocess.run(['scp','-o','StrictHostKeyChecking=no','-o','ConnectTimeout=8','-q',f'ubuntu@{ip}:{fz_remote}', str(local / 'route.json')])
    subprocess.run(['scp','-o','StrictHostKeyChecking=no','-o','ConnectTimeout=8','-q',f'ubuntu@{ip}:{nav_remote}/{slug}.bin', f'ubuntu@{ip}:{nav_remote}/{slug}.json', str(local)])
    try:
        fz = json.load(open(local / 'route.json')); poses = fz.get('poses', [])
        nav, _ = pnm.load(str(local / f'{slug}.bin'), str(local / f'{slug}.json'))
    except Exception as e:
        print('fetch failed', slug, e); continue
    fig, ax = plt.subplots(figsize=(9, 7), facecolor='#101b29'); ax.set_facecolor('#101b29')
    tris = nav.verts[nav.wtris][:, :, :2] / 100
    from matplotlib.collections import PolyCollection
    ax.add_collection(PolyCollection(tris, facecolor='#2d3e50', edgecolor='#34495e', linewidth=0.2))
    if poses:
        xs = [p['x_cm'] / 100 for p in poses]; ys = [p['y_cm'] / 100 for p in poses]
        ax.plot(xs, ys, color='#7fe0d6', linewidth=0.9, label='route'); ax.plot(xs[0], ys[0], 'o', color='#ffd166', label='Start')
    ax.autoscale(); ax.set_aspect('equal'); ax.tick_params(colors='#bacbdd'); ax.set_xlabel('X / m', color='#bacbdd'); ax.set_ylabel('Y / m', color='#bacbdd')
    rn = a.get('road_network') or {}
    ax.set_title(f"{slug.removeprefix('Game_')}\n{r.get('state')} · centreline {rn.get('centreline_m', 0):.0f} m · roads {rn.get('roads', '?')} · kept {(a.get('kept_fraction') or 0):.0%}", color='white', fontsize=10)
    ax.legend(); fig.tight_layout(); fig.savefig(target, dpi=110); plt.close(fig); done += 1; print('plotted', slug)
print('new plots', done)
