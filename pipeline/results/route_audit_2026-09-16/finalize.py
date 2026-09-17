from pathlib import Path
import json,shutil,collections
import numpy as np
R=Path(__file__).resolve().parent
repo=Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline')
data=json.loads((R/'audit.json').read_text())
assert len(data['maps'])==86 and len({x['slug'] for x in data['maps']})==86
assert sum(bool(x.get('visually_reviewed')) for x in data['maps'])==73
assert sum(bool(x.get('input_reviewed')) for x in data['maps'])==13
checks=[]
for f in (R/'cloud').glob('*/route_sample.json'):
 d=json.loads(f.read_text());assert d['csv_frames_read']==d['episode']['frames'];p=np.array(d['poses']);assert np.isfinite(p).all() and (np.diff(p[:,0])>0).all()
 checks.append({'episode':d['episode']['episode_id'],'actual_csv_frames':d['csv_frames_read'],'sampled_poses':len(p)})
assert len(checks)==11
for f in (R/'drafts').glob('*.json'):
 d=json.loads(f.read_text());assert d['state']=='draft_ready' and d['unwalked_roads']==0 and np.isfinite(d['polyline_xy_cm']).all()
report={'scope':86,'visual_reviews':73,'input_reviews':13,'new_offline_drafts':59,'cloud_metadata_episodes':len(list((R/'cloud').glob('*/trajectory.json'))),'sampled_cloud_routes':checks,'status_counts':data['status_counts'],'note':'Offline route graph traversal is not UE collision/depth/camera validation. Gray navmesh overlays are cached and may differ from historical exports. Historical trajectory coverage fields are not recomputed actual coverage.'}
(R/'integrity.json').write_text(json.dumps(report,indent=2))
out=repo/'results/route_audit_2026-09-16';out.mkdir(exist_ok=True)
for pat in ['*.py','*.json']:
 for p in R.glob(pat):shutil.copy2(p,out/p.name)
for p in (R/'cloud').glob('*/trajectory.json'):
 dst=out/'historical_metadata'/p.parent.name;dst.mkdir(parents=True,exist_ok=True)
 for name in ['trajectory.json','acceptance.json']:
  shutil.copy2(p.parent/name,dst/name)
doc='''# 86-map route audit, 2026-09-16

Scope: all 86 start-position catalog entries. No UE capture launched and no existing
route or media overwritten. Read 71 historical cloud trajectory/acceptance records,
sampled actual frames.csv at stride 12 from one representative per 11 maps, and
used one additional local actual engine-state route. Representatives prefer
accepted runs, then larger seeds; they are not the union of all episodes.

Existing diagrams: 12 actual routes, 2 frozen coverage plans, 9 short out-and-back
plans. Generated 59 new offline coverage previews for 50 previously route-less maps
and the 9 short-route maps, using the existing build_network(core_only=False,
min_clear_cm=90) and coverage_walk(seed=8600) on cached navmeshes.
These are spatial drafts: no engine collision pruning, depth probes, camera timing
or per-frame route gates were run. They are not recording-ready frozen tasks.

Visually reviewed route diagrams for all 73 maps with navigation survey data.
Input-checked the remaining 13: 2 files have no numeric starts, 1 slug has no
matching on-disk map package, and 10 retain their historical invalid-spawn/navmesh
issue. Those 10 have numeric positions and assets; ground was not re-tested in UE.

Results: 30 priority region/route checks; 34 offline drafts awaiting engine
validation; 6 existing main routes may be reused subject to pre-recording checks;
3 old routes with failed prechecks; 13 requiring start/nav input repair.
Every map has a specific review in audit.json. Priority means suspected missing
or poorly chosen region, not proof that all gray areas are walkable.

ChemicalPlant_2 representative route stays mainly in the lower part of the cached
map and retains 46.85% of its pre-pruning network. Tokyo's representative mostly
misses the upper built-up area despite retaining 93.08% of its chosen network.
Hwaseong later rounds improved retention to 81.5%, but the pictured representative
still mostly covers one courtyard. TemplePlaza later rounds retain about 98.9%
and the representative spans both main areas. These distinguish graph retention
from correct region selection and from visibility coverage.

Historical coverage fractions on the page are from trajectory reports, not a new
recomputation against actual delivered poses. Map backgrounds are local cached
navmeshes, not authoritative same-run exports or rendered overhead photographs.
Top-down projections cannot establish connectivity between floors. Orange/green
core-survey images are obstacle-density heuristics, not semantic building labels.

Review gallery: http://51.20.82.218:8500/longvideo/route-audit/
Raw audit, sampled actual trajectories, draft paths and plots:
/home/ubuntu/ue_route_audit_20260916/
Website includes search, status filters, original report links and full-size plots.
Old Dubai first-person/top-view and previous comparison galleries remain intact.
'''
(out/'README.md').write_text(doc)
for fn in ['FINDINGS.md','ALL_MAP_RENDER_PLAN.md']:
 p=repo/fn;s=p.read_text();mark='## 2026-09-16：86 张路线审查完成'
 if mark not in s:
  with p.open('a') as f:f.write('\n\n'+mark+'\n\n73 张已查看路线/草案俯视图；13 张核对输入问题。新增 59 张离线草案，未启动 UE 渲染或碰撞验证。\n30 张优先检查补区域，34 张草案待引擎验证，6 张可沿用主体后预录验收，3 张旧预检失败，13 张修起点/导航。\n逐图意见与证据见 `results/route_audit_2026-09-16/` 和 8500 的 `/longvideo/route-audit/`。\n旧覆盖率只针对选定/裁剪后的网络，不能证明全图或可见立面覆盖；不要直接开跑所有旧路线。\n')
print(json.dumps({'maps':86,'statuses':data['status_counts'],'archive':str(out)},ensure_ascii=False))
