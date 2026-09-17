# Revisit capture pipeline

A first-person capture pipeline for a spatial-memory dataset: an agent walks a route, comes back to
a place it has already seen, and the episode records both visits with metric depth so that "has this
been seen before" is answerable from the data rather than from the label.

It runs against the `gym_citynav` UE 5.8 project (not `WorldModelCollect`), driven from Python over
UnrealCV, with the per-frame loop inside the engine tick. It borrows its depth-readback and navmesh
recipes from this repository - see `BORROWED.md` for what transfers and what does not.

## What one episode is

2844 frames, 118.5 s at 24 fps, 1280x720. Per frame: RGB JPEG, depth EXR (float16, linear metres in
R, `-1` for sky and beyond range), and the camera pose as a quaternion in a canonical camera frame.
The trajectory is FROZEN before capture: every pose is decided, validated and written to disk first,
so the capture is a replay and `desired == actual` by construction (measured: 0.000 mm position
error, 0.0000 deg yaw error).

The route is a nested out-and-back: stand and observe, walk out, come back to within 60 cm of the
anchor facing the same way, then do it again on a different bearing with a longer leg. That gives two
revisit events at two ages (35 s and 115 s) from one anchor.

## Moving-camera flicker fix (16 Sep)

The local long-video template now uses a persistent RGB capture with TemporalAA explicitly
enabled, TSR, 2x supersampling and 32 rendered warmup frames (`history_mode: 4`). The native
module must be rebuilt; setting the AA cvar alone leaves the old capture on FXAA. The selected
profile reduced the motion-sparkle proxy by about 61% on two aligned scene tests. Static noise
and depth comparisons have qualifications: see [RENDER_QUALITY.md](RENDER_QUALITY.md#8-16-sep-correction-the-capture-never-enabled-temporalaa).
Videos: [before/after comparison](http://51.20.82.218:8500/longvideo/flicker-fix/).
Old recordings and cloud archives remain unchanged.

## Running it

    bash run_batch5.sh batch5_downtown_west batch5_tokyo      # one task per map, serial

One editor at a time, deliberately: two on the same host deadlock on the shared `Saved/`,
`Intermediate/` and DDC. Each task restarts the editor, because a map switch in a live editor keeps
the previous level's streaming state and navmesh.

`tasks/*.json` defines a capture. `run_task.py` is the single-task entry point:
`freeze.py` plans and validates the route, `capture_engine.py` arms the in-engine capture and polls
it, `package.py` builds the data package and runs the acceptance gates.

## How a route is made safe

Three independent checks, because each catches something the others cannot:

1. **Navmesh planning** (`plan_navmesh.py`) - the anchor and both legs come from the exported
   navmesh, sampled every 30 cm, each point inside a walkable triangle and at least 150 cm from a
   boundary edge. Boundary edges are where walkable space ends - a wall indoors, a hedge or a drop
   outdoors - which is what keeps the camera out of foliage.
2. **Depth probe** - renders depth along each candidate leg every 40 cm and vetoes the route if
   anything is nearer than 60 cm. This is the only check that sees geometry with collision disabled,
   which foliage instances routinely are: such a tree cuts no hole in the navmesh and stops no
   capsule sweep, and a route will walk straight through its trunk with every collision check clean.
3. **Corridor sweep** - the body capsule swept along the centre line and both +-60 cm offsets, at
   planning time. The offsets matter because the trajectory builder does not walk the planner's
   nominal bearing: it resolves geometry against explicit anchor-relative points so the revisits
   close exactly, and the walked heading came out 1.4 deg off, which is 59 cm of lateral deviation
   at the far end of a 24 m leg.

A veto bans one heading and re-searches from the same anchor rather than abandoning it. That is the
difference between routing around an obstacle and refusing the map.

## Third-person footage

`walkpath.py` re-times an episode's route into a path a person would actually walk, and
`capture_walker.py` records a CitySampleCrowd citizen walking it, filmed over the shoulder. This is
for looking at, not for the dataset: the episode trajectory is not a walking trajectory (40% of its
frames have no translation at all, 708.7 deg is turned on the spot, and the mean speed while moving
is 72.7 cm/s against a walk animation authored at 140 cm/s). The five dataset episodes are untouched
by it.

## State

Delivered, all gates evaluable on this build passing:

| episode | map | gates |
|---|---|---|
| batch5_downtown_west | Downtown_West/Demo_Environment | 24 pass / 0 fail / 3 skip |
| batch5_tokyo | TokyoStylizedEnvironment/Tokyo | 24 / 0 / 3 |
| batch5_suburb_v3 | SuburbNeighborhoodHousePack/DemoMap_Day_Lumen | 24 / 0 / 3 |
| batch5_village | Village/Village | 24 / 0 / 3 |
| batch5_winter_town_v3 | WinterTown/RussianWinterTownDemo01 | 23 / 1 / 3 (`M_Fog` is a broken upstream asset) |

Kept as failure cases, not products:

| episode | why |
|---|---|
| batch5_neighborhood | `M_Dokyo_Foliage` fails to compile (Missing Material Function), so every tree draws with the Default Material - 27% of the median frame is pure black |
| batch6_middleeast | four buildings are authored 2.5-7 m above the ground; see `results/.../GEOMETRY_DEFECT.md`. All 23 gates pass - no gate looks at whether the scene's geometry is plausible |

Not done, and not to be presented as done:

- Section 12 `depth_visible_overlap` and `fully_occluded_interval`. Depth exists now, but the
  computation (reproject the anchor's depth into the revisit view and z-buffer test it) is not
  written. The ORB feature proxy in `package.py` is explicitly NOT it and is labelled so.
- `instance/` and `semantic/` masks (P1).
- A run animation for a crowd citizen. The retarget pipeline is built and produces an asset, but the
  chain mapping does not stick: the output has zero root motion and a near-reference pose. See
  `FINDINGS.md`.
- Multiprocess capture. Blocked on per-worker project copies; there is still no capture-phase GPU
  measurement to size it with.

`FINDINGS.md` is the more useful document if you are about to touch any of this - it is the list of
things that cost hours and that no amount of reading the engine source would have told you.
