from pathlib import Path
import json,shutil
r=Path(__file__).resolve().parent;repo=Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline');out=repo/'results/general_stability_2026-09-16';out.mkdir(exist_ok=True)
cfg=json.loads((r/'config.json').read_text());validation=json.loads((r/'validation.json').read_text());assert len(validation['recordings'])==16
rows=[];native={}
for name,mc in cfg['maps'].items():
 mm=json.loads((r/name/'metrics.json').read_text())['variants'];base=mm['baseline']
 for tag,v in mm.items():
  native[name+'/'+tag]=json.loads((r/name/tag/'runtime.json').read_text())
  if tag=='baseline':continue
  delta=lambda a,b:(a/b-1)*100
  a=v['building']['moving'];b=base['building']['moving']
  rows.append({'map':name,'variant':tag,'building_flash_pct':a['sparkle20'],'baseline_building_flash_pct':b['sparkle20'],'building_flash_change_pct':delta(a['sparkle20'],b['sparkle20']),'building_edge_change_pct':delta(a['edge'],b['edge']),'building_laplacian_change_pct':delta(a['lap'],b['lap']),'whole_translation_flash_change_pct':delta(v['moving']['sparkle20'],base['moving']['sparkle20']),'turn_residual_change_pct':delta(v['turn']['residual'],base['turn']['residual']),'static_difference_change_pct':delta(v['static']['raw_diff'],base['static']['raw_diff']),'native_fps':native[name+'/'+tag]['native']['fps']})
summary={'decision':'Do not replace the global default. No tested candidate establishes no-flicker behavior with guaranteed preserved building detail across maps. Keep two explicit experimental options, the original TSR default and all old captures.','maps_tested':4,'recordings':16,'duration_each_s':25,'total_native_frames':9600,'metrics_note':'Fixed baseline flow and manually selected first-8-second building/depth regions; proxies include occlusion/flow error and noise. Four routes are not fleet-wide validation. Simulation/lighting history is not bit-identical between runs.','rows':rows}
(r/'summary.json').write_text(json.dumps(summary,indent=2));(out/'runtime_variants.json').write_text(json.dumps(native,indent=2))
decision={'complete':True,'selected':'taa_balanced','status':'已完成 4 张地图 × 4 组设置。TAA 2× 高分辨率历史较均衡：三张图建筑闪点下降约 23%–34%，森林接近不变。仍有细纹差异，原通用默认保留。','decision_note':'2× TAA 候选的建筑边缘强度约为原来的 98%–100%，但细纹与噪声混合的高频量下降约 13%–18%，不能保证纹理无损或零闪动。3× 方案在 Downtown 反而增加建筑与全画面闪点。候选配置另存，原默认和旧结果保留。'}
(r/'decision.json').write_text(json.dumps(decision,ensure_ascii=False,indent=2))
table='| Map | Candidate | Building flash change | Building edge change | Building high-frequency change | Whole-frame walk flash change | Turn residual change | Static difference change | Native fps |\n|---|---|---:|---:|---:|---:|---:|---:|---:|\n'
for x in rows:table+='| '+x['map']+' | '+x['variant']+' | '+' | '.join(f"{x[k]:+.1f}%" for k in ['building_flash_change_pct','building_edge_change_pct','building_laplacian_change_pct','whole_translation_flash_change_pct','turn_residual_change_pct','static_difference_change_pct'])+f" | {x['native_fps']:.2f} |\n"
body='''

## 2026-09-16 — Cross-map settings, building detail is the hard priority

User requested a common setting for all maps, then explicitly prioritized building detail.
Completed 4 maps × 4 settings × 600 frames = 9,600 native RGB/depth/pose records, sixteen
25-second videos. Maps: ChemicalPlant 2, Downtown West, Hwaseong and ForestGasStation.
All matching route prefixes include still, translation and turn phases. Native capture mode
4, 32 rendered warmups, 1280x720/24 fps, native unfiltered depth and authored assets are common.

**Decision: do not replace the global TSR 2x default.** No tested setting establishes the
requested all-map zero-flicker guarantee while preserving all fine building detail. Higher
internal resolution and history resolution are not uniformly better. Keep these reproducible
options, explicitly marked experimental, in source and deployed tasks:

- `tasks/detail_first_candidate.json`: TAA, 3x RGB, 200% TAA history resolution.
- `tasks/stability_detail_candidate.json`: the same quality settings with 2x RGB.

Both use `r.TemporalAA.Quality 2`, `r.TemporalAACurrentFrameWeight 0.04`,
`r.TemporalAASamples 8`, `r.Lumen.ScreenProbeGather.DownsampleFactor 8`,
`r.MaxAnisotropy 16`, `r.MipMapLODBias 0`, `r.Tonemapper.Sharpen 0`, and explicit fog-grid
8/128 (the latter already matched local defaults). No fog/reflections/material/geometry
removal. The third alternative is TSR 3x with the same denser GI and history sample count 32.
The source/deployed global `longvideo_template.json` retains the original TSR 2x settings.
Existing frozen tasks keep their own settings. Start a fresh editor when changing standalone
profiles; the test runner explicitly reset all relevant cvars between candidates.

Fixed fill within each map was 1.5 / 3 / 4 / 1 to isolate rendering changes; these are not
new universal lighting constants. General candidate templates keep automatic fill. Forest
is a dark authored scene; these tests do not certify exposure/calibration acceptance there.

### Measured changes relative to each map's current TSR 2x baseline

Negative flash/residual/difference changes mean less measured temporal variation. Edge and
Laplacian/high-frequency changes do **not** prove preserved/lost real detail: both contain
noise, so raw crops and video inspection are essential. Metrics use the same baseline flow
for every candidate. Building measurements select first-8-second fixed ROIs intersected
with depth, not semantic instance masks; the station ROI includes surrounding vegetation.
Full-frame translation/turn/static values are kept to expose regressions outside buildings.
'''+table+'''
The 3x TAA variant retained readily visible wall panels, window frames and roof-tile lines
in sampled frames, but Downtown building flash also increased, and the whole-frame walking flash proxy rose from 0.155% to
0.312%. A lower local flash metric cannot justify declaring the whole scene fixed. The 2x
TAA/200%-history option lowers Chemical and Hwaseong building high-frequency energy by
roughly 16–18%; part may be removed alias/noise, but lossless fine texture is not established.
TAA 2x/200% history is the most balanced candidate in this bounded trial: opening building flash decreases about 24% Chemical, 23% Downtown, 34% Hwaseong, and 1% Forest (effectively unchanged). Building edge strength stays around 98–100% of baseline, while the high-frequency proxy falls about 13–18%; this does not certify texture losslessness. Remaining flicker is measurable in all settings. No global default promotion.

### Integrity, reproducibility and limits

Every capture wrote exactly 600 RGB JPEGs, 600 EXRs and 600 pose records, zero write
failures, zero position and wrapped-rotation error. All actual changed cvars were verified.
128 sampled EXRs were finite BGRA, R positive metres or -1 sky; all stayed 1280x720.
Encoded videos are 600 frames/24 fps/25 seconds. RGB JPEG quality 92 and review H.264 CRF18
are unchanged. No temporal video filtering or frame synthesis was used.

This is rendering validation, not collision/route acceptance, a proof of perceptual detail
equivalence, or validation of every map. Repeated world lighting/cache histories are not
bit-identical. Metrics retain both raw values and adverse changes. UE processes are stopped
after captures; website remains on port 8500. Original Dubai, first-person/top-view assets,
chemical comparisons and previous full videos are untouched.

Raw artifacts and runnable original scripts: `/home/ubuntu/ue_general_stability_20260916/`.
Compact audit snapshots: `results/general_stability_2026-09-16/`. Web with paired videos,
click-to-select building magnification and cross-map tables: `/longvideo/general-stability/`.
Installed UE 5.8 source confirmed variable names/defaults; primary background references:
[Epic AA overview](https://dev.epicgames.com/documentation/unreal-engine/anti-aliasing-and-upscaling-in-unreal-engine)
and [TSR FAQ](https://dev.epicgames.com/documentation/unreal-engine/temporal-super-resolution-frequently-asked-questions-for-unreal-engine).
'''
if not (r/'docs_written').exists():
 with (repo/'RENDER_QUALITY.md').open('a') as f:f.write(body)
 with (repo/'FINDINGS.md').open('a') as f:f.write('''

### 2026-09-16 — Four-map detail-first anti-flicker trial, no global promotion

User requested common settings while preserving building details. Recorded ChemicalPlant,
Downtown West, Hwaseong and ForestGasStation: baseline TSR 2x, TAA 3x/200% history,
TSR 3x/high GI, TAA 2x/200% history. 16 × 25 s, 9,600 native RGB/depth/pose records,
zero write failures and tracked pose error. All changed runtime cvars verified. Fixed
per-map fill; unchanged native depth, assets and video encoding.

No candidate establishes all-map zero-flicker/detail preservation. TAA 3x improves several
building regions while preserving visible edges, but Downtown full-frame walk flash proxy
doubles from 0.155% to 0.312%. TAA 2x/200%-history has smaller flash metrics on some maps
but lower fine-frequency energy, whose detail/noise components cannot be certified apart.
Global TSR 2x template remains unchanged. Optional source/deployed templates:
`tasks/detail_first_candidate.json` and `tasks/stability_detail_candidate.json` (experimental).
Tables, raw metrics, limitations and sources: `RENDER_QUALITY.md`;
`results/general_stability_2026-09-16/`; raw `/home/ubuntu/ue_general_stability_20260916/`;
review `/longvideo/general-stability/` on 8500. Old pages and media preserved.
''')
 (r/'docs_written').touch()
for name in ['config.json','building_regions.json','summary.json','validation.json','decision.json','sources.json','record.py','run.py','measure.py','publish.py','check_page.py','validate.py']:
 shutil.copy2(r/name,out/name)
for name in cfg['maps']:
 shutil.copy2(r/name/'metrics.json',out/(name+'_metrics.json'))
(out/'README.md').write_text((out/'README.md').read_text().replace('Final decisions, measured results and verification will be appended after capture completes.','Completed: sixteen 25-second videos / 9,600 frames. No global default promotion; see summary.json, validation.json and RENDER_QUALITY.md.'))
print(json.dumps(summary,indent=2))
