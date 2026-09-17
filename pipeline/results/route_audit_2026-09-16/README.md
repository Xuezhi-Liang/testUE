# 86-map route audit, 2026-09-16

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
