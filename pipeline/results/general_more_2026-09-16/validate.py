from pathlib import Path
import os,json,hashlib
os.environ['OPENCV_IO_ENABLE_OPENEXR']='1'
import cv2,numpy as np
r=Path(__file__).resolve().parent;cfg=json.loads((r/'config.json').read_text());rows=[]
for name,mc in cfg['maps'].items():
 for tag,v in cfg['variants'].items():
  d=r/name/tag;ep=Path(json.loads((d/'done.json').read_text())['episode']);q=json.loads((d/'runtime.json').read_text());st=q['native']
  assert st['finished'] and st['frame']==600 and st['write_failures']==0 and st['pending_writes']==0
  assert st['history_mode']==4 and st['temporal_aa_enabled'] and st['warmup_frames']==32
  assert (st['rgb_render_width'],st['rgb_render_height'])==(1280*v['ss'],720*v['ss'])
  assert all(abs(q['actual_cvars'][k]-x)<1e-4 for k,x in v['cvars'].items())
  states=[json.loads(x) for x in (ep/'engine_states.jsonl').read_text().splitlines()];assert len(states)==600
  assert [x['frame_id'] for x in states]==list(range(600))
  pe=max(max(abs(a-b) for a,b in zip(s['actual_location'],s['desired_location'])) for s in states)
  re=max(max(abs((a-b+180)%360-180) for a,b in zip(s['actual_rotation_pyr'],s['desired_rotation_pyr'])) for s in states)
  assert pe==0 and re==0,(name,tag,pe,re)
  for dr,ext in [('rgb','jpg'),('depth','exr')]:assert sorted(p.name for p in (ep/dr).glob('*.'+ext))==[f'{i:06d}.{ext}' for i in range(600)]
  video=cv2.VideoCapture(str(ep/'rgb.mp4'));n=int(video.get(cv2.CAP_PROP_FRAME_COUNT));fps=video.get(cv2.CAP_PROP_FPS);w=int(video.get(cv2.CAP_PROP_FRAME_WIDTH));h=int(video.get(cv2.CAP_PROP_FRAME_HEIGHT));video.release();assert (n,fps,w,h)==(600,24.,1280,720)
  summary=json.loads((ep/'capture_summary.json').read_text());light=summary['lighting'];control=json.loads((r/name/'lighting_control.json').read_text());assert light['fill']['factor']==control['factor'];assert light['exposure']['mode']=='auto_instant' and light['exposure']['bias_ev']==0.0
  m=json.loads((r/name/'metrics.json').read_text())['variants'][tag];assert len(m['depth_samples'])==8 and m['building']
  rows.append({'map':name,'variant':tag,'frames':n,'fps':fps,'duration_s':n/fps,'render_fps':st['fps'],'position_error_cm':pe,'rotation_error_deg':re,'write_failures':st['write_failures'],'depth_samples_checked':8,'lighting_factor':control['factor'],'lighting_quality_bar_missed':control['calibration']['quality_bar_missed'],'exposure_bias_ev':light['exposure']['bias_ev']})
base=r/'before/longvideo_template.json';current=Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline/tasks/longvideo_template.json');assert base.read_bytes()==current.read_bytes()
report={'recordings':rows,'total_frames':sum(x['frames'] for x in rows),'maps_tested':len(cfg['maps']),'previous_default_unchanged':True,'default_sha256':hashlib.sha256(current.read_bytes()).hexdigest(),'scope':'Native capture and replay integrity checks, not a claim of zero flicker, lossless visual detail, all-map validation, or accepted training episodes.'}
(r/'validation.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
