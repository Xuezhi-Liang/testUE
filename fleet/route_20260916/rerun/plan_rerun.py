"""Categorise every non-passing map of the 20260916 route validation and write rerun_plan.json.

Rules (from the review of the first pass, all fleet maps ran at 6 cm ground clearance because
assignments.json was cut one minute before the manifest was raised to 45 cm on 15 maps):
  clearance45  - coverage_review, or collision_free failed, or probes clamped (camera in geometry):
                 rerun at 45 cm (Hwaseong 22%->99%, TemplePlaza 35%->99% measured earlier).
  lookup10     - only depth_probe_clear failed and every all-sky probe is a look_up frame:
                 rerun at 45 cm with pitch_limit_up_deg 10.
  indoor_sky   - all-sky probes at pitch 0 indoors: needs a look at the probe frame first.
  retry        - RpcTimeout after connect (editor wedge) or no attempt at all: rerun unchanged.
  drop         - action_mix_in_band on a tiny core, kept < 10 %, broken start points, missing asset.
"""
import json,sys
from pathlib import Path
R=Path('/home/ubuntu/ue_route_validation_20260916');OUT=Path(__file__).resolve().parent
manifest=json.loads((R/'manifest.json').read_text())
plan={}
for mc in manifest:
    slug=mc['slug'];rp=R/'maps'/slug/'result.json'
    r=json.loads(rp.read_text()) if rp.exists() else {}
    st=r.get('state');a=(r.get('attempts') or [{}])[-1];gates=a.get('failed_gates') or [];kept=a.get('kept_fraction')
    dp=a.get('depth_probe') or {};err=r.get('error') or ''
    sky_ex=dp.get('all_sky_examples') or [];sky=dp.get('all_sky_probes') or 0;clamped=dp.get('clamped_count') or 0
    reason=None;cat=None
    if st=='geometry_pass':continue
    if st=='blocked_asset':cat,reason='drop','map asset not mounted'
    elif slug.endswith('Traditional_Map'):cat,reason='drop','start points have no traceable ground'
    elif 'action_mix_in_band' in gates:cat,reason='drop',f'action mix unattainable: core too small (kept {kept})'
    elif kept is not None and kept<0.10:cat,reason='drop',f'kept fraction {kept:.2f} at 6 cm; not recoverable'
    elif st=='timeout' and 'RpcTimeout' in err:cat,reason='retry','editor wedge after connect, not a map fault'
    elif st=='route_failed' and not a.get('attempt') and 'start point' in json.dumps(r):cat,reason='retry','no start point projected on first try; nav export succeeded elsewhere'
    elif st=='coverage_review' or 'collision_free' in gates or clamped>0:cat,reason='clearance45',f'kept {kept}, collisions {(a.get("collision") or {}).get("collision_count")}, clamped {clamped}'
    elif gates==['depth_probe_clear'] and sky_ex and all(e.get('phase')=='look_up' for e in sky_ex):cat,reason='lookup10',f'{sky} all-sky probes, all in look_up'
    elif gates==['depth_probe_clear'] and sky and any(e.get('phase')!='look_up' for e in sky_ex):cat,reason='indoor_sky',f'{sky} all-sky probes at pitch 0'
    elif gates==['depth_probe_clear']:cat,reason='lookup10',f'thin {dp.get("thin_depth_probes")} / sky {sky}: sky-heavy frames'
    elif st=='timeout':cat,reason='clearance45','7200 s worker limit; rerun with 14400 s'
    else:cat,reason='review',f'{st} {gates} {err[:60]}'
    plan[slug]=dict(category=cat,reason=reason,prev_state=st,prev_gates=gates,prev_kept=kept,prev_clearance=a.get('ground_clearance_cm'))
(OUT/'rerun_plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=1))
from collections import Counter
print(Counter(p['category'] for p in plan.values()))
for cat in ['clearance45','lookup10','indoor_sky','retry','drop','review']:
    print('\n##',cat)
    for s,p in plan.items():
        if p['category']==cat:print(f"  {s[5:52]:48} {p['prev_state']:16} clr={p['prev_clearance']} {p['reason']}")
