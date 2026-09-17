#!/usr/bin/env python3
"""levels_plan.json: one row per delivered level, classified for recording. Rebuild after any validation run:
    python3 build_levels_plan.py            (then build_map_plan_page.py)
Inputs: the delivery checklist + 地图索引 (project dirs, .umap sizes), the four validation result sets
(newest finished result wins, a geometry_pass anywhere wins over anything), core sizes (09-17 queue ->
09-09 survey -> 09-16 offline), the recorded-episode list, and the 16 Sep drop list."""
import csv, json, glob, re, math, collections
from pathlib import Path
OUT = Path('/tmp/claude-1000/-home-ubuntu-UE5-Agent-Data/ef273e00-f904-4c7f-a0cb-55c0cd005dbc/scratchpad/levels_plan.json')
NM = Path('/home/ubuntu/new_map'); SITE = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo')
RESULT_DIRS = ['/home/ubuntu/ue_route_validation_20260916/maps', '/home/ubuntu/ue_newmap_validation_20260916/maps', '/home/ubuntu/ue_newroute_fleet_20260916/maps', '/home/ubuntu/ue_newroute_fleet_20260917/maps']
DROP = {'Game_HotelCorridor_Maps_Hotel_Corridor', 'Game_OperatingRoom_Levels_Operating_Room', 'Game_QA_HoldingCells_Maps_QA_Holding_Cells_A', 'Game_QA_HoldingCells_Maps_QA_Holding_Cells_B', 'Game_Wild_West_Maps_WildWest', 'Game_Chinese_Landscape_Maps_Chinese_Landscape_Demo', 'Game_Dungeon_Maps_Dungeon_Demo_00', 'Game_AnchientRuins_Maps_AncientRuins', 'Game_Real_Landscape_Maps_Real_Landscape', 'Game_StonePineForest_Maps_Traditional_Map', 'Game_Hangar_Maps_Hangar', 'Game_Factory_Maps_Factory', 'Game_SwimmingPool_Maps_Demonstration_Master'}
BLOCKED = {'Game_Maps_Demonstration': 'Factory Collection 资产全在通用目录，合不进 gym_citynav，需单独立工程'}
RATIO, GB_PER_H, PASS2 = 2.5, 166 / 108, 2.0
rows = list(csv.DictReader(open(NM / '验收清单.csv', encoding='utf-8-sig')))
idx = (NM / '地图索引.md').read_text(encoding='utf-8')
proj_of = {m.group(1): (m.group(2).strip(), m.group(3)) for m in re.finditer(r'## (Fab_\d+) · (.*?)\n.*?项目入口：\[Projects/([^/\]]+)/', idx, re.S)}
res = {}
for d in RESULT_DIRS:
    for f in glob.glob(f'{d}/*/result.json'):
        slug = f.split('/')[-2]
        try: r = json.load(open(f))
        except Exception: continue
        st = r.get('state'); a = (r.get('attempts') or [{}])[-1]; rn = a.get('road_network') or {}
        rec = dict(state=st, kept=a.get('kept_fraction'), cl=rn.get('centreline_m'), roads=rn.get('roads'), frames=a.get('frames'),
                   failed=a.get('failed_gates') or [], advisory=a.get('advisory_failed') or [], core=(r.get('core') or {}).get('core_m2'),
                   fin=r.get('finished_epoch') or 0, src=d.split('/')[3], err=(r.get('error') or a.get('error') or '')[:100])
        p = res.get(slug)
        if p is None or (st == 'geometry_pass', rec['fin']) > (p['state'] == 'geometry_pass', p['fin']): res[slug] = rec
core09 = {r['map']: r['core_m2'] for r in json.load(open('/home/ubuntu/UE5-Agent-Data/revisit_pipeline/results/core_survey_2026-09-09/core_survey.json')) if 'error' not in r}
off = json.load(open('/home/ubuntu/ue_newmap_validation_20260916/core_offline/core_offline.json'))
def core(slug):
    r = res.get(slug)
    if r and r.get('core') is not None: return r['core']
    if slug in core09: return core09[slug]
    o = off.get(slug)
    return o['core_m2'] if o and 'error' not in o else None
recorded = set(json.load(open(SITE / 'core' / 'recorded_slugs.json')))
tried = set()
for mf in ['/home/ubuntu/ue_route_validation_20260916/manifest.json', '/home/ubuntu/ue_newmap_validation_20260916/manifest.json', '/home/ubuntu/ue_newroute_20260916/manifest.json', '/home/ubuntu/ue_newroute_20260917/manifest.json']:
    tried |= {e['slug'] for e in json.load(open(mf))}
L = []
for row in rows:
    fid = row['项目编号']; title, pdir = proj_of.get(fid, (row['资源名称'], None))
    for lp in [x.strip() for x in row['关卡路径'].split(';') if x.strip()]:
        rel = lp.replace('/Game/', '', 1); slug = 'Game_' + rel.replace('/', '_')
        um = NM / 'Projects' / (pdir or '') / 'Content' / (rel + '.umap'); mb = round(um.stat().st_size / 1e6, 1) if pdir and um.exists() else None
        r = res.get(slug, {}); st = r.get('state'); cl = r.get('cl'); rec = slug in recorded
        if fid == 'Fab_018' or fid == 'Fab_038': cls, note = 'F 排除', ''
        elif slug in BLOCKED: cls, note = 'F 排除', BLOCKED[slug]
        elif st == 'geometry_pass' or rec: cls, note = 'A 可直接录', ('配比差一点，录制时两遍一起判' if r.get('advisory') else '')
        elif st == 'coverage_review': cls, note = 'B 待审', f"保留率 {r.get('kept'):.0%}" if r.get('kept') else ''
        elif st in ('failed', 'timeout', 'running', 'interrupted', 'starting', 'connecting'): cls, note = 'C 重跑', (r.get('err') or st)
        elif st in ('route_failed', 'blocked_asset'):
            if slug in DROP or 'network_too_short' in r.get('failed', []) or (cl is not None and cl < 50): cls, note = 'G 放弃', f"路网 {cl:.0f} m 太短" if cl else '太小'
            elif 'no usable road network' in r.get('err', '') or 'area floor' in r.get('err', ''): cls, note = 'G 放弃', '导航网合不出走廊，纯地形'
            elif 'blocked at the agent' in r.get('err', ''): cls, note = 'G 放弃', '每条路在头部高度都被挡'
            else: cls, note = 'D 修路线', ', '.join(g for g in r.get('failed', []) if g not in ('action_mix_in_band', 'retrace_cadence_ok')) or r.get('err', '')
        elif slug in tried: cls, note = 'E 未验证', '排过队没跑到'
        else: cls, note = 'H 未挑选', ''
        x = dict(fid=fid, title=title, project=pdir, level=lp, slug=slug, cls=cls, state=st, note=note, core=core(slug), mb=mb, recorded=rec,
                 cl=cl, roads=r.get('roads'), kept=r.get('kept'), src=r.get('src'), tried=slug in tried)
        one = r['frames'] / 24 / 60 if r.get('frames') else None
        x['one_pass_min'] = round(one, 1) if one else None
        if cls.startswith('A'):
            ep = (1 + PASS2) * one / 60 if one else 9.0
            x.update(episode_h=round(ep, 1), shards=1, machine_h=round(ep * RATIO, 1), gb=round(ep * GB_PER_H), tier='大' if ep >= 8 else '中' if ep >= 2 else '小')
        L.append(x)
json.dump(L, open(OUT, 'w'), ensure_ascii=False, indent=1)
c = collections.Counter(x['cls'] for x in L); print(dict(sorted(c.items())))
A = [x for x in L if x['cls'].startswith('A')]; print('A', len(A), 'episode h', round(sum(x['episode_h'] for x in A)), 'machine h', round(sum(x['machine_h'] for x in A)))
print('C 重跑:', [(x['slug'][5:38], x['state']) for x in L if x['cls'].startswith('C')])
