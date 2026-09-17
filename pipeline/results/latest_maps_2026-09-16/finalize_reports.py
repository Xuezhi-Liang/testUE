from pathlib import Path
import json,shutil,hashlib
root=Path(__file__).resolve().parent;repo=Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline');out=repo/'results/latest_maps_2026-09-16'
reports={name:json.loads((root/name/'report.json').read_text()) for name in ['downtown','chemical']}
for name,r in reports.items():
 assert r['frames']==2844 and r['duration_s']==118.5 and r['runtime']['write_failures']==0
 assert r['runtime']['history_mode']==4 and r['runtime']['temporal_aa_enabled']
 assert r['runtime']['warmup_frames']==32 and r['runtime']['exposure_bias_ev']==0
 assert r['render']==json.loads((repo/'tasks/longvideo_template.json').read_text())['render']
 for f in ['report.json','engine_status.json']:
  shutil.copy2(root/name/f,out/(name+'_'+f))
for f in ['record_map.py','check_page.py','browser_validation.json']:
 shutil.copy2(root/f,out/f)
manifest={'web':'http://51.20.82.218:8500/longvideo/latest-config-20260916/','maps':{name:{'frames':r['frames'],'duration_s':r['duration_s'],'render':r['render'],'pose_tracking':r['pose_tracking'],'chosen_fill_factor':r['lighting']['calibration']['chosen_factor'],'calibration_bar_missed':r['lighting']['calibration']['quality_bar_missed'],'trace_summary':r['trace_summary']} for name,r in reports.items()},'capture_engine_sha256':hashlib.sha256((repo/'capture_engine.py').read_bytes()).hexdigest(),'template_sha256':hashlib.sha256((repo/'tasks/longvideo_template.json').read_bytes()).hexdigest()}
(root/'manifest.json').write_text(json.dumps(manifest,indent=2));shutil.copy2(root/'manifest.json',out/'manifest.json')
p=out/'README.md';p.write_text(p.read_text()+'''\nBoth recordings completed with 2,844 RGB/depth/pose frames (118.5 s each), zero write failures\nand zero tracked position/yaw error. Runtime confirms mode 4, TemporalAA, 32 warmup frames,\n2560x1440 RGB target and no manual-EV offset applied to auto exposure. Downtown calibration\nselected x3 and passed its bar. Chemical selected x1.5 but failed the near-black calibration\nbar; this is flagged on the page and retained in the report, not reported as a lighting pass.\nDowntown's 11 depth-ray differences above 15 cm all hit distant Landscape_0 at 177–233 m;\nthe remaining 21 samples agree. Possible render/collision terrain LOD differences are not\na proof of depth equivalence. Raw discrepancies for both scenes remain in the JSON reports.\nBrowser QA verifies playback/seek, 118.5 s duration, 1280x720 size, byte ranges and mobile width.\n''')
p=repo/'RENDER_QUALITY.md';p.write_text(p.read_text()+'''\nLatest-profile recordings completed on Downtown West and ChemicalPlant 2: 2,844 frames each,\n118.5 seconds, zero write failures and tracked pose error. Automatic fill selected x3 / x1.5;\nChemical still missed its near-black calibration threshold and is explicitly flagged. The\nmanual-EV bridge regression was checked against the native runtime (auto mode, bias offset 0).\nReports and scripts: `results/latest_maps_2026-09-16/`. New videos are published separately\nat `/longvideo/latest-config-20260916/`; prior comparisons and videos are preserved.\n''')
print(json.dumps(manifest,indent=2))
