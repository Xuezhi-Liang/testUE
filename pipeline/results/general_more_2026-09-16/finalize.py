from pathlib import Path
import json, shutil

r = Path(__file__).resolve().parent
repo = Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline')
cfg = json.loads((r / 'config.json').read_text())
rows = []
for name, mc in cfg['maps'].items():
    metrics = json.loads((r / name / 'metrics.json').read_text())['variants']
    a, b = metrics['baseline'], metrics['taa_balanced']
    def delta(x, y):
        return (x / y - 1) * 100 if y else None
    rows.append({
        'map': name, 'label': mc['label'],
        'building_flash_pct_change': delta(b['building']['moving']['sparkle20'], a['building']['moving']['sparkle20']),
        'building_flash_baseline': a['building']['moving']['sparkle20'],
        'building_flash_candidate': b['building']['moving']['sparkle20'],
        'building_edge_pct_change': delta(b['building']['moving']['edge'], a['building']['moving']['edge']),
        'building_high_frequency_pct_change': delta(b['building']['moving']['lap'], a['building']['moving']['lap']),
        'full_walk_flash_pct_change': delta(b['moving']['sparkle20'], a['moving']['sparkle20']),
        'turn_residual_pct_change': delta(b['turn']['residual'], a['turn']['residual']),
    })
summary = {'maps': rows, 'decision': json.loads((r / 'decision.json').read_text()),
           'validation': json.loads((r / 'validation.json').read_text()),
           'browser': json.loads((r / 'browser_validation.json').read_text())}
assert len(summary['browser']['checks']) == 3
(r / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
doc = '''

## 2026-09-16: three additional maps, unchanged TAA candidate

Tested MiddleEast, WinterTown (RussianWinterTownDemo01), and ContainerYard
(Demonstration_Day) using the previously selected optional TAA 2x / 200% history
profile against the unchanged TSR 2x baseline. No profile tuning on these three
maps. Six clips, each 600 frames / 24 fps / 25 seconds; 3,600 RGB/depth/pose records.
First-person camera at 1.7 m, identical frozen routes within each pair; runtime
cvars, 32 rendered warmup frames and persistent history mode 4 verified.

| Additional map | Building flash proxy change | Building edge change | Full walk flash change | Turn residual change |
|---|---:|---:|---:|---:|
| MiddleEast | -20.6% | -1.4% | -16.8% | -2.3% |
| WinterTown | -6.3% | -2.1% | -9.2% | +36.6% |
| ContainerYard | both threshold medians 0 | -7.7% | -27.2% | +1.8% |

Building high-frequency variance decreases 12.5%, 13.2%, and 18.9%, respectively.
These mix texture and noise; neither edge strength nor high-frequency variance
proves perceptual detail preservation. Container local zero threshold medians do
not mean zero flicker. Motion-compensated residual also contains flow, occlusion
and changing-light effects; Winter's increase is a regression requiring visual
review, not a proven identification of ghosting. Local masks use baseline depth:
MiddleEast right curved facade and Container central corrugated walls in the first
8 seconds; Winter left apartment facade after the turn, frames 500..591 (~21–25s),
because the opening is mostly trees. Full-frame metrics retain all phases.

Lighting was calibrated once per map then reused exactly: factors 3, 1, 1.
Auto-instant exposure and zero bias remain identical. MiddleEast calibration missed
its full quality bar and the asset has known floating buildings; both limitations
are visible on the gallery. No map assets were repaired or simplified in this test.

All six recordings have zero write failures, complete frame IDs, zero position and
wrapped rotation error. Forty-eight sampled EXRs are finite at native 1280x720.
Browser checks pass all three video pairs, synchronized playback/seek, magnification,
mobile width, HTTP byte ranges and old gallery URLs. Existing global template is
byte-identical to its pre-test backup. Optional candidates remain optional: the
new maps do not support promoting a universal zero-flicker/detail-lossless setting.

Review: `/longvideo/general-stability-more/` on port 8500. Previous galleries,
including Dubai first person and top views, remain intact. Raw recordings and
scripts: `/home/ubuntu/ue_general_more_20260916/`. Compact audit:
`results/general_more_2026-09-16/`. Diagnostic captures are not accepted training
episodes or route/collision validation.
'''
for filename in ['RENDER_QUALITY.md', 'FINDINGS.md']:
    p = repo / filename
    text = p.read_text()
    if '## 2026-09-16: three additional maps, unchanged TAA candidate' not in text:
        with p.open('a') as f:
            f.write(doc if filename == 'RENDER_QUALITY.md' else '\n\n## 2026-09-16: three additional maps, unchanged TAA candidate\n\nMiddleEast, WinterTown and ContainerYard: six 25-second TSR/TAA paired clips completed.\nNew evidence retains tradeoffs: Winter turn residual +36.6%, Container wall edge -7.7%,\nwhile MiddleEast local flash -20.6%. Global default unchanged; no all-map guarantee.\nSee RENDER_QUALITY.md, results/general_more_2026-09-16/ and gallery\n/longvideo/general-stability-more/ on 8500. All old media remain intact.\n')
audit = repo / 'results/general_more_2026-09-16'
audit.mkdir(parents=True, exist_ok=True)
for pattern in ['*.py', '*.json']:
    for p in r.glob(pattern):
        shutil.copy2(p, audit / p.name)
for name in cfg['maps']:
    out = audit / name
    out.mkdir(exist_ok=True)
    for filename in ['metrics.json', 'lighting_control.json']:
        shutil.copy2(r / name / filename, out / filename)
    for tag in cfg['variants']:
        source = r / name / tag
        target = out / tag
        target.mkdir(exist_ok=True)
        for filename in ['runtime.json', 'done.json', 'frozen.json']:
            shutil.copy2(source / filename, target / filename)
        ep = Path(json.loads((source / 'done.json').read_text())['episode'])
        shutil.copy2(ep / 'capture_summary.json', target / 'capture_summary.json')
(audit / 'README.md').write_text(doc)
print(json.dumps(rows, ensure_ascii=False, indent=2))
