# Findings

Everything here was measured on this build (UE 5.8, headless Vulkan editor in
`editor_play_simulate`, UnrealCV on 9208). Each entry says what the symptom looks like, because in
almost every case the symptom pointed somewhere else first.

## The editor wedges, and there are exactly two common causes

Both look identical from outside: 180-600% CPU, log silent, UnrealCV requests unanswered, `kill -9`
the only way out. Both were **already documented in `local_run/final_run.py` of the other
repository** and were rediscovered the hard way anyway.

1. **Spawning any humanoid without forcing synchronous skinned asset compilation.** UnrealCV's init
   leaves `Editor.AsyncSkinnedAssetCompilation` at 2; the first spawn then blocks forever on
   `Waiting for skinned assets to be ready 0/1 (...)`. Set both of these before anything spawns a
   character:

       vrun Editor.AsyncSkinnedAssetCompilation 0
       vrun Editor.AsyncAssetCompilation 0

2. **Opening a second UnrealCV connection instead of reusing the client.** It spawns a server thread
   per connection. A radius search that reconnected per candidate wedged the editor for 19 minutes.

**When the editor wedges, grep the repo for the last log line before the silence before theorising.**
Three separate wrong causal stories were told about the first hang - "shader compilation, let it
finish", then "nothing is compiling, waiting will not help", then "async is disabled so the wait
never completes" (the truth is the opposite: async being *enabled* is what hangs it). The evidence
for the second story was that the project DDC did not grow; that evidence was worthless, because the
work was skeletal mesh building and LOD reduction, which logs under `LogSkeletalMesh` /
`LogSkinnedAsset` and does not land in the cache that was being sampled.

## Depth

- `vget /camera/N/depth npy` returns a constant `65504.0` (fp16 max, an uninitialised buffer), and
  every render-target readback from editor Python asserts at `VulkanRenderTarget.cpp:171` and takes
  the editor down. `FRHIGPUTextureReadback` from C++ is the only float-safe path on Vulkan. That is
  what `SimWorldCapture::CaptureDepthEXR` does.
- `FRHIGPUTextureReadback::EnqueueCopy` with a defaulted `FResolveRect` copies **nothing**. Pass the
  explicit rect.
- `Lock()` returns a row pitch in **pixels**, not bytes.
- OpenCV decodes EXR as **BGRA**, so an EXR written with depth in R reads back at channel **2**.
  Channel 0 is all zeros, `z > 0` selects nothing, and every check silently passes. This produced a
  QA report of "0% near pixels, 0 buried frames" for every episode, which was not a measurement of
  anything. The build also ships the OpenEXR codec disabled - set
  `OPENCV_IO_ENABLE_OPENEXR=1` before importing cv2 or reads raise.
- `bit_depth` in the metadata must be read back from what was written. Claiming float16 while
  `ERawImageFormat` was float32 was wrong for one whole run.

## Characters

- **`BP_CrowdCharacter` spawns with an actor scale of 2.0** in this project: unscaled capsule half
  height 88 cm, scaled 176 cm, so the citizen stands about 3.5 m tall. Symptoms: the head clips
  through the top of frame at any camera distance, AND the feet slide, because at twice size a walk
  cycle covers twice the ground while the animation is positioned from the unscaled 140 cm/s. Three
  rounds of camera-distance tuning were spent on the first symptom before the cause was measured.
- **A Character's actor origin is the centre of its collision capsule, not the soles of its feet.**
  Placing the actor at the foot position buries it by one capsule half height - the walker was sunk
  to mid-thigh with road drawn across its legs, which reads as a framing problem and is not one.
- **A Character will not spawn where the default collision handling refuses it.** `BP_CrowdCharacter`
  at the world origin of a city map returned null. Use
  `SpawnCollisionHandlingOverride = AlwaysSpawn` and `bNoFail` for a walker that is driven
  kinematically along an already-verified route.
- **A CitySampleCrowd citizen cannot be assembled from raw meshes.** It is five layers across three
  skeletons. Three attempts, each verified by looking at the render:
  - base + garments: a suit with an empty collar and empty sleeves. `m_tal_nrw_base` carries material
    `M_Hide` - it is the body that hides UNDER clothes and renders no skin at all.
  - plus the skin as a leader-pose follower: a stretched black spike above the shoulder. SK_Base has
    232 bones against the skin's 167, so only matching names are driven.
  - plus the MetaHuman face snapped to the `head` bone: an oversized head at hip height. A MetaHuman
    face's origin is at the character's feet.

  Spawn `BP_CrowdCharacter` and let it assemble itself, then take over the animation of whichever
  component shares the walk animation's skeleton.
- The crowd has **no run animation** - City Sample citizens only ever walk. `MTN_N_WalkQuickly_F` is
  a fast walk at 243.5 cm/s.
- Retargeting a mannequin run onto SK_Base is **built but not working**: the IK Rig, its
  auto-characterised chains and the retargeter all come out correct, `RunBatchRetarget` produces an
  AnimSequence, but `auto_map_chains` and explicit `set_source_chain` both report success and read
  back as `None`, and the output has zero root motion and a near-reference pose. `DuplicateAndRetarget`
  is deprecated; `AssetsToRetarget` is a `TArray<FAssetData>`, not an array of animation objects.

## Animation driven by distance

The walk animation is positioned by distance travelled, not by time:

    anim_time = fmod(travelled_cm / anim_speed_cm_s, anim_length_s)

That is what stops the feet sliding, at any speed. Two consequences that must be stated rather than
hidden:

- A pivot in place has zero translation, so the animation freezes and the character becomes a statue
  rotating on the spot. Charging the turn an arc length puts steps back into it.
- The play rate is `path_speed / anim_speed`. Far from 1 it reads as slow motion (Manny's
  `MM_Walk_Fwd` at 240 cm/s driven along a 140 cm/s path plays at 0.58x). A single clip cannot cover
  a speed range; that needs a blend space.

## Lighting: black frames have two independent causes

Both look the same on screen - large regions at exactly RGB 0 - and they need opposite responses.

1. **A material failed to compile** and fell back to the Default Material, which this build draws as
   absolute black. `ModularNeighborhood`'s `M_Dokyo_Foliage` fails on a Missing Material Function, so
   every tree is a black silhouette over 27% of the median frame. The editor log names the assets:
   grep for `Failed to compile Material`. Unfixable without the missing dependency - drop the map.
2. **A Static or Stationary skylight with no built lighting data.** Its indirect contribution is
   baked, so with nothing baked it contributes zero: direct sunlight renders correctly and everything
   in shadow comes out at 0. `MiddleEast`'s `SkyLight_0` is Stationary with `real_time_capture` off -
   15.4% of the median frame was pure black while every material compiled fine. Switching the
   skylight to Movable with real-time capture took it to 2.4%, against a 1.6-1.7% baseline on maps
   that were already fine. `capture_engine.ensure_dynamic_sky` does this per session and records it in
   `capture_summary.json`, because "the map as shipped" and "the map with a dynamic sky" are different
   provenance.

The gates separate the two: `no_dead_black_regions` reports the symptom,
`materials_compiled` names the cause when it is the first one.

## What the gates cannot see

`batch6_middleeast` passes all 23 evaluable gates and has **four buildings authored 2.5-7 m above the
ground**. The gates check collision, penetration, dead-black regions, material compilation and pose
closure; none of them asks whether the scene's own geometry is plausible. A person looking at the
footage found it twice, after two wrong conclusions from biased sampling on this side: an overhead
survey taken from the anchor only (where the buildings happen to be grounded), and a per-building
ground trace that stopped after ten actors (the floating ones are further down the list).

The human check in section 14 step 15 is not a formality.

## Smaller things that cost real time

- **`unreal.Rotator`'s positional order is `(roll, pitch, yaw)`**, not the `(pitch, yaw, roll)` that
  `FRotator` prints. Passing a yaw positionally sets pitch: it aimed a depth capture at the floor and
  produced a frame of entirely plausible readings that all equalled the camera's eye height. Nothing
  errored. Always use keywords.
- `EditorAssetLibrary.load_asset` returns `None` for assets plainly present on disk in this session;
  module-level `unreal.load_asset` works.
- `AreaIndices[0]` is `RECAST_NULL_AREA`. Walkable navmesh triangles were in slot 63.
- `ANavMeshBoundsVolume` synthesised from code has no brush, so its bounds stay degenerate without
  `UModel` / `UPolys` / `UCubeBuilder` - none of which are exposed to Python.
- `EnsureNavMesh` must check that an existing bounds volume actually **covers** the region. A stale
  origin-centred volume while the spawn was 18 km away produced 537 triangles of empty ground and an
  anchor in open sky.
- The navmesh build only progresses when the engine ticks. Ticking the navigation system from inside
  the engine tick re-enters it: that hung one map for 42 minutes at 584% CPU with a silent log.
  Dispatch the build and poll from outside.
- `pgrep -f <pattern>` matches your own shell when the pattern appears in your command line. Killing
  by such a pattern kills the caller; use the `[b]racket` trick or kill by PID.
- Editing a shell script while it is running corrupts execution - bash reads scripts incrementally by
  byte offset.
- `mv a* b` with several matches and a non-existent destination fails, and if you do not check the
  exit status you will believe a backup happened that did not. That mixed 159 frames of an old take
  into a new one and destroyed the original.
- Reading the engine headers before calling an API is cheaper than iterating: 13 C++ signatures
  checked that way compiled first try, while four rounds of guessing Python method names on the
  retargeting API cost four editor round trips and one 15-minute hang.

## A depth channel with no depth in it ends the capture

`ASimWorldCaptureActor` refuses to write a frame whose every depth pixel is invalid
(`SimWorldCaptureActor.cpp:747`, `Invalid == N`) and calls `Finish`, so the run stops there. The
guard is right - for a dataset frame an empty depth channel cannot be told apart from a capture
that produced nothing - but it means the *route* must never contain such a frame.

Looking up walks straight into it. Measured on a Tokyo street at eye height, the fraction of the
frame carrying valid depth against camera pitch:

| pitch | valid | nearest |
|---|---|---|
| 0.0 deg | 35.5% | 3.20 m |
| 11.5 deg | 16.4% | 5.12 m |
| 20.3 deg | 3.6% | 10.04 m |
| 27.2 deg | 0.005% (46 px of 921600) | 42.67 m |
| 27.5 deg | 0% | - |

At eye level 63% of that frame is already sky. Pitching up pushes the near ground out of frame and
what remains is distant geometry along the bottom edge, until that leaves too. **This stopped a
capture at frame 97519 of 146845 - 64 minutes of recording and 22 GB.** 43 frames already written
were under 1% valid, the thinnest at 46 pixels: the failure had been arriving for a while.

Two wrong turns on the way to the fix, both worth recording:

- **"The capture is safe, only the probe refuses."** Read from `sed -n '715,745p'`, which ends two
  lines before the guard at 747. A conclusion drawn from a window that stopped short, and stated as
  a conclusion.
- **"Half the vertical FOV is the limit."** At 90 deg horizontal the vertical FOV is 58.7 deg, so
  pitch below 29.4 deg keeps the horizon in frame. The frame that failed was at 27.5 deg. Horizon
  *direction* in frame is not geometry in frame: the ground at 1.8 deg below horizontal is 53 m
  away and occupies 22 of 720 rows. And at the spawn point on the same map, pitch 27.5 deg reads
  99.4% valid, because a wall fills the view.

There is no universal pitch limit - it depends entirely on how open the spot is. So: cap looking up
well short of it (15 deg, against 30 deg for looking down, where the ground is always in view), and
have the depth probe check the frames that pitch furthest up rather than a uniform stride. A stride
of 587 hit one such frame by luck and missed the one that mattered.

## `cv2.fillPoly` takes (x, y); a grid is indexed [row, col]

Rasterising a navmesh region with `fillPoly(grid, [p[:, ::-1] for p in tris])` and reading it back
with `to_world((r, c)) = (lo_x + c*cell, lo_y + r*cell)` transposes the map. Nothing errors, and the
centrelines that come out still *look* like a road network, so a top-down preview passes inspection.

What it actually produced: 8 of 12 sampled points on the longest road were inside no navmesh
triangle at all, the height lookup fell back to a downward trace that hit container roofs and a 13 m
gantry, and the body capsule ended up inside `BP_Container8`. It surfaced as "174 of 221 roads are
blocked at the agent's head height" - a plausible-sounding fact about the map. After the fix: 1 road.

On a non-square map it also silently clips, because x-scaled values get written into rows sized from
the y extent. The map that hid this was 55 x 62 m, near enough to square that the road count changed
only from 221 to 216.

The check that now guards it costs nothing: sample the centrelines and require that they lie inside
the region they were derived from. An axis error is never subtle when you ask that question.

## A connection test is not a health check on this UnrealCV

Probing readiness with three `/dev/tcp` connect-and-close cycles reproduced the wedge documented at
the top of this file exactly - `Socket: NULL`, `SE_ECONNRESET`, `Client disconnected.`, process gone.
UnrealCV spawns a server thread per connection and this build dies on the next request after a
client disconnects, so opening a socket to see whether it is alive is the documented way to kill it.

Read readiness out of the log instead: the start-up hook's own success line, plus a listening port
and a live process. A listening port alone is not readiness either - it is bound early in startup and
stays bound for a moment after the editor decides to quit, which reported "listening after 30s" for
an editor that was already shutting down.

## `--ExecutePythonScript` failing quits the editor, and two mounts are not optional

`EditorPythonExecuter` treats a script it cannot open as a fatal error and issues `QUIT_EDITOR`. From
outside this looks like a successful start followed by "connection refused", because the UnrealCV
port really was bound for a second.

The container's own `/home/ue4/tools` is an empty directory with mode 000 and its
`simworld/Content` is empty: both are bind mounts from the host in the original launch
(`launch_unreal_instance/start_enroot_container.sh`), and without them there is no start hook and no
licensed asset packs. `launch_ue.sh` assumes a long-lived container to `enroot exec` into, which does
not exist after a host reboot.

## The biggest walkable region on a purchased map is usually the empty apron

Tokyo's coverage episode walks the map's outskirts. It is 145950 frames, 101 minutes and 22 GB, it
passed all seven pre-gates and every acceptance gate that can run on this build, and the reason is
one line in its own freeze log:

    [freeze-cov] road network: 1742 roads, 1144 m of centreline over 8174 m2

8174 m2 is navmesh region 0: the flat ground plane the asset pack leaves around its content. The
street network with every building in it is region 1, 2151 m2. `survey_coverage.py` picks the
region with the longest covering tour, the apron is four times the area, so the apron won and the
route inspection solved it beautifully.

Nothing about this is visible in the metadata. The tour is real, the roads are real, the frames are
collision-free and the depth channel is full. Only a picture of the navmesh shows it.

`survey_core.py` measures the **interior** walkable surface instead, and ranks maps by it. Two
tests, and a cell has to pass both:

1. **Local obstacle density** - at least 6% of a 12 m disc around the cell is obstacle, where
   obstacle is the space the walkable surface does not cover inside the map footprint: buildings,
   walls, props.
2. **Content on opposite sides** - of 8 pairs of opposing directions, at least one pair has an
   obstacle within 25 m on *both* sides.

Density alone measures "near content", which is not "inside content", and the difference is
precisely the map's outskirts. A strip along a boundary wall, or a path round the rim of an empty
field, has content on one side and nothing on the other. Adding the pair test removed 80% of
ModularNeighborhood's core, 93% of Grass_Hills', 78% of MMSupermarket's and 87% of
UltimateFarming's, and left 97% of Tokyo's street network and 80% of Downtown_West's - that 20%
being the fringe where its blocks open onto the apron. It reorders the ranking too, because what it
takes varies from 3% to 93% of a map: Pyramids drops from 6304 m2 to 3163 and ContainerYard from
3861 to 1677, while AncientRuins keeps 94% and moves up past both.

Counting covered directions without pairing them does not work. A cell two metres from a long
boundary wall has a wide fan of directions landing on that wall and scores as high as a street:
at 8 of 16 directions ModularNeighborhood's edge band went from 503 m2 to 502.

Four wrong turns, each of which produced a plausible number:

- **Obstacles as the holes `binary_fill_holes` finds in one region.** A building whose footprint
  touches the region's own edge is not a hole, so the eastern half of Tokyo's street network scored
  zero obstacle density and was not core.
- **Closing the walkable surface into a footprint in place.** `cv2.morphologyEx(MORPH_CLOSE)`
  treats out-of-image as filled during the erosion half, so the dilation's full 15 m band survives
  along every edge of the raster: a phantom wall around the export box, which reads as content.
  388 of ModularNeighborhood's 435 m2 of obstacle was this frame, and the 503 m2 core it produced
  was a strip pinned between one real wall and the phantom one. Pad by the radius before closing
  and crop after.
- **A ground-level height band anchored on the lowest region.** A 53 m2 dip 15 m below the terrain
  put the terrain itself outside the band: Grass_Hills, ForestGasStation, ChemicalPlant and
  WinterTown each came back with two orders of magnitude less walkable surface than they have, and
  a core of zero.
- **Unioning regions within that band at all.** Downtown_West has a coarse 10784 m2 plane at
  z = -270 (467 triangles) that runs under the whole level, buildings included. Union it with the
  8401 m2 street level 2.7 m above and every building footprint is filled in: obstacles vanish and
  a city-block map reports a 200 m2 core. Measure each region separately and take the region with
  the largest core - a region is one continuous surface an agent can walk without leaving it, which
  is exactly the thing whose core is wanted, and stacked surfaces cannot dilute each other if they
  are never mixed. The height band is then unnecessary, and was dropped.

Two limits of the numbers this produces, both worth stating before anyone ranks maps by them:

- **The nav box bounds the answer.** `nav_ensure` builds 120 m of half extent around one start
  point. On the 25 cached exports 9 cores run to within 1.5 m of that box, so those areas are
  lower bounds rather than measurements - ranking on them partly ranks how built-up one 120 m
  window happens to be. ModularNeighborhood and ForestGasStation are the clear cases: their start
  points span 308 x 195 m and 96 x 137 m, and the window that was exported holds almost no content
  at all. Comparing maps needs a box that contains the map, centred on the content rather than on
  whichever start point sorted first.
- **A core split across regions is undercounted**, because only the best region is reported. Stairs
  and kerbs are what split them.

Do not compute a covering tour on a core in the survey. A core is a blob, not a corridor network;
ChemicalPlant's core skeleton is 9336 nodes with 5864 of odd degree, and route inspection's
all-pairs shortest paths and maximum weight matching do not finish on it. The tour belongs to a
route planned inside one chosen core.

## The gates judged a file the upload excludes

`no_blank_frames`, `no_duplicate_frames`, `no_dead_black_regions` and `video_decodes_fully` all
decoded `rgb.mp4` from end to end. The uploader excludes `rgb.mp4` (`--exclude rgb.mp4`): the
delivered dataset is `rgb/%06d.jpg` and `depth/%06d.exr`. So four gates were measuring an H.264
re-encode of the data instead of the data, and `contact.png` - the human check that caught a
camera standing on a rooftop - was seeking frames in that same re-encode.

Moved to the JPEGs, sampled, with the sampling in the detail line. On the IndustrialArea episode
the two verdicts agree closely enough to show the change measures the same thing:

| | via rgb.mp4 | via rgb/*.jpg, stride 12 |
|---|---|---|
| dead black, median | 19.3% | 19.3% |
| frames over 10% | 845 of 1200 | 843 of 1200 |
| duplicate pairs | 557 of 14399 (3.9%) | 40 of 1200 (3.3%) |

Duplicates need CONSECUTIVE frames, so the sample is of adjacent **pairs** - frame *i* and *i*+1 -
not of single frames every half second. A stride of single frames would find no duplicates on any
episode, and that would read as a pass.

**`hold` is not what makes duplicate frames.** Tokyo's stroll spent 8100 frames (5.55%) holding
and had **zero** duplicates; IndustrialArea held for 181 frames (1.26%) and had **557** - more
duplicates than hold frames. Whatever produces them there, it is not the action mix, and the
neighbouring gate points at what it probably is: 19.3% of the median frame is pure black, worst
frame 100%, which is what geometry drawn with the Default Material looks like in this build.
Unconfirmed, because `materials_compiled` was skipped for want of `UE_LOG` - which is now set by
`longvideo/driver.sh` so the next occurrence names the assets.

## A 20 h episode breaks three steps that a 10 minute one does not

Per-frame saving scales fine - it is JPEG and EXR per frame, no length limit. What did not:

- **`assemble_mp4` has `timeout=3600`.** 1.73 M frames is a ~110 GB x264 `preset medium` encode,
  hours of CPU. Above 120000 frames the review video is now a timelapse of every *n*th frame,
  named `rgb_proxy.mp4` rather than `rgb.mp4`, because a sampled video called `rgb.mp4` is a file
  that silently disagrees with `frames.csv` about what frame 100 is. Built through ffmpeg's
  concat demuxer, not `-vf select`, which would still decode every input frame.
- **`qa_video.py` composites a panel per frame** - PIL drawing, a depth EXR read, a top-down
  redraw. 1.73 M panels outlasts the capture. It now strides to a target length and prints the
  true frame id in the header, so a reviewer is never looking at frame 40000 believing it is 12.
- **Whole-file reads.** `engine_states.jsonl` is 745 MB at 20 h and was read with `read_text()`,
  then turned into 1.73 M row dicts before any of it was written. Streamed into `frames.csv` now,
  carrying forward only the aggregates `capture_summary` reports.

And one thing that is a policy, not a bug: a single 20 h episode has no checkpoint. The capture
actor starts at frame 0, so an editor lost at hour 15 cannot resume, and
`if st["frame"] != total: raise` threw away the 15 h that were on disk. Under
`allow_short_episode` the episode becomes what was recorded - with `planned_frames` and
`ended_early_reason` beside it - and `min_frames` stops that turning a two-minute failure into a
"successful" two-minute episode. `longvideo/runner.py` also recomputes the cap from the wall
clock on every attempt, because retrying a 20 h episode at 20 h needs 20 hours the instance does
not have.

## Eight random passes over a core cost more than the clock allows

`coverage.py:coverage_walk` is already a randomised walk - it takes a random unwalked road at each
junction rather than solving route inspection - and `prepare_walks` already chains eight of them,
each starting where the last ended. A `random_walk` family would have been a second copy of it.

What the pass count costs, measured offline from the cached navmesh exports with knobs at 1.0
(`longvideo_dryrun.py`, no editor):

| map | core | one pass | 8 passes |
|---|---|---|---|
| ChemicalPlant_2 | 5762 m2 | 208 min | 58.2 h |
| WinterTown | 3778 m2 | 93 min | 26.5 h |
| AncientRuins | 3287 m2 | 117 min | 31.8 h |

So against a 20 h ceiling the ceiling binds, and "eight times over the map" is not what these
episodes will be - about six is. Worth knowing before the GPU time, not after, which is why the
sizing prints both numbers and says which one bound the episode. `plan()` records it in
`length.bound_by`.

The mix tuner cannot run at that length. Its coordinate descent evaluates up to 72 knob settings
per iteration and every evaluation scripts every pose in the budget: at 1.73 M frames that is tens
of millions of poses per iteration. The mix is a per-frame statistic, so it is tuned on a one-pass
proxy - and the mix that gets **reported** is measured on the full-length episode, never on the
proxy.

## Trimming the episode after tuning the mix rejects the episode

"N passes or the clock, whichever comes first" needs the episode to stop when the Nth pass
finishes. It does not stop there on its own: the budget is N passes times `size_headroom`, so on
a map where N passes fit, a 25% headroom bought a 9th and a 10th pass in the fill styles and the
episode was neither N passes nor the cap. Tokyo at 8 passes came out 14.4 h instead of 11.5 h.

Cutting it afterwards is worse than not cutting it. Measured on Tokyo at 2 passes:

    [coverage] full-length mix: penalty 0.00000            <- 287588 frames, every band satisfied
    [coverage] pass 2 completes at frame 201877; trimming there (201878 frames, 2.34 h)
      mix in band=False   forward 0.259                    <- against a floor of 0.26

The tuner optimised 287588 frames and 201878 were delivered, and the discarded tail was worth
0.001 of forward. `accepted` is `len(failed) == 0` and `action_mix_in_band` is one of the gates,
so that rounding error rejects the whole recording - 20 h of it, on a real run.

So the trim happens inside the tuner's scoring, not after it: `evaluate()` trims before it
measures, and what gets reported is what gets delivered. A one-pass proxy still cannot see the
blend of one styled pass with N-1 fill passes, so after the full-length generate there is one
round of single-knob moves at full length, and only when the penalty is non-zero. On Tokyo it
took one move (`back` x1.15, 8 seconds) to go from 0.001 out to in band.

## A runner that skips the refusals records 20 h of a route the pipeline would have refused

`run_coverage.py` refuses on three things before capturing: a route that is not collision free, a
depth probe that found geometry closer than the threshold (the only check that sees foliage with
collision disabled, so a clean capsule sweep is not proof), and a route that reaches fewer than
all the roads. `longvideo/runner.py` was written from `campaign/runner.py`, which checks the first
and does not have the other two - the campaign's family has no coverage fraction and its 60 s
episodes were cheap to redo.

At 60 s a missing refusal costs a minute. At 20 h it costs the machine's whole run, and the
episode that comes out looks complete: the frames are there, the timestamps are even, and the
gate that would have caught it was never asked. All three refusals now run in the long-video
runner, recorded as the specific outcomes they are rather than as failures.

## The spawn retry `freeze.py` carries was never copied into `freeze_coverage.py`

MedievalCastle refused all twelve of its start points:

    RuntimeError: no start point projected onto a navmesh: test1 does not project onto the
    navmesh - skipped; test2 ... test3 ... test4 does not project onto the navmesh - skipped

The map is fine. `test1` sits inside the navmesh bounds cached from an earlier batch7 run
(-20083..-8170 x, 9747..22173 y) - that export was built around test1 and it projected then. What
differs is the code path: `freeze.py` waits the nav tile build out (8 s, five times) before giving
up, because "idle + has_navmesh" from `nav_ensure` can mean the OLD navmesh with the newly
synthesised volume's tiles not started yet. `freeze_coverage.pick_spawn` was written without that
wait, and Tokyo and IndustrialArea - the only two maps it had ever run on - happened not to need
it.

Two things this also showed:

- `pick_spawn` calls `nav_ensure` **per candidate**, and this map's start points are scattered
  over 20 km, so each candidate synthesises its own bounds volume somewhere else. The retry now
  runs per candidate, after that candidate's own ensure.
- The old message said "does not project onto the navmesh" and nothing else. It now carries
  `settled`, `has_navmesh` and the build wait from `nav_ensure`, so the next occurrence says
  whether the navmesh was there at all or the projection simply lost a race.

## The start hook's success line is `Called editor_play_simulate()`

Waiting for a level-load message instead cost fifteen minutes on an editor that was ready the
whole time. There is no "LoadMap" line for a map given on the command line, and the world comes up
as `UEDPIE_0_<map>` in a line reading `Bringing World` - a grep for `Bringing world` matches
nothing, and a lower-case w is not a diagnosis.

What the editor looks like when it IS ready, and why it reads as a wedge: 597% CPU across 82
threads, 64% GPU, and a log that has not moved for fifteen minutes. That is `editor_play_simulate`
idling at full tilt with nothing to say. The two documented wedges look identical from outside -
so before calling it one, check the two things that separate them: no `ShaderCompileWorker`
processes and a DerivedDataCache that does not grow means it is not compiling, and both documented
wedge causes require a request to have been sent first. None had been.

## `launch_ue.sh` counts the previous client's TIME-WAIT as the port being in use

Its check is `ss -tan | grep -q ":$PORT "`, which matches the port in either column. After a run
ends, the client side leaves `127.0.0.1:<ephemeral> -> 127.0.0.1:9208` in TIME-WAIT, the grep
matches the remote port, and the relaunch waits for a socket that belongs to the process that just
exited. It costs up to a minute per relaunch and it is not a hang - the wait budget is 240 s and
TIME-WAIT clears well inside it - but a relaunch that sits at "waiting for port 9208 to be free"
with no editor process alive is this, not a stuck editor.

## A start point can sit metres above the surface it belongs to, and 800 cm of z extent hides it

MedievalCastle refused all twelve start points with `nav_ensure` reporting `settled=True
has_navmesh=True`. Probed directly, one candidate at a time, with the z extent as the variable:

    test1 at (-13878, 15959, 11112)
      downward trace from 11512: 10257 cm
      z extent  800 cm -> nothing
      z extent 2000 cm -> PROJECTED to z 8901 cm  (13.6 m below the probe point)

    test2 at (-23678, 1532, 13710)     trace 13600 cm
      z extent 800 / 2000 / 5000 / 20000 cm -> nothing at any extent

So test1 is 13.6 m above its own courtyard and `nav_project`'s 800 cm z extent - the value
`freeze.py` uses and `freeze_coverage.py` copied - cannot reach it. test2 through test4 are 10-15
km away in parts of the level with no navmesh at all, which is not a problem: one candidate is
enough, and it is the one the core region lives under.

Two things worth keeping:

- **`has_navmesh` is global, not local.** It says some navmesh exists somewhere, so `settled=True
  has_navmesh=True` next to "nothing projects" is not a contradiction and not a race. The message
  now says so, because reading it as a race cost a rerun.
- **A downward trace hitting geometry proves nothing about walkability.** test2's trace hits at
  13600 cm and no navmesh exists there. The repo already says this about capsule sweeps - "the
  Visibility channel is not what blocks a Pawn" - and it applies to the ground trace too.

The projection now tries 800, then 2000, then 5000 cm, and prints which extent worked and how far
the point snapped. Conservative first, so a point already on its surface still snaps to the
nearest one, and a spawn that fell through a floor is visible instead of silent.

**Nine of the twelve long-video maps have never been through this function.** Only Tokyo and
IndustrialArea had, and neither needed the taller extent. On a fleet run this failure costs a
machine-day per map that has it, silently - the instance boots, refuses, and stops. A pre-flight
that launches the editor once per map and probes the spawn is ~3 minutes a map against 20 hours of
instance time, which is not a close call.

## The navmesh is not the collision surface, and moving the capsule onto the traced one is worse

Head-clearance pruning on WinterTown's core blamed `Landscape_0` - the terrain - for its longest
blocked roads, at 0.0 to 31.3 cm. 272 of 2028 roads directly, 1286 more left unreachable: 80% of
the core network gone for a reason that has nothing to do with head clearance. ChemicalPlant, the
one flat map of the four measured, had zero collisions. That contrast is what pointed here.

Measured, over every road sample point, traced ground minus the navmesh height the capsule is
placed on: p10 **-37.7 cm**, median **-13.1**, p90 **+5.2**. So the navmesh sits about 13 cm above
the surface a trace finds, and Recast's own cell height is the reason.

Placing the capsule and the poses on the traced surface instead **made it worse**: blocked roads
272 -> 557, kept centreline 243 m -> 84 m. Two reasons, and both say the navmesh height is the
better choice:

- **The sweep is a straight segment between samples 40 cm apart, and the ground bulges between
  them.** Hugging the ground makes the segment cut those bulges more often, not less. The two
  measurements are monotone in the wrong direction for the traced surface.
- **The trace runs on `TRACE_TYPE_QUERY1`, which is not what blocks a Pawn** - the repo says this
  about capsule sweeps and it is just as true of the ground trace. It passes through wooden decks
  and porches and returns the terrain underneath, so a camera 1.7 m above *that* is below the deck
  the agent is standing on.

So the traced surface is now measured and recorded (`collision_surface` in the frozen file) and
**not used to place anything**. What it is good for is saying how far a map's navmesh is from its
traced ground, which is what separates a flat map from a rolling one.

The lever that does work is `GROUND_CLEARANCE_CM`, now an environment override rather than a
constant, because raising it buys roads back and costs the sweep its ability to see obstacles
below that height. That is a trade-off to measure per map, not a default to change quietly.

Four hypotheses were refuted before this one, each by the measurement that killed it, and each
worth keeping so the next person does not spend the time again:

1. **A nav tile-build race.** True for the spawn projection and nothing else: `nav_ensure` reported
   `settled=True has_navmesh=True` while nothing projected, because `has_navmesh` is global.
2. **Orphaned `zenserver` processes competing for the DDC.** They were `Z` zombies parented to the
   container's `sleep`, which never reaps. Zombies hold nothing.
3. **The editor crashing mid-work.** The callstack is `UEditorEngine::EndPlayMap` into Slate
   widget destructors - a teardown segfault caused by killing an editor that is in
   `editor_play_simulate`. The documented commandlet behaviour, on an editor.
4. **The camera buried in the terrain.** Sampled on the frozen route: the plan's surface is a
   median 14 cm *above* the traced ground, so the capsule floats. That probe also sampled the
   wrong population - the route only uses roads that survived pruning, and the blocked ones are
   the question.

## 6 cm of ground clearance is a flat-map default; on terrain the sweep needs 40

The lever, measured on WinterTown's core over one editor session and one road network:

| clearance | blocked | unreachable | kept roads | kept m | kept | worst blocker |
|---|---|---|---|---|---|---|
| 6 cm | 272 | 1286 | 470 | 243 | 20% | `Landscape_0` @ 18.3 cm |
| 15 cm | 174 | 1291 | 563 | 297 | 24% | `Landscape_0` @ 10.2 cm |
| 25 cm | 73 | 308 | 1647 | 965 | 79% | `Landscape_0` @ 24.1 cm |
| 40 cm | 23 | 19 | 1986 | 1175 | 96% | `Landscape_0` @ 10.0 cm |
| 60 cm | 1 | 0 | 2027 | 1217 | 100% | `SM_Board18` @ 33.5 cm |

The knee is between 15 and 25 cm, where the unreachable cascade collapses from 1291 roads to 308.
At 60 cm the terrain stops blocking at all and the worst blocker is finally a prop rather than the
ground - so the terrain's roughness across a 40 cm sample step on this map is 20-40 cm, and 6 cm
of clearance cannot span it.

**What raising it costs is less than it sounds.** The capsule centre is at
`surface + clearance + half_height`, so at 6 cm it spans 6-182 cm and at 40 cm it spans 40-216 cm.
The camera is at 170 cm and stays bracketed either way; the sweep keeps doing the job it is there
for, which is showing that the camera is not inside geometry. What is given up is seeing obstacles
below the clearance height - and nothing below 40 cm can contain the camera. The top rising from
182 to 216 cm makes head clearance slightly *stricter*, not looser.

6 cm was right for what it was written against: a flat apron and a flat industrial yard, where
the navmesh and the ground agree to within a centimetre or two. It is a step offset, not a terrain
allowance, and the two got conflated.

## Vetoing a place makes the road network bigger, not smaller

The reroute loop cuts the corridor at every place a previous attempt collided or put the camera
inside geometry, so the walk turns and takes another road. On Village it ran its four attempts and
produced this:

| attempt | collisions | depth too-close | vetoed | roads | centreline |
|---|---|---|---|---|---|
| 1 | 435 | 18/501 | 15 | 2748 | 1624 m |
| 2 | 27 | 19/500 | 34 | 3733 | 2170 m |
| 3 | 27 | 26/500 | 44 | 4385 | 2520 m |
| 4 | - | - | - | 4928 | 2734 m |

Two things in there are the opposite of what the mechanism was built for.

**The network grows as vetoes accumulate** - 1624 m to 2734 m. Cutting a hole in a wide corridor
turns one centreline into two that pass either side of it, so thinning yields a bushier skeleton
with more roads, and the largest-component rule then keeps the bigger graph.

**And the depth count rises with it** - 18, 19, 26. The collisions do fall (435 to 27, and on
AncientRuins 898 to 257 to 96), because a collision is a place. But every new branch the veto
opens is road the walk had not been down, and on a map with distributed clutter that road has its
own obstacles. The loop converges on collisions and diverges on the camera.

AncientRuins behaved differently - 80, 69, 33 too-close frames over three attempts, falling - so
the loop is not useless. It converges where the obstacles are localised and chases its own tail
where they are not. It also costs a full re-plan per attempt: 15-20 minutes on a 2600 m network,
so raising the attempt cap to eight spends two hours of a seven-hour instance budget on planning.

## A 60 cm depth threshold cannot tell a wall from being inside a wall

`PROBE_MIN_CM = 60`. The refusals it produced across four maps, with the nearest reading each:

| shard | nearest | what it is |
|---|---|---|
| WinterTown s00 | 10.0 cm | the camera's near clip - depth is CLAMPED, the camera is inside geometry |
| WinterTown s03 | 11.0 cm | the same |
| Village s01 | 17.4 cm | close to a wall |
| Village s02 | 21.1 cm | close to a wall |

10.0 cm exactly, repeatedly, is the near clip: the depth buffer has nothing valid to report because
the geometry is behind the near plane. That is a frame of the inside of a wall and it is bad data.
21.1 cm is a wall filling the frame - uninformative, not corrupt.

One threshold judges both, so a map is refused for the second kind. Whatever the right value is,
60 cm was never calibrated against the failure it exists to catch, and the clamp at the near clip
is a signal that separates the two cleanly.

## A shard that is recording looks exactly like a shard that is stuck

`runner.py` appends its attempt record after capture AND packaging finish, so for the four to five
hours in between the beacon says `attempts: []` - the same thing it says while freeze is still
planning. Twenty shards read `starting` for an hour and nothing distinguished the ones working
from the ones wedged.

What worked was CloudWatch: `CPUUtilization` at 27-42% is a machine planning or recording, and the
three shards sitting at 15-17% were the ones to look at. That is a workaround, not an answer - the
runner should write a beacon when it enters capture, and again per N frames, so the fleet can be
read from its own status rather than inferred from instance metrics.

`uploader.sh` has the same shape: it waits for `acceptance.json`, so a five-hour shard puts nothing
in S3 until it is completely finished. Zero bytes uploaded five hours into a seven-hour job is the
expected state, and it is indistinguishable from total failure.

## The black-pixel gate cannot tell a broken material from a dark room

`no_dead_black_regions` failed the ChemicalPlant episodes that reached S3, and it was wrong. It
gated on the median fraction of pixels that are zero in all channels, at 10%, on the theory that a
material which fails to compile falls back to the Default Material and this build draws that as
absolute black.

Measured over 60 sampled frames of each of ten shipped episodes:

| map | zero% median | largest connected zero block |
|---|---|---|
| ModularNeighborhood - **materials genuinely broken** | 28.0% | 25.1% |
| Modular_MedievalTown night - sound | 66.7% | 55.9% |
| Cave - sound | 25.2% | 18.8% |
| MiddleEast - sound | 5.7% | 2.9% |
| ChemicalPlant - sound | 11.1% | 3.0% |
| Tokyo / Downtown_West / Pyramids / ForestGasStation / WinterTown - sound | 0.0-1.7% | 0.0-0.4% |

The one broken map sits *below* two sound ones. No threshold on either statistic separates them,
so the second statistic - largest connected block, added on the theory that dense foliage
silhouettes scatter into thousands of tiny components while a Default Material mesh is one solid
slab - does not rescue it either. A night level and a cave crush to zero after tone mapping in
exactly the way the Default Material does.

The historical verdicts already said so, and nobody read them: Cave and Medieval_Nighttime both
FAILED this gate while `materials_compiled` PASSED, and ModularNeighborhood failed
`materials_compiled` too - which named `M_Dokyo_Foliage` and three others. Three false positives
out of three dark maps, and not one true positive that `materials_compiled` had not already caught
with the asset names attached.

It is now a statistic in `video_stats` carrying that table's conclusion, not a gate. The lesson is
narrower than "don't gate on pixels": the gate was calibrated by *looking at two sound maps and one
broken one* and finding a wide separation. Two of the ten maps would have refuted it. A threshold
is only as good as the worst case in the population it was fitted on.

## A gate that reads the wrong file reports a pass, not an error

With the pixel proxy demoted, `materials_compiled` carries the whole weight of detecting a Default
Material fallback - so it matters that it could pass vacuously. It scans the file at `UE_LOG` for
`Failed to compile Material` and passes when it finds none. Pointed at a *runner* log instead of
the editor log, it found none, and reported "every material in the level compiled for this shader
platform" for the one map whose foliage is visibly broken.

Both outcomes print the same sentence. Nothing in the pass distinguishes "a clean editor log" from
"a file that never contained editor output".

It now requires positive evidence that the file is an editor log which reached shader compilation:
a real one carries `LogInit` (118 lines in a sample) and `LogShaderCompilers` (8); the runner log
carries 0 of each. Absent those it skips, with the file's name in the reason. A skipped gate is an
honest gap; a pass earned by reading the wrong file is worse than no gate at all.

The same shape is worth looking for elsewhere in `package.py`: any gate whose pass condition is
"absence of an error string" passes on an empty file, a truncated file, and a file about something
else entirely.

## `finalise` crashed on exactly the nulls its docstring promised

Recovery is the point of splitting `finalise` out of `capture()`: run the metadata step on an
episode whose capture was killed. Its docstring says `st`, `engine_s` and `lighting` are unknown in
that case and "recorded as null with a note rather than invented". The code then did
`round(engine_s, 1)`, `st["render_ms"]` and `st["fps"]` unconditionally.

`TypeError: type NoneType doesn't define __round__` on the first real call. All 15 recovery
instances would have hit it, and the only reason they did not is that the function was run once
against a local episode first - 145950 frames of Tokyo with `engine_states.jsonl` truncated to
130000 to imitate a SIGKILL, `rgb/` and `depth/` symlinked so nothing was copied. That test cost
four minutes and caught a defect that would have cost fifteen instance-hours.

A docstring describing a contract is not the contract. The recovered summary now carries the nulls
plus a `measurements_unavailable` field saying why - they are measurements of the *recording
process*, and in a recovery no process ran to measure. Inventing a plausible `engine_fps` there
would be the exact failure this repo is organised against: the per-frame data is real, and a made-up
number beside it would make the whole file untrustworthy.

## This repo is behind the machine that runs the pipeline

`revisit_pipeline/` is the source of record per CLAUDE.md. It is not the newest copy. Diffing it
against the tarball the 31-shard fleet actually ran, the *deployment* side was ahead on four files:

| file | only in the deployment |
|---|---|
| `freeze.py` | waits out a freshly synthesised bounds volume's tile build before giving up on a spawn |
| `plan_navmesh.py` | `anchor_hint` region selection - without it every route goes to the globally largest patch, however far from the requested spawn |
| `walkpath.py` | `ground_align()` - traces the path onto the ground instead of carrying a corner's z through an arc |
| `run_task.py` | crowd support |

Each of those is a fix with a measurement behind it, living only on the runner. Shipping this
repo's copies to the fleet would have been a silent four-way regression, which is why the recovery
tarball replaced only the five files recovery needs and left the rest at the version that ran.

## `capture_engine` supports a short episode and `package.py` rejects it

`capture()` has an `allow_short_episode` path: when the engine stops before the plan is done, the
episode "becomes what was actually recorded - a complete episode of a different length", and
`ended_early_reason` records why. `package.py` then gated `frame_count_matches_frozen` on
`n == traj["frames"]`, and `trajectory.json` carries the FROZEN frame count - so every episode that
took that path failed a gate for doing exactly what the flag permits.

It went unnoticed because nothing had ever used the flag until recovery, where a short episode is
not an option but the definition: the capture was killed, so the episode is shorter than its plan
by construction. All 15 recovered episodes would have been rejected for it.

The gate now branches on whether an early stop was *declared*. Declared: it passes, and states the
shortfall and the reason. Not declared: unchanged - frames going missing with nothing accounting
for them is the defect it was built for. The discriminator is the declaration, not the count.

Worth checking for the same shape anywhere a gate compares a delivered quantity against a *planned*
one: the plan is an input, and an episode is allowed to legitimately differ from its input.

## Frames written past the last engine state are not short frames, they are poseless ones

A killed capture leaves rgb/ and depth/ slightly AHEAD of `engine_states.jsonl` - the image and its
state line are written at different moments in the tick. Those images are real pixels with no pose,
no timestamp and no action, so they cannot be part of an episode, and `frames.csv` is built from the
state lines, which means leaving them in place makes the delivered image count disagree with the
frame table.

`recover.py` moves them to `orphan_frames/` with a note, rather than deleting them: they are
recorded data, and a recovery script is not the right place to decide they are worthless. The
episode is then internally consistent and `all_frames_present` measures what it claims to.

The count is normally a handful. It is worth handling anyway, because the failure it causes is a
gate that fails for a reason that has nothing to do with the frames anyone will train on.

## The fix arrived after the fleet had already read the code

The two gate defects above were found by a local dry run *while* the 15 recovery instances were
booting, and they had already pulled the tarball. There was no way to push code to them: the fleet
has no SSM agent (`describe-instance-information` returns 0), so the only lever is stop/start.

Restarting was the wrong call. It would have cost ~40 minutes of finalise work and, worse, risked
losing instances to eu-north-1 capacity on the way back up - which is what `retry_capacity.sh`
exists for. Trading running recoveries for a verdict correction is a bad trade: the frames are
identical either way, and a wrong verdict is repairable while a volume that will not restart is not.

So the episodes ship with the as-run verdict and `longvideo/readjudicate.py` corrects it afterwards
from S3. That is cheap because the gates worth re-adjudicating need no pixels - only
`capture_summary.json` and `frames.csv`. It keeps the original as `acceptance_asrun.json`: the
verdict a run produced is part of that run's record, and overwriting it would erase the evidence
that it was ever wrong.

The general point: a fleet with no way to receive a code change has exactly one remedy for every
bug, and it is the expensive one. An agent that can be told to re-read its inputs would have made
this a 30-second fix.

## Starting a stopped instance runs none of its user-data

`recover_fleet.sh` tagged 15 stopped instances and started them, on the assumption that user-data
runs at boot. It does not. cloud-init's `scripts-user` module is **per-instance**, not per-boot: a
plain `#!/bin/bash` user-data executes on an instance's first launch and never again. All 15 came
up, ran nothing, and idled.

The symptom was 0% CPU across the whole fleet with the instances in `running` and no beacons after
70 minutes - which is *also* what a container that fails to start looks like, because `boot.sh`
exits 1 on that path without shutting the instance down. Two explanations, one symptom, and no SSH
(the AMI does not carry this host's key) and no SSM (the account has no Systems Manager instance
management role: `describe-instance-information` returns 0).

`aws ec2 get-console-output` settled it in one call, needing neither:

    cloud-init[1636]: ... running 'modules:final' at ... 05:51:21. Up 13.54 seconds.
    cloud-init[1636]: ... finished at ... 05:51:21. Up 13.59 seconds

The final stage began and ended 0.05 s apart. Our script waits for a GPU and starts a container -
it cannot finish in 0.05 s, so it never ran. That is evidence, not a story, and it cost one API
call against the 25 minutes of theorising it replaced. The repo's rule about grepping for the last
log line before the silence has an addendum: on EC2 the console output is available when the
instance is unreachable by every other means, including when it has no credentials of its own.

The fix is the MIME multipart user-data in `longvideo/userdata_perboot.mime`, whose cloud-config
half sets `cloud_final_modules: [[scripts-user, always]]`, applied by `recover_fleet.sh` with
`modify-instance-attribute` before each start. A tag-driven fleet *requires* per-boot execution:
the tag is the instruction and the boot script is what reads it, so re-tag-and-start is the entire
control plane, and it is inert if the boot script never runs.

Two hours of 15 g6.4xlarge instances went to this. The cheap check that would have caught it is the
one that caught it in the end - look at CPU a few minutes after starting a fleet, because a machine
doing nothing is visible immediately and indistinguishable from a machine working only if nobody
looks.

## The watchdog killed the packaging, not the capture

For most of a day the story was "the in-engine capture stalled and a watchdog killed it, orphaning
the frames". Every part of that except the killing was wrong, and the number that settled it was
sitting in the recovery logs:

| shard | frames on disk | planned |
|---|---|---|
| Tokyo s00 | 358,560 (4.15 h) | 358,560 |
| Downtown_West s03 | 406,080 (4.70 h) | 406,080 |
| ForestGasStation s01 | 452,160 (5.23 h) | 452,160 |
| Pyramids s01 | 432,000 (5.00 h) | 432,000 |

`contiguous == planned` on every one. **The captures finished.** Nothing stalled. What the
watchdog killed was `finalise` + `package.build`, which for a 400k-frame episode takes about two
and a half hours serially and prints nothing at all while it runs.

The mechanism is a three-way interaction, and each part looks reasonable alone:

1. `capture()` printed progress only when the frame counter moved.
2. When capture completes, the frame counter stops moving - so the log goes quiet at exactly the
   moment the long silent phase begins.
3. The watchdog measured the log's mtime and called 45 minutes of quiet a hang.

So the fleet's own success condition triggered its kill condition. Thirteen complete 4-5 hour
episodes were left unpackaged by a watchdog doing exactly what it was told.

The lesson is not "raise the timeout". It is that **a watchdog must measure the thing it claims to
watch.** This one claimed to detect a stalled capture and actually detected an absence of stdout,
which is a different event that happens to coincide with a stall - and also coincides with the
capture finishing. The fix measures the frame counter, and the heartbeat now prints every 30 s
whether or not the counter moved, labelling a stall as `STALLED 2400s at this frame`. A stall is
now the loudest state instead of the quietest.

Two more consequences worth keeping:

- **SIGTERM before SIGKILL.** `kill -9` runs no handler, and the metadata that turns frames into
  an episode was written only at the end. The watchdog now sends SIGTERM and waits 180 s;
  `runner.py` catches it and calls `salvage()`, which is `recover.recover()` - deliberately the
  same code path, because two implementations of "turn these frames into an episode" drift, and
  the one used less often is the one that breaks.
- **The estimate that set the budget was wrong in the same direction.** Routes were planned at
  24 fps and the engine sustains 14.4. Every shard was therefore given roughly 1.67x less wall
  clock than its own plan needed, which is why they were still packaging when the clock ran out.

## Unresolved: a frames.csv row with a missing column, on every shard

The recovery crashed on all thirteen episodes at the same line:

    package.py:478  perr = [float(r["pos_error_cm"]) for r in rows]
    TypeError: float() argument must be a string or a real number, not 'NoneType'

`csv.DictReader` fills a short line's missing columns with `None`, so some row of `frames.csv` has
fewer fields than its header. **Why is not known**, and the obvious explanation is ruled out: with
`contiguous == planned` there is no torn state tail, and a mid-line truncation of
`engine_states.jsonl` raises `JSONDecodeError` rather than producing a short row - verified by
truncating a real file mid-key and running it through.

Candidates not yet eliminated: a disk that filled during `finalise`'s write; a value the engine
emitted containing a delimiter or newline that survived quoting; an `engine_states.jsonl` carrying
lines from two different attempts.

What has been done is to make the next occurrence self-describing rather than to guess again.
`read_frames` now validates every row and refuses with the row index, the frame_id and the names
of the absent columns, instead of failing hundreds of lines later inside whichever gate touches
that column first. The gates are off for the current run, so this will not fire until they are
turned back on - which means the diagnosis is deferred, not obtained. Recorded here so it is not
mistaken for solved.

## `accepted: null` is a third outcome and the uploader needs to know it

Running with the gates off raises a question the pipeline had no answer for: where does an
**unjudged** episode go? `uploader.sh` had two branches - accepted to the normal prefix, anything
else to `_rejected/`. Sending an unjudged episode to `_rejected/` asserts a failure nobody
measured, which is the same class of error as presenting an unmeasured episode as passing.

So `accepted` is now tri-state: `true`, `false`, and `null` for "the gates did not run".
`acceptance.json` is still written in that case - the uploader waits for that file, and its absence
is what orphaned thirteen episodes - and it says plainly that nothing here asserts the data is
sound and nothing asserts it is not. Unjudged episodes land on the normal prefix, and
`longvideo/readjudicate.py` computes the verdict later from S3, which is cheap because the gates
worth re-running need `capture_summary.json` and `frames.csv`, not pixels.

The first version of that tool got this wrong in an instructive way: it downloaded no depth frames
and then re-ran the depth gates, turning a bogus `no_dead_black_regions` failure into a bogus
`metric_depth_present` failure. A different wrong answer is not progress. Gates whose inputs were
not downloaded now carry the run's verdict forward, labelled as not re-measured.

## The dev host and the fleet were not running the same code path

`write_table` writes parquet when pyarrow imports and falls back to CSV when it does not. The dev
host has pyarrow; the fleet does not - `sequence.json` on a delivered episode says
`"frames_table": "frames.csv"`. So on the fleet, and only on the fleet, `build()` did this:

    finalise            writes frames.csv          (358,560 complete rows)
    build: read_frames  reads the whole table into memory
    build: write_table  ImportError -> REWRITES frames.csv from those rows
    build: acceptance   crashed on a None in a column

Two writers for one file inside a single `build()`, one of which reads the file first and writes it
back. Locally that branch never executes, so every local test exercised a path the fleet never
took. That asymmetry is the structural reason a day of "green locally, broken on the fleet" was
possible at all, and it is worth more attention than the specific bug: **an optional dependency
that changes which code runs is a second, untested pipeline.**

The fallback has a smaller defect of its own: `fieldnames=list(rows[0])` takes the schema from the
first row's keys rather than from `FIELDS`, so a table whose first row picked up a `None` key from
`csv.DictReader` would be rewritten around that shape.

What this does NOT explain is the corruption itself, and that is still open. Ruled out by
experiment on 400k rows - the fleet's actual episode size, which no earlier test reached:

- `finalise` alone: 400,000 rows, 166 MB, zero short rows.
- `finalise` -> `read_frames` -> `write_table` CSV fallback (pyarrow forced absent) -> re-read:
  byte-identical, zero short rows.

Also ruled out: a torn state tail (the bad row was in the middle of the file, not at its end, and
`contiguous == planned` on every shard); a full disk (4 TB gp3 root, and the frames survived a
stop/start so they are not on the 600 GB ephemeral mount); a missing field value (that writes an
empty column and fails as ValueError on `float('')`, not as TypeError on None - the observed rows
were short by seven *columns*).

So the remaining explanation is at the instance's I/O layer, and it is not reachable from here: the
fleet has no SSH key for this host and the account has no SSM role. The next round prints `df -h`,
`df -i`, the mount table and `du` of `episodes/` before it starts, and `read_frames` now quotes the
offending line with its byte length. Recorded as open, with the eliminations, so the next person
does not repeat them.

It did not recur. The second round packaged all fourteen shards with clean tables, so whatever it
was is intermittent - which is the least comfortable result, because an intermittent corruption of
the per-frame table is exactly the failure this repo is organised against: the frames look right,
the timestamps are even, and one row in three hundred thousand has no pose.

## A refusal that contradicted its own numbers

Three MedievalNight shards were refused with:

    the depth probe found geometry closer than 60.0 cm on 0 of 499 probed frames (nearest 73.7 cm)

Zero frames too close, nearest reading 74 cm, refused anyway. The verdict was right and the
sentence was wrong. `probe_route` returns `clear` as three vetoes ANDed together - too-close
frames, all-sky probes, thin-depth probes - and `runner.py`'s message described only the first.
The veto that fired was all-sky: at night in a medieval street, a `look_up` at +15 deg pitch
renders no geometry within 200 m, and the capture actor refuses to write a depth channel with no
depth in it and ends the run. The code even records the incident that made this a veto: one such
frame at 97,519 of 146,845 cost 64 minutes of recording.

So the refusal protected the run. But it took two rounds of reading code to learn that, because
the message answered a question nobody was asking. Same shape as `materials_compiled` passing on
the wrong file and the watchdog measuring stdout instead of the frame counter: **a check whose
report describes a different condition from the one it evaluated.** The message now names each
veto that fired with its count and example frames, and the `[freeze-cov] depth probe:` log line
prints all three counts and the verdict.

The underlying limitation stands and is worth its own fix: an all-sky frame is not corrupt data -
it is a frame whose every depth is "nothing within range", and the `-1` sentinel exists precisely
to say so. `ASimWorldCaptureActor` aborting on it is a defensive choice from when an empty depth
buffer usually meant a broken readback. A night map with open sky between rooftops turns that
defence into a route constraint: three of eight MedievalNight shards refused, on routes that were
otherwise clean. The right fix is in `cpp/SimWorldCaptureActor.cpp` - write the all -1 frame and
carry on, flagging it - not in the probe. Deferred; noted so it is not rediscovered.

## A prefix move and a concurrent write leave the new object behind

`aws s3 mv --recursive` from `_rejected/<ep>/` to `<ep>/` was started at 10:26 for 278,382
objects. `readjudicate.py --apply` wrote the corrected `acceptance.json` into `_rejected/<ep>/` at
about 10:40. The move walks keys in order and had passed `acceptance.json` within its first
minute, so the corrected verdict was never moved: it sat alone in the old prefix while the new
prefix carried the *old* verdict that the move had copied at 10:27. Fifty-eight minutes later the
episode looked complete and correctly placed, and said `accepted: false` for a reason that had
been disproved.

Nothing failed and nothing logged. The two operations were each correct; running them at the same
time on the same prefix produced a result neither would have produced alone. Fixed by hand for the
one episode it hit (the sibling's move started after its re-adjudication, so it was fine), and the
rule is the plain one: **finish writing to a prefix before you start moving it**, and after any
bulk move, list the source prefix - a non-empty source is a write that happened mid-move.

## Pruning 39 roads out of 846 removed 65% of the map, and the route reported 100% coverage

Hwaseong (a palace: courtyards joined by gates) came back from the fleet as 8-minute episodes
covering "260 of 260 roads, 100%". The survey had said 642 m of centreline; the episode's network
was 114 m. Same navmesh, same region (5,640 m2), same corridor (3,420 m2). The difference is in
`head_clearance_pruning`:

    roads_before 846   centreline 518 m
    roads_blocked 39                         <- the capsule genuinely cannot pass these
    roads_after_pruning 807
    roads_kept_connected 260   114 m  22%
    dropped_disconnected 547                 <- passable, but cut off by the 39
    components_after_pruning 9

Only 4.6% of the roads are blocked. They are the gates. Remove them and the palace is nine islands;
the planner keeps the island the spawn is on and plans a perfect cover of it. Every downstream
number is then true and misleading at once: coverage 100%, collisions 0, one complete pass - of
22% of the walkable map. This is the plausible-looking-wrong-data shape again, and it passed every
gate because every gate measures against the pruned network.

`kept_fraction: 0.22` is also almost exactly the WinterTown data point already in this file - ground
clearance 6 cm keeps 20% of the network - so the cause is the same: door sills and the first step of
a stair are higher than 6 cm, and stepped architecture (Hwaseong, TemplePlaza, MedievalCastle) turns
every threshold into a wall. TemplePlaza kept 36%. Street maps (Tokyo kept more than the survey
predicted) are unaffected.

Two consequences:

1. **A pruning that disconnects the network must be reported as such, and probably refused.**
   `dropped_disconnected` is already computed; nothing reads it. Covering the surviving island and
   calling it the map is exactly what `refused_partial_cover` exists to prevent, and it prevents
   the 1663/1664 case while letting the 260/846 case through, because the denominator was
   redefined before the check ran. The fraction to gate on is `centreline_kept_m /
   centreline_before_m`, not `roads_walked / roads_total`.

2. **The body's ground clearance is a per-map parameter, not a constant.** The template's 6 cm is
   the Tokyo profile. `worst_blocked` names what the sweep hit and how high: on TemplePlaza
   `StairLong3` at 38 cm, `StairLong_406` at 35, `TileSmall40` at 34, `StairShort4` at 11.5,
   `DecorativeCircle` at 4-6; on Hwaseong unnamed static meshes at 0-34 cm. Every blocker is a
   sill, a stair or a platform edge under 40 cm. 45 cm clears all of them by construction.

## The probe that was supposed to measure the clearance measured nothing

`probe_matrix.py` was run to pick that value rather than guess it. It returned three GOs at 40 and
60 cm - each with the *identical* pruning line to the 6 cm run: 39 blocked, 547 unreachable, 22%
kept. Three identical results from three different inputs is the signature of a dead lever, and it
was: the script set `fc.GROUND_CLEARANCE_CM = clearance`, a module global, while `freeze_coverage`
reads the clearance into a function-local of the same name from `env or task["body"] or 6.0`. The
module global is never consulted. The script's own comment, four lines above, warns that
reassigning `C.MIN_CLEAR_CM` is dead for the analogous reason - and then does the analogous thing
to the other lever. The corridor lever, which goes through the environment, worked (805 -> 536
roads at 120 cm), which is what made the ground-clearance result look like a measurement.

So the "GO at 40 cm" was a GO at 6 cm, and had it been acted on it would have relaunched sixteen
shards to record the same courtyards again. Fixed to set both the environment and `task["body"]`,
the two paths freeze actually reads. The general rule this is the third instance of today: **when a
sweep returns the same number for every setting, the setting is not reaching the code.** Check
that before reading the table.

Ground clearance was set to 45 cm for the second pass on the strength of the `worst_blocked`
heights, not the probe. The `refused_disconnected_network` gate makes a wrong guess cheap - it
refuses at freeze, fifteen minutes in - which is the right way to be able to skip a measurement.

Measured on the first second-pass episode, TemplePlaza r2s01, same map and same navmesh as the
35% run:

    ground clearance     6 cm                 45 cm
    centreline kept      173 / 499 m  (35%)   486 / 491 m  (99%)
    roads blocked        86                   1   (StairCorner_397 at 19.2 cm)
    dropped disconnected 371                  0
    components           10                   1
    one complete pass    12-15 min            38 min, 791 / 791 roads
    delivered            12-15 min            46 min - a second pass had begun when the cap hit

The plan's cap (1.25 x the survey's 37 min) was set for the survey's network and the real pass is
38 min, so the cap bound before the second pass finished; loosen it for stepped maps.

## Three operator errors from one afternoon, each already documented in this file

Recorded because the value of a pitfalls file is in the re-reading, and none of these were re-read.

1. **"No gates, upload what is recorded" was applied to one code path.** `SKIP_GATES` was wired
   into `recover.py` and not into `runner.py`, so the recovery round shipped unjudged while the
   fresh recordings that followed ran the full acceptance and filed four episodes under
   `_rejected/` for an action-mix band that a 114 m network cannot meet. The instruction was
   about the job, not about one script. Fixed: `runner.py` honours `SKIP_GATES`, `driver.sh` and
   `boot.sh` pass it through, `LongVideoSkipGates=1` is a launch tag, and a gates-off recording
   reports `recorded` - not `accepted`, because nothing judged it.

2. **`pkill -f` matched the shell that ran it - fifth time today.** The pattern used the
   `[r]2_retry.sh` bracket trick, which protects against matching pkill's own argv and does
   nothing about the *enclosing* `bash -c` whose command line also contained the string. The
   shell died at exit 144 with the remaining steps unrun and the loop it meant to restart dead.
   The rule in this file says "use the bracket trick or kill by PID"; the bracket trick is not
   sufficient when the pattern also appears elsewhere in the same command. Split the pattern
   across a variable (`pat='r2_retr'; pgrep -f "${pat}y.sh"`) so the joined string exists
   nowhere in the caller's argv, or kill by PID.

3. **A retry list built from `| tail -3` of the launcher's output lost two of three failures.**
   Sixteen shards launched, three failed for capacity; the grep that built the retry list ran on
   output that a `tail` had already cut to three lines, and found one. Fixed at the source:
   `launch_fleet.sh` writes every failed shard id to `$FAILED_FILE` itself. A launcher knows what
   it failed to launch; nothing downstream should have to reconstruct that from its log.

A fourth, caught before it ran: the `$FAILED_FILE` line used `$P` one line before `P=` was
assigned, which under `set -u` would have aborted every future launch at line 20. Shipping a
one-line addition to a script that a retry loop invokes every three minutes is a change to a
running system, and it was checked only after the fact.

## A shard-id suffix that the uploader could not strip put episodes under the wrong map

Round-two shards were named `<slug>__r2s02` so they would not collide with round one's
`<slug>__s02`. `uploader.sh` derived the map prefix with `${SHARD_ID%%__s*}`, which strips
`__s02` and does nothing to `__r2s02` - there is no `__s` in it. The whole shard id became the
"map", and the first round-two episode landed at
`Game_ModularTemplePlaza_Maps_ConceptMap__r2s02/_rejected/<episode>/` instead of
`Game_ModularTemplePlaza_Maps_ConceptMap/<episode>/`. Sixteen instances carry that uploader and
all sixteen will do the same; `unreject_all.sh` folds the misplaced prefixes back.

The naming change and the parser that consumed the name were three files apart, and the change
was tested by generating tasks (which worked) and not by following the id through to the bucket
layout. Fixed with `sed -E 's/__r?[0-9]*s[0-9]+$//'`, which handles both forms. The general
point: an identifier's format is a contract with every consumer of it, and adding a field to it
is a change to all of them, not to the producer.

## The watchdog that was fixed this morning killed eight shards this afternoon

The morning's fix made `driver.sh`'s watchdog measure the frame counter instead of the log's
mtime, so that a finished capture's silence would not be mistaken for a hang. It read the counter
from `[capture] N/` lines. Before capture begins there are no such lines, and the counter it
tracked was the empty string - which never changes. Forty-five minutes after boot it reported
`frame counter stuck at none for 2702s`, sent SIGTERM, and killed a runner that was still in
freeze. Then it did it again on the retry. All eight MedievalCastle shards died this way, and
nothing recorded it: the SIGTERM handler had been installed around `capture()` only, so during
freeze the default handler ended the process with no state written and the beacon still said
`starting`. Eight instances stopped for a reason the fleet could not see, for the second time
today, for a different reason than the first.

Castle's freeze genuinely takes more than 45 minutes. Its network is 3,066 roads and 1.7 km of
centreline, the first route had 5,570 collisions, the reroute had 4,340, and one pass is
1.95 hours - the survey said 51 minutes. None of that is a hang, and all of it prints constantly.

Two fixes. The stall clock now starts only when a frame counter has been seen; before that the
log's mtime remains the only guard, which is right because freeze is never silent. And the SIGTERM
handler covers the whole run, so a kill during freeze records `terminated_during_freeze` with the
time spent. The morning's lesson was "measure the thing you claim to watch"; the afternoon's is
its corollary: **a watchdog must know which phase it is watching, because the same reading means
different things in different phases.** Zero frames in minute 50 of a capture is a hang. Zero
frames in minute 50 of a freeze is a big map.

## A headless editor left over from yesterday's probe nearly took the host down today

A background task on the dev host was killed by the kernel for memory. `ps` showed why: an
`UnrealEditor ... AncientRuins -RenderOffscreen -unattended`, launched by yesterday's
`probe_once.sh` run through enroot, still alive 24 hours later at 37.5 GB RSS and 541% CPU, with
nothing connected to its UnrealCV port. Beside it: the `aws s3 sync --include x14400` clip download
from four hours earlier, which `pkill -f` had reported killed and had not killed, and a
`--summarize` over eleven million keys started by a size monitor whose method stopped scaling at
about two million.

None of these were doing anything. Together they held two thirds of the host's memory and most of
its cores under the machine that serves the review page, and the first symptom was an unrelated
job dying. Two rules follow: **anything this session launches, it lists at the end** - `ps` for
editors and syncs, `TaskStop` for monitors - and **verify a kill by PID, not by the absence of an
error from `pkill`**, whose silence means only that the pattern matched nothing, which is also
what it says when the pattern was wrong.

## The depth probe's 60 cm threshold refused a two-hour episode over one frame at 57.5 cm

MedievalCastle r2s06, second pass, 45 cm clearance, full network: refused at freeze because
1 of 501 probed frames had geometry at 57.5 cm. Not 10.0 cm - the near-clip clamp that means the
camera is inside a mesh - but 57.5, a wall a little over half a metre away for one frame, 2.5 cm
under a threshold this file already records as never having been calibrated against the failure it
exists to catch. Today's tally of that threshold: WinterTown s00/s03 at 10.0 and 11.0 cm (true
penetrations, correctly refused); Village s01/s02 at 17.4 and 21.1 cm and Castle r2s06 at 57.5 cm
(proximity, wrongly refused). Three false refusals to two true ones, on a check whose true
positives are all at the clamp.

The two classes are separable by the value itself: a depth clamped at the near plane reads the
near plane, ~10-11 cm, every time; a wall reads its distance. The veto now fires on the clamp (any
probe under `PROBE_CLIP_CM`, 12 cm) and reports proximity under 60 cm as a count without refusing.
What is lost is nothing: the frames a proximity veto would have prevented are frames of a wall,
which are uninformative and are not wrong, and the other vetoes - all-sky and thin depth, which
protect the capture actor from aborting - are untouched.

## Day's end, 8 Sep: 71 episodes, 108.13 h, 9.34 M frames

Where the morning started: 2 episodes on S3, both under `_rejected/` for a gate that was wrong,
and 13 shards' worth of frames sitting on stopped disks with no metadata.

Where it ended, all on the normal prefixes, `_rejected/` empty, no misplaced prefixes:

    Pyramids                 4 x 5.0 h   20.00 h    recovered from disk
    Downtown_West            4 x 4.7 h   18.80 h    recovered from disk
    MedievalCastle  r2       6 x ~2 h    11.97 h    45 cm clearance, 2.2 h cap
    ForestGasStation         2 x 5.2 h   10.47 h    recovered from disk
    Suburb                   8 x 52 min   8.95 h    B round, clean at 6 cm
    Tokyo                    2 x 4.15 h   8.30 h    recovered from disk
    ChemicalPlant            4            7.50 h    2 original + 2 recovered
    ContainerYard            6 x 59 min   5.87 h    B round (2 refused: 1663/1664 roads)
    TemplePlaza     r2       8 x ~44 min  5.83 h    45 cm clearance
    Hwaseong        r2       8 x ~44 min  5.83 h    45 cm clearance
    TemplePlaza     r1       8 x ~14 min  1.88 h    island covers, superseded
    MedievalNight            3            1.49 h    5 of 8 lost to all-sky frames
    Hwaseong        r1       8 x ~9 min   1.25 h    island covers, superseded

Verdict states across the 71: 37 accepted, 23 unjudged (gates off, by instruction), 11 rejected
by gates since demoted or recalibrated. None of the 11 is a data defect; the labels are kept in
each episode's acceptance.json and not used for filing.

What produced the hours was not more machines but three corrections to what the machines were
told: the fps ratio (0.906 -> 1.665), the ground clearance on stepped maps (6 -> 45 cm), and one
pass per shard instead of four. What lost hours was every place a check reported something other
than what it measured - the watchdog, twice; the depth probe's message; the pixel proxy; the
island cover called 100%. The fixes for all of those are in the tree. The 47 stopped instances hold
nothing S3 does not.

## Two small ones from the clip batch, 9 Sep

**ffmpeg infers the container from the output extension.** Writing to `<name>.mp4.part` and
renaming on success is the right pattern for an atomic artifact, and it fails with `Unable to
choose an output format for '....part'` unless `-f mp4` names the muxer explicitly. The single-file
test wrote straight to `.mp4` and passed; the batch added the `.part` and every episode failed after
a 200 s download. Test the exact command the batch will run, not a simplified one.

**`pkill -f` / `pgrep -f` / `awk '/pattern/'` against `ps`: every alternative needs the bracket.**
Seventh self-kill of the session. The pattern `bash -c on[e]|build_cli[p]s` had the bracket on one
alternative and not the other, and the unbracketed one appeared verbatim in the calling shell's
own command line. The rule is not "use the bracket trick"; it is **no literal substring of the
pattern may appear anywhere in the argv of the shell running it**, which in practice means every
alternative gets a bracket, or the pattern is split across a variable, or the kill is by PID from
a list produced one step earlier.

## Sizing every map's core without recording any of them, 9 Sep

`survey_core.py` reads `frozen/nav/<slug>.bin` and nothing from the engine, so ranking the whole
catalogue only needs a navmesh per map - but a navmesh needs a live editor, one launch per map
(a map switch in a live editor keeps the previous level's navmesh). 25 maps had one from earlier
freezes; 85 of the 86 maps with a start-positions file resolve to an asset path (`Game_Hangar_Maps_Hangar`
is in no inventory), so 60 had to be exported. `longvideo/nav_export_only.py` is `freeze()`'s first
three calls and nothing else - connect, `pick_spawn`, `nav_export` - and boot.sh grew a
`LongVideoNavExport=<batch>` mode that walks `nav_export_batches.json` through `probe_once.sh`,
uploads each `.bin/.json` to `_nav/`, and shuts the instance down. 27 s per export once the editor
is up; the editor launch is the cost.

Three things the fleet taught, in the order they cost time:

**Four of eight instances wedged on one map each, for 2.5 hours, with nothing firing.** LowPolyMedieval
`Map_5_Top-Down`, ModularCourtyard `overcast` and `sanny`, SwimmingPool `ChangingRoom_Male`. Past
`launch_ue.sh`'s 19-minute wait, past `engine.connect`'s 900 s, past every `simworld()` timeout -
the per-map loop had no cap of its own, so the 18 maps queued behind them never ran and their
boot logs (uploaded only at the end) were never seen. Stopped and tagged `LongVideoWedged`; the
18 redistributed over four fresh instances with the four suspects isolated in their own batch. The
loop now runs each map under `timeout -k 60 1500`, writes a `TimeoutError` NAVEXPORT line so the
map is a refusal and not a gap, and kills the editor the timeout leaves holding the UnrealCV port.
Rule: **a fleet loop over maps needs a per-item cap that does not depend on any of the item's own
timeouts firing.** Every layer below had one and none of them did.

**"No start point projected" is not always the navmesh's fault.** Ten maps refused in `pick_spawn`.
Two (ChemicalPlant_1, SchoolGymDay) have start-positions files whose every entry is
`x: null, y: null, z: null` - nothing to export from. For the other eight, the survey-only fallback
(export whatever navmesh the engine built around the first start point, flag `spawn_projected:
false`) ran and came back with `navmesh present but empty (0 verts ...)` and `ground_z: null`: the
downward trace under the start point finds nothing at all. CastleRiver's test1 is at (-34712, 16000)
over void. These start points were collected for a different purpose and were never checked against
the level they name; the earlier pipeline recorded CastleRiver anyway because `freeze.py` falls
back to the trace when nothing projects - the "camera in the sky" configuration its own comment
warns about. So the eight are refusals of the start-positions file, not of the map, and the core
survey lists them as unmeasured with that reason rather than with a small number.

**Fix the batch file before the tarball, not after.** A syntax error in the script that wrote
batches 10-13 was followed, in the same command, by the tarball rebuild and the launch of the four
instances that would read those batches - the `&&` chain only guarded the tar. Caught and re-uploaded
31 s after launch; the instances take ~3 min to reach the tarball fetch. Generate inputs, verify
them, then build the artifact that ships them, in separate commands.

Final tally, 12:05: 86 maps with a start-positions file -> 73 measured (25 cached + 48 exported),
13 unmeasured (2 all-null files, 10 start points over void, 1 with no asset path). 18 instances
launched in all, 5 of them stopped wedged; every wedged map that was retried on a fresh instance
exported in under 15 min, so the wedge was never the map. Survey in `_core_survey/` (the 25-map
one kept as `_core_survey_25maps_2026-09-07/`), metadata in `results/core_survey_2026-09-09/`,
page at `/longvideo/core/`. Of the 73: 10 have a core >= 2000 m2, 27 under 300 m2, median 597 m2.

## The rotation rate is now one number, 14 Sep

Asked: is the camera's yaw speed fixed? It was not, on three levels - a per-episode draw from
the tier (fast: 60-90 deg/s), a raised-cosine envelope inside every turn peaking at 1.35x that,
and a per-frame ceiling on the walking loop. Asked next: fix it at 45 deg/s.

`coverage.py` gained `turn_profile` ("cosine" as before, or "constant") threaded through
`PoseWriter`, `generate`, `measure_passes`, `size_episode` and `plan`, and `plan()` honours a task
key `yaw_deg_per_s` that pins the rate (the tier draw still happens so the seed's random stream is
unchanged; the tier is kept for the 1.5x gate and the metadata). Constant means every turning and
looking frame rotates exactly yaw_rate/fps, with ONE remainder frame per turn. The first version
spread the remainder over the whole turn and the median turning frame on Tokyo came out at 44.6
with a floor of 22.5 deg/s - "fixed" has to mean the frames are equal, not that the mean is right.
Verified on a full Tokyo covering pass: max frame rate 45.000, 92.5% of the 41k turning frames
exactly 45.0, the rest the remainders, no walking frame above 45, mix still in band.

Cost: passes run 6-8% longer than at the fast tier (six maps dry-run: 1.06-1.08x), because 45
deg/s is slower than the 60-90 tier it replaces.

`tasks/longvideo_template.json` now carries `yaw_deg_per_s: 45`, `turn_profile: constant`,
`yaw_tier: medium`. The fleet generates each shard's task from the template AT BOOT
(`runner.py` calls `gen_task.py`), so what governs a recording is the template inside the S3
hotfix tarball, not any file on the dev host; the tarball was rebuilt with the new template and
`coverage.py`. The seven `tasks/lv_*.json` on the dev host are local test files and still say
`fast`. `frozen/*/trajectory.json`
records `turn_profile` and `yaw_rate_pinned` so a delivered episode says which it was.

## The deployment was behind the repo, and a tarball built from it shipped the old code, 14 Sep

Rebuilding the hotfix tarball for the nav-export batches, I built it from the deployment directory
(`local_run/pipeline`) because that is what the tarball has always been described as. The 8 Sep
fleet tarball, checked afterwards against its backup, had in fact been built from THIS repo: the
deployment copies of `longvideo/runner.py`, `driver.sh`, `uploader.sh`, `recover_fleet.sh`,
`launch_fleet.sh`, `gen_shards.py`, `probe_matrix.py` and `freeze_coverage.py` were the 7-8 Sep
morning versions - realtime ratio 0.906, the watchdog that killed packaging, the uploader without
the `__r2s02` slug fix, the 60 cm depth-probe veto. So for about four hours on 14 Sep the S3
tarball carried those regressions. No fleet booted from it (the nav-export instances had all
finished), so nothing was recorded with it. Repo copies synced into the deployment, the stale ones
kept under the scratchpad, tarball rebuilt and its contents grepped before upload (ratio 1.665,
PROBE_CLIP_CM present, slug regex present, turn_profile present).

Rule: the repo is the source of record for these files, and the deployment is a mirror that has
to be re-synced from it, not the other way round - and a rebuilt tarball is verified by reading
the files back out of it, never by trusting the directory it was made from. CLAUDE.md's "source of
record" line meant exactly this and was not followed.

## `render.exposure: "fixed"` was applied by nothing; now it is, and the bias is per map, 14 Sep

Every task since the first episode declared a fixed exposure. `task["render"]` was copied into
`capture_summary.json` and read by no code, so every episode on S3 was recorded under whatever
auto-exposure the purchased level ships - `_diag_exposure.py` had already measured it on Tokyo
(same pose twice, 23.9/255 apart, 14.6x the frame-to-frame floor). The Dubai project that was held
up as the example has `r.DefaultFeature.AutoExposure=False` in its ini AND lights it built itself to
suit that fixed exposure; the two go together.

Done: `ASimWorldCaptureActor` takes `ExposureBiasEV, bManualExposure` and pins the RGB capture's
own `PostProcessSettings` (`AEM_Manual` + bias, which sits above any PostProcessVolume in the
level) - the same override `WMCCaptureActor` has always made. `USimWorldCapture::CaptureRgbPng`
renders one pose at a stated exposure and returns its histogram, so a map's bias can be swept
before any episode exists (`exposure_probe.py`). `capture_engine` passes the task's declaration
in and REFUSES a fixed exposure with no `exposure_bias_ev`.

What the Downtown West test measured (`/longvideo/exposure/`, same frozen route recorded twice):

- Manual exposure meters from the default camera, EV100 ~ 9.9 - a sunlit exterior. The level's
  sun is a few lux. The first sweep, -2..+3 EV, came back black at every bias; the picture lives
  at **+11 to +12 EV**, and +11.5 was chosen. So a default bias of 0 is a 20-hour black episode
  with every frame count right. There is no default.
- The first pipeline's limits (0.5% blown, 3% near-black) are unreachable on a street with sky
  and arcade shadow in one frame: the level's own auto-exposure sits at 2.6% / 11%. Limits are
  now 5% / 20% and the choice is the bias losing the fewest pixels at both ends.
- Frame-aligned (same frame, only the mode differs): a constant 16/255 offset (0.18 EV of bias
  choice) and, after removing it, auto-exposure DRIFT of median 2.7, p90 12.6, max 13.6 /255;
  247 of 948 sampled frames over 10, where the route turns from arcade shadow to open sky.
- Same place twice: auto median 1.21 / max 8.1, manual 0.74 / max 9.8 - at 60 cm / 10 deg the
  residual is viewpoint, and the pairs are few because an out-and-back returns facing the other
  way. The aligned comparison is the one to read.
- Cost: near-black 15.8% vs 12.1% - the shadows auto-exposure used to lift stay dark. That is
  the property being bought.

Downtown's auto-exposure is clamped tightly by its own post-process, so the gain here is tens of
grey levels, not Tokyo's; the fix matters most on maps that do not clamp. Fleet: boot.sh now runs
`cpp/install.py` + an incremental `Build.sh` before the first editor launch (16 s here), because the
AMI's compiled module predates this. Still to do before the next batch: a bias per map, from the
sweep, into `longvideo_shards.json` (`exposure_bias_ev`, which `gen_task.py` now forwards).

## The Dubai recipe, applied to a purchased level: hide its lighting, light it ourselves, 15 Sep

Pinning exposure on the capture (previous entry) kept the author's lighting and grading and only
froze the meter; the user wanted what `~/dubai_ue_detail` has - no dark and no blown frames at all.
That project gets it by turning auto-exposure off project-wide AND lighting the scene itself with
one sun, one sky, one haze tuned to that fixed exposure. `lighting_rig.py` does the same to any
level for the session: hide every directional/sky/atmosphere/fog/cloud component (blueprint skies
included) and disable every post-process volume and component, spawn the rig (sun 7.0 at pitch
-38 / yaw -55 with atmosphere sun light and a 1 deg source, SkyAtmosphere, sky 2.5 real-time
capture, height fog 0.006 - the values finalize_render_quality.py left Dubai with), set
`r.DefaultFeature.AutoExposure 0` and `r.EyeAdaptationQuality 0`. Local lights stay: they are
content. Nothing saved; everything hidden and spawned is in capture_summary.json.

Downtown West, same frozen route, current vs rig (`/longvideo/lighting/`): near-black 12.1% ->
8.3% (worst frame 32% -> 25%), blown 0% both (worst 3.0% -> 0.2%), brightness 5-95% spread
64 -> 53, same-pose repeat difference 1.62 -> 1.42. Look: neutral and flat, the author's warm grade
gone, road light grey. p50 164 - about half a stop bright; sky 2.0 / sun 6 would centre it, one
global number for every map, not a per-map calibration. Template now `lighting: rig`,
`exposure: off`; `fixed` + per-map bias and `level` + `auto` remain selectable.

Two things that cost a round trip each:

- **`EditorLevelLibrary.spawn_actor_from_class` returns None in this session.** The editor runs
  PIE (`start_hook.py` calls editor_play_simulate), `w` is the PIE world, and the editor library
  spawns into the editor world. Hiding components on existing actors works from Python in either
  world; spawning does not. The rig is spawned by `USimWorldCapture::SpawnLightingRig` from C++
  with the world context, as everything else this module spawns. There is no `ASkyAtmosphere`
  actor class in Engine/Classes; a `USkyAtmosphereComponent` on a plain actor is what placement
  does. `RecaptureSky()` after `SetRealTimeCapture(true)` or the first frames use the old cubemap.
- **The harness's low-memory watchdog killed the editor launch twice** when run as a tracked
  background command: UnrealEditor passes 30 GB RSS while loading Downtown. `setsid nohup` the
  launch+test chain so it is not in the task's process tree, and wait on its log with an `until`
  loop. UBT wanted `-MaxParallelActions=4` for the same reason.

## Fill: keep the author's light, replace their sky light, pin the exposure, 15 Sep

The Dubai rig fixed the dark frames and broke the colours (the previous entry). What was actually
dark was shadow with no ambient, on a level whose sky light is Static with no built data - so the
fix is the sky light alone. `lighting_rig.apply_fill(factor)`: hide the level's sky light, spawn
ours (C++ `SpawnSkyLight`, Movable, captured-scene) at the level's own intensity x factor, leave
the sun, the painted sky and the post-process grade alone, pin manual exposure on the capture at
a bias swept in-session. Downtown West, same route, four recordings (`/longvideo/fill/`):

| | current | fill x1 | x2 | x3 |
|---|---|---|---|---|
| near-black median / worst | 12.1% / 32% | 5.7% / 19% | 4.4% / 15% | 2.7% / 10% |
| blown worst frame | 3.0% | 1.3% | 0.6% | 0.6% |
| brightness 5-95% spread | 64 | 62 | 55 | 51 |
| same-pose repeat diff (median) | 1.62 | 0.43 | 0.27 | 0.41 |
| chosen bias | auto | +11.0 | +10.5 | +10.5 |

Colours are the author's. x1 keeps the most contrast; x3 is flat. Template: fill x2, exposure
fixed, bias `auto` - `capture_engine` now runs `exposure_probe.sweep` (12 poses) right before
recording and writes the choice into capture_summary, so a shard calibrates itself.

Four rounds it took to get there, each a real engine behaviour:

1. **`set_editor_property("intensity")` on a light in PIE does not reach the renderer.** x1/x2/x3
   recorded byte-identical frames. `set_intensity()` (the UFUNCTION) marks render state dirty.
2. **A real-time-capture sky light captures only SkyAtmosphere, clouds and height fog.** On a
   level whose sky is a painted sphere it captures black and its intensity is irrelevant; our
   own sky light at 18.0 changed nothing. `SLS_CapturedScene` + `RecaptureSky()` photographs the
   sphere. Corollary: `ensure_dynamic_sky`'s "Movable + real-time capture" fix, in place since
   MiddleEast, did nothing on atmosphere-less levels - on Downtown the level's sky light was dead
   before and after it. It now checks for a SkyAtmosphereComponent and uses captured-scene when
   there is none.
3. **`ensure_dynamic_sky` retuned OUR sky light.** Every recording calls it; it flipped the fill
   light to real-time capture (black), so probes were right and every recording was dark
   (x1 27% near-black, x2 = x3 exactly). It now skips actors tagged SimWorldFillSky / SimWorldRig.
4. **Changing an existing sky light's intensity + recapture after a recording did not land**,
   while a fresh spawn always did; the fill destroys and respawns per factor. Not explained,
   worked around, noted.

Rule that covers all four: a lighting change is verified by a rendered frame's histogram, never
by reading the property back. Every one of these read back exactly the value that was set.

## Adaptive per-map lighting: what it chose on four maps, and the rule that had to be added, 15 Sep

A fixed sky-light factor is one number for eighty maps. `lighting_calibrate.py` makes it per map,
before the first frame, fixed for the episode: try x1, 1.5, 2, 3, 4 in order; sweep the exposure
bias per factor with the level's own median grey as the target; the first factor whose render
meets the bar wins - near-black <= 3%, blown <= 1%, contrast (p99-p10) >= 80% of the level's own,
and median grey within 0.6-1.6x the level's own. None meets it: least total violation, flagged
`quality_bar_missed`. `render.skylight_factor: "auto"` runs it in the capture (10-45 s).

The fourth check exists because the first run without it "passed" MedievalTown Nighttime at x2,
+15.5 EV: near-black 2.8%, blown 0.04%, contrast 299% of a reference whose contrast was nearly
zero - a night street re-lit into a sunlit day with stars in the sky. Every check said yes; the
frame said no. A bar built from pixel fractions alone cannot tell "shadows lifted" from "scene
replaced"; the median-grey band ties the result to the author's brightness.

Second run (`/longvideo/calib/`):

| map | reference (auto) | chosen | result |
|---|---|---|---|
| Downtown West | p50 145, black 3.7% | x3, +10.5 EV | pass |
| Tokyo | p50 110, black 4.7% | x2, +8.5 EV | pass |
| ChemicalPlant_2 | p50 113, black 14.5% | x1, +12.0 EV | missed: black 12-14% at every factor |
| MedievalTown Nighttime | p50 0, black 79% | none | refused: no bias inside the sweep limits |

ChemicalPlant's near-black is full occlusion under pipe racks and inside sheds; a sky light does
not reach it, and the first run's "fix" (x3 at +12.5, 2.1% black) was the whole scene a stop
brighter, washed out - which the brightness band now forbids. If 3% is wanted there, the band has
to be widened to 2x, knowingly. The night level is refused rather than brightened; a night
recording, if wanted, is `lighting: level` with a bias picked by hand.

Also visible in the table: Downtown's reference black is 3.7% now, against 12.1% two days ago,
because `ensure_dynamic_sky` finally gives an atmosphere-less level a sky light that captures
something. The baseline moved under the comparison; the fill numbers before this entry were
measured against the older baseline.

## Two more self-kills (exit 144), 15 Sep - and a wedge right after connect

Eighth and ninth: a `pgrep -f` whose pattern was split across variables (`"$a$b"`) was still
matched, because the *same command* carried the assembled literal elsewhere - once in a heredoc
that edited a script by name, once in the relaunch line. The variable split protects only the
pgrep argument; the rule is about the whole argv. What finally worked: build the PID list with
`ps -eo pid,ppid,args | awk -v me=$$ -v pp=$PPID '$1!=me && $2!=me && $1!=pp && /pat[t]ern/'`,
in a command that contains nothing else, and do the edits and relaunch in the NEXT command.

The thing being killed: an editor that answered UnrealCV's connect and then never serviced the
first Python query - 20 minutes of silence, no log line, the same shape as the four nav-export
wedges. Frequency so far roughly one launch in ten. Every per-map loop now runs its step under
`timeout -k 30 1500` with one retry on a fresh editor; the cause is still unknown.

## The shimmer: a persistent view state that never converges, fixed by not keeping one, 15 Sep

Every recording, from the first fleet episode to today's, sparkles on brick joints, tile edges and
foliage. Measured on frames where the camera is STILL (the route's dwell phases): consecutive
frames differ by 1.8/255 mean, 0.4% of pixels by more than 20. Sky is clean. Same in all four
exposure schemes, so not exposure.

What did not fix it, each a full recording of the same route: FXAA, TSR with history at 100%,
`r.AntiAliasingMethod 0` (sharper, still 2.0), ray-traced shadows off and Lumen off (both cvars
ineffective at runtime - the image was unchanged, so nothing was tested), virtual shadow maps
off (1.6), pinning every temporal jitter sequence (`r.TemporalAASamples 1`, the three Lumen
`FixedJitterIndex` cvars; 1.79, unchanged), `bCameraCutThisFrame` every frame (1.87). And one that
"fixed" it wrongly: `bAlwaysPersistRenderingState = false` gives 0.00 - and no Lumen GI, so every
shadow is black.

What located it: `CaptureRgbPng` re-rendering one pose six times, a new component each time,
differs by 0.00 between renders, GI present. The difference between that and the actor is only
that the actor's component - and its FSceneViewState, frame counter, histories - persists across
frames. In an on-demand scene capture the temporally accumulated effects re-converge every frame
from a history that does not match, and the residue is the shimmer.

The fix, `HistoryMode 3`: a fresh `USceneCaptureComponent2D` per frame, `NewObject` with the old
one as template (every UPROPERTY carried over: target, source, FOV, the exposure and local
exposure settings), attached, registered, old one destroyed. Hwaseong, same route:

| | persistent (all recordings so far) | fresh per frame |
|---|---|---|
| static-frame diff median / p90 | 1.79 / 2.54 | 0.15 / 0.35 |
| sparkle pixels | 0.40% | 0.001% |
| sharpness (Laplacian var) | 866 | 880 |
| mean brightness / near-black | 136 / 8.7% | 139 / 8.2% |
| same-pose repeat diff | 0.78 | 0.15 |
| moving frame-to-frame brightness jump p90 | 0.45 | 0.39 |
| in-engine fps | 15.0 | 11.8 |

The same-pose column is a free consequence: a fresh view state also means the level's
auto-exposure meters each frame from nothing, so it is instant and path-independent by
construction - the whole exposure investigation's goal, without pinning anything. Template now
`history_mode 3`, `exposure auto`, fill auto, local exposure shadow 0.65. Cost 20% capture speed,
from allocating a view state per frame; a two-component ping-pong would recover most of it and
was not tried.

Rule that would have saved the day: when a probe and the production path disagree, list what the
production path KEEPS between calls and remove those one at a time; the cvars were guesses about
the renderer, the persistent state was a fact about our own code.

## Moving-camera shimmer is aliasing, not the same thing as the static shimmer, 15 Sep

The fresh-view-state fix (previous entry) took the static-camera shimmer to 0.19 and the user
saw the recordings still flicker while walking. Right: with a fresh state per frame there is no
temporal anti-aliasing at all, and high-frequency texture (brick joints, roof tiles) crawls as
the camera moves. The static metric cannot see it. New metric (`motion_shimmer.py`): on moving
frames, warp frame t+1 back onto frame t with dense optical flow and measure the residual - real
motion is compensated, crawl and noise are not. Original 3.57 mean / 2.05% sparkle; fresh state
alone 3.52 / 1.81% - as bad, the user's eye was right.

Fix: supersampling. The RGB target renders at Width*N x Height*N and is box-filtered to the
delivered size in the actor before encoding; depth stays native (a filtered depth edge is a
depth that exists nowhere). Deterministic, no history, composes with the fresh state.

| Hwaseong | original | fresh 1x | fresh 2x | fresh 3x |
|---|---|---|---|---|
| motion residual mean | 3.57 | 3.52 | 2.59 | 2.34 |
| motion sparkle | 2.05% | 1.81% | 0.72% | 0.50% |
| static-frame diff | 1.79 | 0.20 | 0.19 | 0.19 |
| sharpness | 1209 | 1165 | 1029 | 1052 |
| in-engine fps | 15 | 12 | 10 | 8 |

Template `supersample 2`. Not zero: sub-third-pixel texture still aliases, and the residual
median (1.33 in every row) is the optical-flow floor, not shimmer.

Two mistakes in getting here, both caught by looking at frames rather than numbers:

- The first supersampled recordings reported a residual of 0.00 - and were three quarters
  black. `ReadTargets` sized and copied the RGB request with the batch's 1280x720 while the
  target was 2560x1440, so the top quarter came back tiled. A per-request width/height on the
  readback request fixed it. **A metric that suddenly reads perfect is a metric that stopped
  measuring; open the frame.**
- The static metric said "fixed" and it was, for what it measured. One number per phenomenon:
  static shimmer, motion crawl, exposure drift, path dependence each have their own now.

ChemicalPlant_2 with the same defaults + 2x (15 Sep, 18:00): motion residual 5.56 -> 4.48 (original
4.91), motion sparkle 5.55% -> 4.00%, static 1.69 -> 1.43, near-black 7.4% -> 6.0%, 9.6 fps. Far
less than Hwaseong's gain, and the residual sits at 10-40 m (6.4) and beyond (5.7), not on the
near walls (3.3): pipe racks, railings and gratings are sub-pixel THIN GEOMETRY, which a 2x box
filter only halves, plus the height fog's own per-frame grain in the far band and the sky (2.8).
Big textures alias at the pixel scale and 2x cures them; thin geometry aliases below it and
needs 3-4x or a real temporal accumulation. Left at 2x; the map is flagged, not fixed.
4x on ChemicalPlant_2 (5120x2880 -> 1280x720, 5.7 fps): motion residual 3.97, sparkle 2.90%,
static 1.36, near-black 5.2%, sharpness back to 856. Monotone with the factor (5.56 / 4.48 /
3.97 for 1x / 2x / 4x) and still short of Hwaseong's 2.6 at 2x: thin geometry keeps aliasing
below a quarter pixel, and the fog grain is untouched. The cost curve is the whole story -
12.6 / 9.6 / 5.7 fps - so the factor is a per-map choice against recording time, not a fix.

## Summary written, 16 Sep

`RENDER_QUALITY.md` collects every exposure, lighting and shimmer scheme tried on 14–16 Sep with
its numbers and verdict, and the template defaults they produced. This file keeps the narrative.

## 16 Sep — moving-camera shimmer: a disabled capture flag, not a failed TSR algorithm

UE 5.8 SceneCapture's constructor sets `TemporalAA=false`. `SceneView::SetupAntiAliasingMethod`
then demotes TAA/TSR to FXAA. Earlier cvar-only experiments did not activate temporal AA, and
mode 3 threw away the history each frame. The earlier conclusion that TSR had been ruled out
was wrong. New mode 4 explicitly enables the show flag, persists history, renders 32 genuine
warmup frames, and records its runtime settings. RGB remains 2x; depth is untouched.

Two aligned 876-frame routes now show 60.5% (Hwaseong) / 61.4% (Chemical) less motion sparkle.
Full numbers, static-noise regression on Hwaseong, depth pixel differences, alternatives and
reproduction details are in `RENDER_QUALITY.md` section 8. `tasks/longvideo_template.json` now
selects mode 4 + TSR + 2x + auto_instant locally; no fleet restart, upload or old-video rewrite.
The native module built successfully and all eight short captures finished without write
failures. Results and preserved comparisons: `/longvideo/flicker-fix/` on port 8500.

## 16 Sep — auto-fill calibration must not become an automatic-exposure offset

While running the latest profile on Downtown West and ChemicalPlant 2, found that
`capture_engine.py` unconditionally copied the manual calibration sweep's `chosen_ev` into
`EXPOSURE_BIAS_EV`. The new `auto_instant` mode then applied that value as an automatic
exposure bias. A manual camera compensation of +11.5 EV is not an auto-metering offset.
The calibrated manual bias now stays local and is consumed only in manual mode when no
explicit task/environment bias is supplied. No environment mutation means it cannot leak into
a later capture. Five capture-entrypoint regression cases verify manual calibration, subsequent
auto mode, explicit task bias in both modes, and an explicit environment override.
Artifacts and the test are under `results/latest_maps_2026-09-16/`; original files and startup
attempt logs remain in `/home/ubuntu/ue_latest_two_maps_20260916_0639/`.


### 2026-09-16 — ChemicalPlant left far tower, targeted anti-flicker follow-up

User localized the remaining issue to the opening seconds, left tower (~48–60 m).
Added optional `tasks/chemical_far_stable_template.json`: mode 4 + TAA (`r.AntiAliasingMethod 2`)
+ 2x RGB; global TSR default unchanged. Local first-8-second flash-pixel proxy fell 47–50%
in short/final captures and a repeated TSR control. Fine wall detail is visibly softer;
static raw frame difference did not improve. Do not claim zero flicker or a new asset fix.
TSR cadence/thin/history tuning, 3x/4x sampling, disabling fog/reflections did not resolve
this target. Fog interacts with TSR anti-flicker diagnostics, but removing fog worsened
results, so it remains enabled. All unsuccessful trials retained.

A new 60-second native replay has 1440 RGB/depth/pose records, zero write failures and
position/wrapped-angle error, mode 4, 32 warmups and 2560x1440 RGB. Browser playback,
paired crops, seek/mobile checks and old galleries passed. Details and limitations:
`RENDER_QUALITY.md`; compact reports `results/chemical_far_2026-09-16/`; raw artifacts
`/home/ubuntu/ue_chemical_far_20260916/`; web `/longvideo/chemical-far/` on port 8500.


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


## 2026-09-16: three additional maps, unchanged TAA candidate

MiddleEast, WinterTown and ContainerYard: six 25-second TSR/TAA paired clips completed.
New evidence retains tradeoffs: Winter turn residual +36.6%, Container wall edge -7.7%,
while MiddleEast local flash -20.6%. Global default unchanged; no all-map guarantee.
See RENDER_QUALITY.md, results/general_more_2026-09-16/ and gallery
/longvideo/general-stability-more/ on 8500. All old media remain intact.


## 2026-09-16：86 张路线审查完成

73 张已查看路线/草案俯视图；13 张核对输入问题。新增 59 张离线草案，未启动 UE 渲染或碰撞验证。
30 张优先检查补区域，34 张草案待引擎验证，6 张可沿用主体后预录验收，3 张旧预检失败，13 张修起点/导航。
逐图意见与证据见 `results/route_audit_2026-09-16/` 和 8500 的 `/longvideo/route-audit/`。
旧覆盖率只针对选定/裁剪后的网络，不能证明全图或可见立面覆盖；不要直接开跑所有旧路线。


## 2026-09-16：86 图验证的启动就绪与超时修正

化工厂在初始 UnrealCV 请求触发 90 秒 alarm。旧 runner 等端口后固定睡 12 秒，不能代表 PIE 世界已准备好；握手又处于异常结果捕获之外。新版等本次原生 UE 日志的 PIE 完成标记（stdout 实测缓冲会停在半行），再建立唯一连接；同步 unrealcv.request 库实现没有使用正数 timeout，故新增真实 SIGALRM 请求截止时间。超时标注指令、存档该轮、关闭 editor 后限次重启，禁止在活会话重连。重跑化工厂 40.1 秒就绪，单连接在 PIE 完成后建立，世界/cvars/导航导出已成功，完整路线尚在验证。细节和阻塞调用注入测试见 `results/route_validation_2026-09-16/`。


## 2026-09-16：10 台并行验证已部署

用户授权后复用 10 台已停止的 g6.4xlarge，在 eu-north-1a 容量不足时改用 1b 的 4 台同配置闲置实例，其他资源未动。首台 cloud-init 无稳定登录 session，默认 `/run/user/1000` 导致 enroot exec Permission denied；显式导出固定 ENROOT_RUNTIME_PATH 后恢复，其余机器使用修正包。原地图/媒体保留，源码和失败尝试存档。10 台编译成功、实际 UE 世界校验成功、每台仅一个 editor，代码和模板哈希相同。83 图分组，新输出使用独立 S3 前缀，由控制端汇总到 8500 网页；任务完成自动 stop 而非 terminate。细节和验证清单见 `results/route_fleet_2026-09-16/`。


## 2026-09-16：86 图第一遍结果分类与第二遍重跑

第一遍 54 张未过。逐张读 `depth_probe` / `collision` / `kept_fraction`，不是按状态名分：

- **10 台车队全部按 6 cm 离地间隙跑了**，虽然清单上 15 张已改成 45 cm——`assignments.json` 是
  10:35 切的，清单 10:36 才改。以后先改清单、再切分片、再打包，并在包里 grep 核对。
- 29 张（coverage_review 全部 + collision_free 失败 + 探针 clamped）→ 45 cm 重跑。之前实测
  Hwaseong 22%→99%、TemplePlaza 35%→99%。
- 10 张 `RpcTimeout`/起点未投影 → 原样重跑；是连接后编辑器卡死，不是地图问题。
- 3 张 7200 s 超时（Arctic、Mountains_LevelDesign、WinterTown02）仍在推进 → 上限放到 14400 s。
- **"全天空"深度探针在室内地图上出现在平视帧，是选错楼层，不是天空。** 本机把这些帧渲染出来看：
  Dungeon 的最长区域是地牢屋顶（z 400–500 带 3935 m²，地面带 −100..0 只有 3693 m²），相机在
  黑色岩体里，脚下地面在 6.8 m 之下；CommandCenter 的 content_region（z 480，核心 69 m²）是
  楼顶，看到的是天空和沙漠；Factory 的 1248 m² 区域是带天窗的厂房屋顶，室内导航网只有 90 m²；
  SwimmingPool Demonstration_Master 在导航区域 z 660 处每帧都是白色虚空、1000 m 内无深度。
  处理：worker 增加每图 `region_z_cm`，把中位高度不在 ±150 cm 内的区域先剔掉再交给原策略；
  Dungeon 用 −50、CommandCenter 用 50 本机重跑；Factory、SwimmingPool_Master 放弃并写明原因。
- 12 张放弃：`action_mix_in_band` 的小图（HotelCorridor、OperatingRoom、HoldingCells A/B、
  Wild_West、Chinese_Landscape）、保留率 <10% 的 AncientRuins 与 Real_Landscape、起点无地面
  的 Traditional_Map、未挂载的 Hangar，加上面两张。
- 深度探针的 all-sky 否决按 200 m 量程算；这四张在 1000 m 量程下同样全无效，所以不是量程问题。

第二遍：40 张按上一遍耗时倒序轮转到 9 台已停实例，`supervise.py` 对分片内每张传 `--retry`
（实例上仍有第一遍结果，否则 runner 会跳过）。eu-north-1a/1b 的 g6.4xlarge 容量不足，
首轮只起来 05、06，`rerun/launch_retry.sh` 每 3 分钟重试其余。收集器因一个 246 字符的
frozen 文件名 `ENAMETOOLONG` 反复抛异常、`fleet.json` 停在 12:42 而下载仍在继续——页面上的
"12 排队 3 运行" 是这个假象；已改为跳过并记日志。
计划与探针帧：`/home/ubuntu/ue_route_fleet_20260916/rerun/`（`rerun_plan.json`、`sky/<slug>/`）。


## 2026-09-16：新交付 87 个项目里，哪些关卡还没统计

`~/new_map/`（444 GB，S3 `SimWorld/new_map/`）的验收清单列了 87 个项目 442 张已加载检查的关卡。
和 86 张目录对齐：86 张全在其中，另外 356 张没统计过——但其中 280 张是 Overview / Showcase /
Assets / 子关卡 / 套件零件（Dubai 的 58 个 Kit 零件、Lyra 22 张、看台碎块 13 张、维多利亚城 40 个
Zone），不是场景。按名字过滤后剩 76 张，再去掉单栋房子、素材图、已放弃地图的变体，定 55 张。
其中 30 张所在资源包 gym_citynav 已经有；25 张要从交付里拷 Content 子目录（14 个包，合计约 45 GB）。
Dubai（64 GB，需 Cesium 插件）单独处理，没排进去。

做法是把 86 张的路线验证整套复制成 `~/ue_newmap_validation_20260916/` + `~/ue_newmap_fleet_20260916/`：
- 新地图没有起点文件，worker 用 `recover_start`（PlayerStart → 目录起点 → 大网格顶面），起点目录用空桩。
- `policy: content_region`，这样 `core_m2` 也一起量出来；离地间隙 45 cm。
- boot.sh 在编译模块前按本机分片的 `packs_to_sync` 用 `aws s3 sync` 把缺的包拷进容器
  `Content/`（走 `/proc/<pid>/root`，再 chown 给容器用户）。只拷缺的顶层目录；`Meshes`、
  `Materials`、`ThirdPerson` 这类通用名一律不拷，会污染项目根目录。实测 12 GB 45 s。
- 需要同一个包的地图排在同一台机，避免重复下载；其余轮转均衡到 8 台 ue-lv-nav-6..13。
页面 `/longvideo/new-map-validation/`。


## 2026-09-17：42 张补测的拉取队列，以及第一轮全军覆没的四个原因

没录过视频、核心大小或路线信息不全的 42 张，用 `ami-0056a81f740e03d99`（UE5.8_all_map）开 10 台
g6.4xlarge 补测。第一轮 42 张全失败，原因全在工具侧，没有一张是地图的问题：

- **25 张 `float(None)`**。任务表里把 `pitch_limit_up_deg` 写成显式 null，而 worker 里是
  `float(mc.get(键, 默认))`——键存在且值为 null 时 `.get` 的默认值根本不触发。**生成清单时宁可
  不写这个键，也不要写 null**；读取端一律用 `or 默认` 而不是 `.get(键, 默认)`。
- **13 张假的"编辑器卡死"**。首次 `vget /unrealcv/status` 的 180 秒上限，在镜像从未打开过的地图上
  会撞上着色器编译：本机复现时编辑器正以 400% CPU 编译，端口早就在监听了。首次握手单独放宽到
  1500 秒，后续请求仍是 180 秒。**端口能连不等于游戏线程有空。**
- **4 张关卡加载成空的 Untitled 世界**。镜像里这几个包是残缺的，缺的正是要用的 .umap
  （Hangar、Stadium Demo_LVL、ModularBuildingSet、维多利亚城合成图），交付目录里都在。
  加 `force_sync`：包已存在也重新 `s3 sync` 一遍，只补差异，很便宜。
- **编辑器在容器里崩了，宿主机那层 `enroot exec` 还活着**，于是 `editor.poll()` 一直是 None，
  要等 RPC 超时才发现。改成直接查 `pgrep -x UnrealEditor`，崩了立刻换新编辑器重试。

引导阶段另外三个坑：`enroot exec` 默认用 `/run/user/$UID`，cloud-init 下不存在，**必须在启动容器
之前**导出 `ENROOT_RUNTIME_PATH`；校验"模块确实重编了"要按产物找（插件链接到
`Plugins/unrealcv/Binaries`，不是项目的 `Binaries`），`Result: Succeeded` 不能证明这个模块被编译；
镜像自带的 botocore 太老、`put_object` 不认 `IfNoneMatch` 参数，改用 botocore 事件在
`before-sign` 注入 HTTP 头，任何 SDK 版本都能做原子认领。

**队列设计**：任务表按预计耗时从长到短排在 S3，每台机器空闲就扫表、用 S3 条件写原子认领第一个
没人要的任务，没有静态分片。这就是 LPT：大图先开工、小图填空隙，十台一起收工；认领带心跳，
某台掉线 30 分钟后任务被别人接管。没有用 Ray——十台机器上再拴 Python 版本和 head 节点的依赖链，
换来的调度能力这里用不上，而队列没有 head 也能自己跑完。

**核心大小放在 finally 里算**：路线失败的图照样从这一轮的 navmesh 量出核心，这正是之前 8 张
"有路线结果、没核心大小"的成因。第一轮虽然全失败，仍然补回了 24 张的核心数字。
UnrealCV 颜色表补丁（32→64、越界取模）每台开机重打并重编，约 120 秒。
计划、清单与结果：`~/ue_newroute_20260916/`、`~/ue_newroute_fleet_20260916/`，页面
`/longvideo/route-queue/`（实时）与 `/longvideo/inventory/`（总表）。


## 2026-09-17：新动作「转身折返」——掉头、沿原路正向走一段、再转回来

用户要求的动作：转 180 度、往回走一段时间、再转回来继续往前。这和已有的 `backward` 是两回事——
`backward` 是脸朝原方向、身体倒着走，画面还是已经看过的那一面；折返是把刚走过的路**用相反的视角**
再看一遍。对空间记忆数据来说这才是「重访」：一个地方只有被再次**看到**、并且是从别处看到，才算重访。

实现在 `coverage.py`：新原语 `_retrace()`，两条腿都标 `forward`（相机朝着自己的运动方向，被下达的
动作就是前进），中间两次约 180 度的转向标 `turn_left/turn_right`。任务里用 `retrace` 块声明节奏，
`freeze_coverage.py` 增加第八道预检门槛 `retrace_cadence_ok`。默认：中位数 300 s、抖动 ±25%、
上限 420 s、回走 10–25 s。

实测（Cigar_room，3 个种子 31 个间隔）：中位数 305.7 s、均值 307.7 s、范围 235–370 s，
回走最短 11.2 s，掉头角度每次 180 度。

过程中修掉的四个自己的错：

- **下界会把所有间隔都吸到下界上。** 先写成「不早于 150 s、不晚于 180 s」，结果每个间隔都贴着
  150–178 s。改成每次事件后抽一个目标间隔（中位数 + 抖动），节奏才真正围绕中位数分布。
- **判定点必须在走这一段之前，并且要算上这一段的代价。** 放在走完之后判，间隔必然超出一整段加上
  它的看/扫尾巴——实测 181–203 s 对 180 s 的要求。
- **估算一段耗时要用速度区间的中值，不能用最慢值。** 用最慢值把耗时高估三倍，`due` 在 20 s 就越过
  阈值，于是每 80 s 折返一次。
- **`turn_deg` 一度是在「转回来之后」才计算的**，量到的是折返前后路线方向的变化（中位数 21 度），
  不是掉头角度。掉头其实一直是 180 度：`_heading(s, +1)` 和 `_heading(s, -1)` 是同一段 1.4 m 弦的
  两个读法，按构造精确相反。顺带删掉了据此写的「挑直路折返」逻辑——它永远不会拒绝，是死代码。

另外：把中位数设成 0 本应关闭这个动作，但关闭判据当时读的是 `max_interval_s`，而只覆盖中位数的任务
会保留它的默认值，于是目标间隔为 0、每段都触发（半小时 24 次）。判据改读中位数，并在触发处再挡一次。

对照实验：关掉折返后动作配比同样出带（抬头 0.2%），所以配比问题是这张图 1208 m 路网塞进 30 分钟
的性质，不是这个动作引入的。折返每次约给路线增加两倍回走距离（默认约 2×15 m），按 5 分钟一次算，
对覆盖预算的影响在 5% 量级。


## 2026-09-17：抗锯齿全局切到 TAA 2×，用户拍板

用户在看过取舍后选了 TAA（"用TAA呀"）：闪动更少，锐度代理低约 23–36%。这是对 16 Sep 测量的
取舍决定，不是新测量。做法是把 `stability_detail_candidate.json` 的 render 块原样搬进
`tasks/longvideo_template.json`——所有入口（`gen_task.py`、`preflight_one.py`、`probe_matrix.py`、
两个 dryrun、`new_default_record.py`）都写死读这一份，所以不需要加模板选择，改一处即全局生效。
旧的 TSR 2× 原样留在 `tasks/tsr_2x_previous_default.json`，回退要写进批次清单。
`render.taa` 字段仍然只是标签，决定抗锯齿的是 `render.cvars`。`capture_summary.json` 里记录的是
实际执行过的 cvars，判断某集用的是哪种抗锯齿以它为准，不以录制日期为准。
已同步到部署目录和 testUE 镜像。


## 2026-09-17：步速钉死 1 m/s、转速统一 45°/s——并由此发现走了两个月的弧长错位

用户要求：步速固定 1 m/s；相机上下左右转和回正都固定 45°/s。

转速这边其实已经是了：`_pitch_to` 和 `turn()` 共用 `_rotation_profile`，`turn_profile: constant` 下每帧
精确转 45/24 度，最后一帧取余数。这次只是把它写成事实：计划和冻结摘要里新增 `pitch_deg_per_s`。
走弯路时的航向变化是跟路，速率在 45 以下（中位数 8°/s，最大 44.99）；拐角超过速率时停下来转。

步速新增两个任务字段，和 `yaw_deg_per_s` / `turn_profile` 同构：
- `speed_m_s: 1.0`——所有行走原语（前进、倒退、后退小段、折返两条腿）都用这一个速度，覆盖风格在
  档内的位置、倒退折扣和调参器的 speed 旋钮（钉住时调参器不再动这个旋钮，剩五个）。`speed_tier`
  保留给 `speed_within_tier` 门槛。
- `speed_profile: constant`——每个行走帧精确走 100/24 cm，段末一帧取余数，不再有 0.5 s 加减速斜坡。
  旧的 `trapezoid` 仍是默认，老任务行为不变。

**第一次实测不对：中位数 1.000 但 p5 0.36、最大 1.43 m/s。** 根因在 `prepare_walks`：路径先按 20 cm
等距重采样得到弧长轴 `s_axis`，再对点做 7 点滑动平均，**轴没有重算**。于是"沿 s 走 4.17 cm"落到平滑后
的折线上，弦长在 0.04–1.43 倍之间摆（ModularCourtyard 上轴比实际路径长 9%，折返的死胡同被平滑折叠后
相机几乎原地不动）。这个错位从有平滑那天起就在，之前没人发现是因为速度本来就是每段随机抽的——
没有一个"应该是多少"可以对。修法：平滑后再重采样一次，轴就是真正走的那条线的弧长。

修后 ModularCourtyard 两个种子各 30 分钟：行走帧 96.8% 在 1 m/s 的 ±1% 内，最大恰为 1.000，
均值 0.9875；2.2% 是段末余数帧，按构造存在。转向帧 94–95% 精确 45°/s，其余是转角余数帧；抬头低头同。
八道预检门槛全过，折返节奏正常。副作用：同一种子的路线几何和以前略有不同（轴变了），已冻结的路线
不受影响，它们存的是位姿。

验收端 `package.py` 新增门槛 `speed_pinned_held`：任务钉了速度时，用引擎实测的行走帧速度判
中位数与 p95 都在钉值 ±2% 内——判的是引擎有没有把相机放到计划说的位置，不是计划本身。


## 2026-09-17：每集改成「每张地图走两遍」，不封顶不切片

用户决定去掉「8 遍或 20 h 封顶、5 h 分片」的规则：每张地图一集，走两遍就结束。模板 `target_passes` 8→2，
`max_duration_s` 72000→0（规划时不设上限）；`gen_shards.py` 改成每图一片、不封顶，写进分片表的
`max_duration_s` 是两遍估计的 3 倍，只作失控保护——Tokyo 实测相邻两遍交替为巡游的 ~4× 和 ~8×，第二遍
通常比第一遍长一倍，所以两遍可能到一遍估计的 3×。两遍走法不同：第一遍覆盖遍（三风格轮换、配速保覆盖），第二遍填充遍（整遍 study、动作密度加倍、从第一遍终点重抽一次覆盖走法），第二遍约 2× 第一遍，所以成片按一遍估计的 3× 估（用户指出两遍模式不同，文档据此改正）。85 张可录关卡：成片约 335 h，机时约 838 h。
路线验证队列不受影响（它一直只跑 1 遍）。


## 2026-09-17：20 台重新验证队列，开跑一小时暴露的四件事

52 张（重跑 41、未验证 6、修路线 5）用拉取队列在 20 台上跑，镜像 `UE5.8_all_map`。开跑一小时：

- **清单复制时丢了 `packs_to_sync`。** 09-16 的 prepare.py 在 project_dir 为空时把包清空，我照抄了它的条目，
  CityPark、Sunset、LVL_Stadium 三张关卡文件不在镜像里，加载成 Untitled 空世界。修法：清单里每条都从交付
  清单补 project_dir，包名从关卡路径顶层目录取；Factory Collection 的资产全在通用目录（Maps/Meshes/…），
  合不进 gym_citynav，从队列拿掉，要单独立工程。
- **折返节奏门槛在短路线上必然失败。** 它要求至少一次折返，一遍验证的小图路线只有 15 秒。改成路线短于
  上限就不欠事件，间隔少于 4 个不判中位数。
- **认领心跳只在阶段切换时更新。** 跑 30 分钟以上的图被别的机器当成掉线偷走，同一张图两台机器一起跑，
  结果互相覆盖，机时白扔。当场给 20 台各起一个每 4 分钟刷新认领的旁路进程；`worker_loop.py` 加了
  `Pulse` 线程供以后的批次用。教训：**心跳必须是定时的，不能挂在事件上。**
- **首个请求在游戏线程忙时会被吞掉，且不可恢复**（不能重连）。09-16 的"等日志安静 45 秒再连"上限 900 秒，
  CityPark（465 MB）和 Stadium 在 PIE 之后还要编译着色器 10 分钟以上，900 秒到了就连，请求丢了，然后等满
  1500 秒被杀。runone/mapworker 现在读任务里的 `quiet_max_s` / `first_rpc_s`，重图给 2400 秒。Stadium 两次
  都是连上后 25 分钟一行日志没有，更像 PIE 后被蓝图占死，等同包的 Demo_LVL 结果再定。

顺带看清了一种失败模式：**TinyFields 胶囊 0 次碰撞、近平面 394 帧穿透**，庄稼挡视线不挡胶囊。剪枝用的
碰撞通道看不见植被，抬间隙没用。候选修法是剪枝时沿路在眼高加可见性射线；先记下，看队列里还有几张同类。


## 2026-09-17：只录有东西的地方——`content_buffer_m`

用户："有些地图给的很大，其实只有中间一小部分有东西。"之前放弃的 `confine_to_core` 是硬性只走核心内部，
走廊太窄要放宽间隙。这次做温和的版本：在细化中心线之前，把离"内容"超过 N 米的可走面去掉，荒地上的路
不进路网；区域照旧按中心线长度选，但长度是剪完后量的，空旷大区域自然输掉。

"内容"的定义试了两种，10 m 缓冲，五张缓存导航网：

    地图                      无缓冲    dense 10 m   buildings 10 m
    Tokyo                     1144 m    1003 m       979 m     （全是街道，几乎不剪）
    ForestGasStation          1638 m     585 m       532 m     （只剩加油站一圈）
    Mountains_Map_LevelDesign 4619 m    1282 m       317 m     （森林蜂窝：树算不算内容差 4 倍）
    Arctic                    3617 m    2892 m      1554 m     （岩石地形同理）
    GooseLand                  957 m     775 m       535 m

第一版用"任何 ≥2 m² 的障碍"当内容，Mountains 只剪到 3437 m——每棵树都是障碍，森林蜂窝几乎全保留。
改成 survey_core 的密度核心（12 m 内障碍占比 ≥6%）加 ≥20 m² 的footprint（dense），或只算footprint
（buildings）。模板默认 dense / 10 m；森林、岩石算不算"有东西"由用户定 content_mode。
跑着的验证队列没有换代码，它量的是无缓冲的路网；录制冻结时才按缓冲剪，规划页的路网数字另用离线批量算。


## 2026-09-17：第一批实录（6 张 5–10 分钟图，两台机器）——三个问题

- **Storage House 通过**：22,340 帧 15.5 min，两遍 7,226 / 13,279 帧（1.84×），配比全在带内，引擎 14.6 fps。
  **存储 8.66 GB，45,153 个文件**：每帧 JPEG q92 + EXR f16 约 380 KB，**33.5 GB / 成片小时**。规划页原先按
  1.5 GB/h 算，错了 20 倍；100 张两遍 362 h 是 12 TB。
- **Courtyard 被拒**，两条都是边缘：2/27,127 帧深度全无效（庭院抬头纯天空，每像素 −1 正是格式规定的），
  左右转 15.4% / 15.5% 对 15% 上限，停留 3.4% 对 4% 下限。验收改成：全无效帧 ≤0.5% 记录不拒；配比带每边
  容 1 个百分点。数据完整地在 `_rejected/` 里，没丢。
- **SICKA Interior2 失败在补光**：给场景做天光捕获时游戏线程忙了 5 分钟，`apply_fill` 的 300 s 超时把它判死。
  超时放到 1200 s 重排。
- **开机脚本没覆盖镜像里的空起点桩**（Courtyard 第一次：`{name:"", 0,0,0}`），改成一律覆盖。
- 顺带：`capture_summary.json` 没有带 `spec_version`（只在 trajectory.json 的 task 里），下一版补。

- **补光（`lighting: fill`）在室内图上把编辑器卡死**：Cave 和 SICKA Interior2 都是执行天光重捕获的那条
  Python 之后日志戛然而止，1200 s 也不回。之前用过 fill 的全是室外图。分片表新增逐图 `lighting` 覆盖
  （`gen_task.py` 读），这两张改为 `level`（保留作者灯光，不动天光）。室内图用 fill 的问题下一版查根因。
- **队列"空了"的判断在我手动重排时会误判**：worker 00 在我删掉失败结果之前扫了一遍清单，认为没活了就关机；
  重排要在机器还活着时做，或者给循环加"再等 10 分钟"的空转期。


## 2026-09-17 深夜：第二批（30 台、97 张、两遍不封顶）开跑后的四个坑

- **"验证机"按大图先领拿走了最大的两张**（松林 56 h、Arctic 49 h），两天出不了结果，验证不了任何东西。以后验证机
  要单独喂小图。
- **看门狗 45 分钟无输出就杀**：大图的配比调参器一轮一个多小时没输出，Arctic 在冻结阶段被杀两次进死循环。
  `driver.sh` 默认 SILENCE_S 2700 → 14400。
- **目录里的老起点投影不到导航网**（RainMap、NorthenIsle、CastleRiver）：验证阶段有起点恢复兜底，录制这条路没有。
  改成所有图用验证通过路线的第一帧当起点（95 张），`pick_spawn` 加 PlayerStart 兜底。
- **`speed_pinned_held` 用的是三维步长**：frames.csv 的 `speed_m_s` 含 z，坡地加 60 cm/s 的竖向跟随，p95 正好
  sqrt(1+0.36)=1.166 m/s，CastleRiver、SnowMap 平面中位数 1.000 却被拒。门槛改为逐帧平面速度；两集离线按平面
  重判并搬出 `_rejected/`。**钉住的是平面步速，验它就得量平面。**

另外：补光校准找不到可用倍率（ContainerYard、TokyoNight）和补光卡死（柬埔寨、Urban District）都自动追加
"作者灯光"任务；路线被拒自动换种子一次；每张最多处理两次。清单变更靠每台机器上的 `table_sync` 单元同步到本地
分片表，否则 `gen_task.py` 找不到新加的任务。

- **配比门槛在 1 m/s 下系统性出带**：Palace 转向 17.7/17.5%，NorthenIsle 19.0/18.1%，TrainStation 16.3%，带上限 15%。
  走得快了前进帧少，其它动作占比全体上浮，带是按 0.7 m/s 时代定的。验收改成只报告（`warn`），三集离线重判通过。
- **重复帧门槛把停留帧当成重复**：Sci-Fi Base 8 对"近似相同"的相邻帧全是 hold，TAA 收敛后静止画面本来就一样。
  改成只在相机被命令移动的帧对上判。空白帧改比例（≤0.5% 抽样帧只记录）。

## 2026-09-18：录后验收改为只记录

用户："录制的都需要，不要后面的验收。"`package.py` 的 `accepted` 恒为 true，`verdict_mode: advisory`，没过的门槛写在
`gates_would_have_failed`；uploader 不再分 `_rejected/`。已分到 `_rejected/` 的集全部搬回正常目录并改写 acceptance.json。
录前门槛（碰撞、深度探针、路网连通）不变，那是拒绝录一条坏路线，不是丢数据。
