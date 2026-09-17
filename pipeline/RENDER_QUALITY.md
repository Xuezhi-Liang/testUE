# Render quality ledger: exposure, lighting, flicker (14–16 Sep 2026)

Everything tried to make the delivered RGB frames (a) free of black and blown regions, (b) the
same at the same place regardless of where the camera came from, and (c) free of frame-to-frame
shimmer — with the measurements that decided each one. The detailed narrative, including the
wrong turns, is in `FINDINGS.md` (entries dated 14–16 Sep); this file is the summary. Pages with
the actual frames and videos: `/longvideo/exposure/`, `/longvideo/lighting/`, `/longvideo/fill/`,
`/longvideo/calib/`, `/longvideo/flicker/` on the 8500 site (`local_run/build_*_page.py`).

> **16 Sep update:** section 8 corrects the prior TSR diagnosis and documents the new local
> motion-shimmer profile. Earlier fleet videos and S3 archives retain their old configuration.

## 1. The problem as found

- Every recording since the first fleet episode declared `render.exposure: "fixed"` and nothing
  applied it: the levels' own **auto-exposure** decided brightness, with a lag of seconds. Same
  pose, different history → different brightness (Tokyo: up to 23.9/255 apart).
- **Black regions**: shadow with no ambient. Purchased levels ship a Static sky light with no
  built lighting, so it contributes nothing; `ensure_dynamic_sky`'s fix (Movable + real-time
  capture) only works on levels that have a SkyAtmosphere — on a painted sky sphere it captures
  black. Downtown's reference near-black was 12% for this reason alone.
- **Shimmer**: static-camera frames differ by ~1.8/255 with sparkle on brick joints, tile edges
  and foliage; moving-camera frames crawl on high-frequency texture. Present in every recording.

## 2. Metrics (all in `local_run/build_*_page.py` and `motion_shimmer.py`)

| metric | what it measures |
|---|---|
| near-black / blown | fraction of pixels with luma ≤5 / ≥250, median over frames and worst frame |
| same-pose repeat diff | mean-luma difference between two visits of one pose (≤60 cm, ≤10°, ≥10 s apart): exposure path dependence, with viewpoint change as its floor |
| static-frame diff | per-pixel mean abs diff of consecutive frames while the camera is still: shimmer at rest |
| motion residual | per-pixel diff after warping frame t+1 onto t with dense optical flow: shimmer while moving (crawl, noise); median ≈1.3 is the flow floor |
| sharpness | Laplacian variance, to see whether a fix blurred the image |
| contrast / p50 ratio | p99−p10 and median grey relative to the level's own render: guards against "fixed" scenes that were simply re-lit |

## 3. Exposure schemes

| scheme | how | Downtown / Tokyo / Chem / Hwaseong results | verdict |
|---|---|---|---|
| A. as shipped (level AE) | nothing | same-pose diff 1.5 / 5.0 / 1.8 / 0.8; near-black 3–14% | the baseline; path-dependent |
| B. pinned manual exposure | `AEM_Manual` + per-map bias on the capture component (WMC pattern). Bias must be swept per map: Downtown needs **+11.5 EV**, bias 0 renders black. Capture refuses `fixed` without a bias | same-pose 0.4 / 2.8 / 0.7 / 0.25; colours unchanged; shadows stay dark | consistent, but the user found the dark shadows worse than AE; kept as option `exposure: fixed` |
| C. Dubai rig | hide the level's sun/sky/fog/clouds/post-process, spawn sun 7.0 / sky 2.5 / haze 0.006, AE off (`r.DefaultFeature.AutoExposure 0`) | near-black 12→8%, blown 0, spread 64→53, p50 164 | **colours wrong** (grade gone, white noon sun under a sunset sky sphere, blue shadows). Rejected; kept as `lighting: rig` |
| D. instant AE | level metering, `AutoExposureSpeedUp/Down = 100` on the capture | same-pose 0.20 / 0.52 / 1.5 / 0.36; brightness ≈ as shipped once the author's bias was left alone (first run overrode it with 0 and was 0.7 stop dark) | removes the lag; near-black unchanged. Superseded by G |
| E. instant AE + local exposure | D + `LocalExposure` bilateral, shadow contrast scale swept 1.0→0.5 per map | near-black Downtown 3.1→2.3%, Tokyo 4.0→2.3%, Chem 14.3→5.2%, Hwaseong 8.7→7.0% | the shadow lift that does not re-light the scene; shadow scale 0.65 (Tokyo, Downtown) or 0.5 (Chem, Hwaseong) |
| F. `auto_instant` mode kept in code | D as a template option | — | option only |
| G. fresh view state per frame (see §5) | AE meters from nothing each frame → instant and path-independent by construction | same-pose 0.21 (Hwaseong), 1.3 (Chem) | **default**: `exposure: auto` + `history_mode 3` |

## 4. Lighting schemes

| scheme | how | result | verdict |
|---|---|---|---|
| ensure_dynamic_sky (old) | level sky light → Movable + real-time capture | no effect on atmosphere-less levels (captures black) | fixed: real-time capture only when a SkyAtmosphere exists, else captured-scene + `RecaptureSky()`; Downtown reference near-black 12.1→3.7% from this alone |
| Dubai rig | see C | colours wrong | rejected |
| sky-light **fill** | hide the level's sky light, spawn ours (C++ `SpawnSkyLight`, Movable, **captured-scene**) at the level's intensity × factor; sun, sky sphere, fog, grade untouched | Downtown ×1/×2/×3 near-black 12.1 → 5.7 / 4.4 / 2.7%, colours unchanged | **default**: `lighting: fill` |
| fill factor **auto** (`lighting_calibrate.py`) | try ×1, 1.5, 2, 3, 4; per factor sweep the exposure bias; first factor meeting the bar wins (near-black ≤3%, blown ≤1%, contrast ≥80% of the level's own, median grey within 0.6–1.6× of it); else least violation, flagged `quality_bar_missed` | Downtown ×3 pass, Tokyo ×2 pass, Hwaseong ×4 pass, Chem missed at every factor (full occlusion under pipe racks), night level refused | **default**: `skylight_factor: auto`; 10–50 s per map. The brightness band was added after the first run "passed" a night level re-lit into day |
| DAYLIGHT mode | brightness target 120 absolute, for night→day | Nighttime x4 / +15.0, still missed | option `DAYLIGHT=1`; night work parked by request |
| local exposure fixed 0.65 | template default; per-map sweep exists in `instant_test.py` but is not yet wired into the capture | Chem would prefer 0.5 | open item |

Things that did not do what they claimed, all caught by rendered frames, not property reads:
`set_editor_property("intensity")` on a light in PIE (use `set_intensity()`); a real-time-capture
sky light on a level without atmosphere (captures black); `ensure_dynamic_sky` retuning our own
spawned light (now skips tagged actors); `EditorLevelLibrary.spawn_actor_from_class` in PIE
(returns None; spawn from C++); changing an existing sky light's intensity after a recording
(respawn instead).

## 5. Shimmer

Ruled out, each by a full recording of the same Hwaseong route: FXAA, TSR parameters,
anti-aliasing off (sharper, still 2.0), Lumen off and ray-traced shadows off (runtime cvars had
no effect on the image — untested, not ruled out), virtual shadow maps off (1.79→1.59, minor),
pinning every temporal jitter sequence (`r.TemporalAASamples 1`, Lumen `FixedJitterIndex`),
`bCameraCutThisFrame` every frame. `bAlwaysPersistRenderingState=false` gave 0.00 — and no GI,
black shadows. The exposure schemes above did not change it either.

Located by a probe: `CaptureRgbPng` re-rendering one pose six times, a new component each time,
differs by 0.00 with GI present. The actor's component persisted across frames; its view state,
frame counter and histories re-converged every frame from a history that never matched.

| Hwaseong | persistent (all recordings before 15 Sep) | fresh component per frame (`history_mode 3`) | + 2× supersampling | + 3× |
|---|---|---|---|---|
| static-frame diff | 1.79 | 0.20 | 0.19 | 0.19 |
| motion residual / sparkle | 3.57 / 2.05% | 3.52 / 1.81% | **2.59 / 0.72%** | 2.34 / 0.50% |
| same-pose repeat diff | 0.78 | 0.21 | 0.21 | — |
| near-black | 8.7% | 1.7% (with fill ×4 + LE) | 1.7% | — |
| sharpness | 866 | 880 | 1029* | 1052* |
| in-engine fps | 15.0 | 11.8 | 9.9 | 8.1 |

*measured on a different frame set; the 2×/3× rows in `/longvideo/flicker/` use the same frame.

Fresh state fixes the static shimmer and makes AE instant, but leaves **no temporal AA at all**:
the moving camera crawls over texture as badly as before. Supersampling (render at N×, box-filter
down, depth untouched) is the deterministic spatial AA that composes with it.

| ChemicalPlant_2 | as shipped | fresh 1× | 2× | 4× |
|---|---|---|---|---|
| motion residual / sparkle | 4.91 / 4.26% | 5.56 / 5.55% | 4.48 / 4.00% | 3.97 / 2.90% |
| static-frame diff | 2.31 | 1.69 | 1.43 | 1.36 |
| near-black | 14.3% | 7.4% | 6.0% | 5.2% |
| fps | 15 | 12.6 | 9.6 | 5.7 |

ChemicalPlant improves monotonically but stays far from Hwaseong: its residual is thin geometry
(pipe racks, railings, gratings at 10–40 m) aliasing below a quarter pixel, plus the height fog's
own per-frame grain (sky band 2.8). The supersample factor is therefore a per-map choice against
recording time, not a fix. The fog grain's source is not yet identified (the diagnostic run was
cut short by an editor wedge).

Wrong turns worth remembering: the first supersampled recording reported a motion residual of
0.00 — and was three quarters black (readback copied a 2560-wide target with the 1280 batch
stride). The first ChemicalPlant 2× run was byte-identical to 1× because the driver did not pass
the supersample setting. **A metric that reads perfect or identical is a metric that stopped
measuring; open the frame.**

## 6. Previous default (superseded locally by section 8) (`tasks/longvideo_template.json`, in the S3 hotfix tarball)

```
render.history_mode      3        fresh capture component per frame (static shimmer, instant AE)
render.supersample       2        RGB at 2560x1440 -> 1280x720; 3-4 on thin-geometry maps
render.exposure          auto     level metering, instant by construction; no per-map bias needed
render.lighting          fill     level's own sun/sky/grade kept; its sky light replaced by ours
render.skylight_factor   auto     per-map, first of x1..x4 meeting the bar, else flagged
render.local_exposure    shadow 0.65, detail 1.0 (per-map sweep exists, not wired)
```

Cost: 15 → ~10 fps in-engine (fresh state −20%, 2× supersampling −20%). Fleet boot rebuilds the
C++ module from the tarball (`boot.sh`, ~20 s incremental).

Options kept: `exposure fixed` (+`exposure_bias_ev`, per-map sweep `exposure_probe.py`), `off`,
`auto_instant`; `lighting rig`, `level`; `history_mode 0/1/2`; `supersample 1..4`; `DAYLIGHT=1`.

## 7. Open

- Wire the local-exposure shadow-scale sweep into `lighting_calibrate` (Chem wants 0.5).
- ChemicalPlant's fog grain: find the source (volumetric fog cvar test was interrupted).
- Night levels: refused by the bar by design; `DAYLIGHT=1` re-lights them, parked.
- Editor wedge after UnrealCV connect (~1 in 10 launches): cause unknown, per-step `timeout` +
  retry everywhere.
- The 108 h already on S3 carry the old exposure, sky light and shimmer.

## 8. 16 Sep correction: the capture never enabled TemporalAA

**This section supersedes the old AA diagnosis and the local defaults in sections 3/5/6.**
Earlier S3 hotfix archives and recorded datasets are unchanged; this is a local source/module
fix, not a claim that the fleet or its old videos have been re-rendered.

Installed UE 5.8 source explains why setting `r.AntiAliasingMethod 4` did not fix the old
capture: `Engine/Source/Runtime/Engine/Private/Components/SceneCaptureComponent.cpp` sets
`ShowFlags.TemporalAA = false` in its constructor. `SceneView.cpp::SetupAntiAliasingMethod`
falls back from TAA/TSR to FXAA when that flag is false. The previous tests therefore did
**not** rule out a working temporal AA path. Mode 3 additionally recreated the view state on
every frame. Waiting ticks alone did not prewarm this manually triggered capture.

`history_mode: 4` now explicitly enables AntiAliasing/TemporalAA on the RGB capture, retains
its view state, disables motion blur, and actually renders 32 warmup frames at pose 0 before
writing any dataset frames. The capture remains manually triggered once per recorded pose.
Depth keeps its separate, native-resolution, non-AA capture and readback path. Modes 0..3
keep their previous behavior for reproducibility. Runtime status and `capture_summary.json`
now report the actual flag, history mode, warmup count and RGB render size; Python refuses
to finalize a mode-4 recording with an old module or missing warmup.

Selected local long-video template: `history_mode: 4`, `supersample: 2`,
`cvars: ["r.AntiAliasingMethod 4"]`, `exposure: "auto_instant"`; the existing fill and local
exposure settings stay in place. Fast metering avoids reintroducing slow adaptation with a
persistent view state; it does **not** guarantee history-independent identical revisit pixels.
The diagnostic routes used fill factors 4 (Hwaseong) and 1 (Chemical), held constant across
variants. The production template still calibrates its own fill factor with `auto`.

### Aligned native capture measurements

Each variant replayed the same first 876 frozen poses (36.5 s, 24 fps), with native JPEG/EXR
outputs. `qa_temporal.py` sampled the same 40 moving pairs (translation OR rotation) and 40
stationary pairs for every variant. These are a different sample definition from the older
ledger; compare columns within this table, not absolute numbers against earlier tables.

| Map / scheme | Motion residual /255 | Motion sparkle % | Static diff /255 | Sharpness | Luma |
|---|---:|---:|---:|---:|---:|
| Hwaseong, mode 3 + 2x | 2.971 | 0.984 | 0.195 | 493.1 | 153.0 |
| Hwaseong, mode 4 TSR + 2x | 2.233 | 0.388 | 0.802 | 541.6 | 150.2 |
| Hwaseong, mode 4 TAA + 2x | 2.031 | 0.221 | 0.817 | 379.9 | 151.1 |
| Chemical, mode 3 + 2x | 5.160 | 4.750 | 1.458 | 1084.3 | 118.9 |
| Chemical, mode 4 TSR + 2x | 3.374 | 1.833 | 1.197 | 1008.7 | 118.5 |
| Chemical, TSR + fixed GI jitter | 3.406 | 1.804 | 1.208 | 1003.1 | 118.6 |
| Chemical, TAA + fixed GI jitter | 2.856 | 1.615 | 1.268 | 890.4 | 118.4 |

The selected TSR profile reduces motion sparkle by **60.5% / 61.4%**. Hwaseong static noise
regresses versus the unusually quiet mode 3; Chemical improves. TAA reduces motion residual
further but loses more detail (Hwaseong sharpness -23%). Fixed Lumen GI jitter did not give a
material win and is **not** enabled by the template. These are motion-shimmer reductions,
not elimination of all flicker, Lumen noise, disocclusion artifacts or temporal ghosting.
The flow metric includes flow-estimation errors; full frames and detail crops were inspected
to catch black readback, brightness changes and missing detail.

All eight short captures completed with 876 native frames and zero write failures. The
variants have exactly matching recorded camera poses. Depth is finite and remains 1280x720.
Twelve depth frames per variant were compared: independently rendered EXRs are not all
bitwise identical. For selected TSR, Hwaseong frame 0 has about 1.09% differing R pixels;
all later sampled frames agree at >=99.998%. Chemical frame 0 agrees at 97.590%; later sampled frames agree at >=99.985%.
Differences are sparse and can have large edge distances, so reporting only the p99 error
would hide them. Cold-start/streaming or raster edge changes are possible causes, not proven
here. Raw per-frame equality, mask agreement, maximum and p99 errors are retained. Do not
claim a depth equivalence proof from these samples.

Artifacts: `/home/ubuntu/ue_flicker_20260916/` (before-edit backups, build log, frozen routes,
per-variant engine status, RGB/depth/states, metric JSON and scripts). Short probes are
render diagnostics, **not** new accepted training episodes; truncated inputs retain source
annotations and their original route hash. The full-route validation has a newly computed
hash and source provenance. Web comparison: `/longvideo/flicker-fix/` on port 8500; old
`/longvideo/flicker/` and all previous galleries remain available.

Reproduce metrics with:

```bash
python qa_temporal.py BASELINE_EP CANDIDATE_EP --out comparison.json --samples 40
```

Use the Python environment with NumPy/OpenCV and EXR support. Rebuild/install the native
module before capturing, restart the editor once per map, and reuse one UnrealCV connection
through all variants. Changing Python/cvars alone does not install this fix.

### Full-route validation of the selected template

A fresh local UE launch replayed all 2,844 original Hwaseong poses (118.5 s), with the new
template render settings and the same documented fill factor 4. Native status confirms mode 4,
TemporalAA enabled, 32 rendered warmup frames and a 2560x1440 RGB target. All 2,844 JPEGs,
2,844 EXRs and pose records were written; position/yaw errors and write failures are zero.
An independent collision-ray check sampled 32 centre pixels: 32 were comparable, all
within 15 cm; maximum absolute difference 1.352 cm. All raw hits and depth readings are kept
in the report. This is a render/depth regression
check, not a new full dataset acceptance claim. Native build and Python syntax checks passed.
Browser checks passed synchronized playback, variant switching, mobile width, range requests
and preservation of old Dubai/flicker URLs. Full video is on the comparison page.

Compact machine-readable reports: `results/temporal_2026-09-16/`. Runtime source copies and
`tasks/longvideo_template.json` were backed up and synchronized to
`/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline`; native implementation was installed
and built in the local `gym_citynav` project. Existing frozen tasks keep their own render
settings; regenerate them from the new template or explicitly opt in to mode 4 for a replay.

### Auto-fill + auto_instant integration correction

The latest two-map run uses the template's actual `skylight_factor: auto`, whereas the earlier
aligned anti-flicker comparison held a numeric factor fixed. Fixed a Python integration bug:
the manual EV calibration result must not become the auto_instant exposure offset. The result
is now local to manual exposure; automatic mode keeps the map's bias unless explicitly
configured otherwise. Manual mode and explicit overrides are preserved (five regression cases).
This changes no TSR, sampling or warmup settings. New videos: `/longvideo/latest-config-20260916/`.

Latest-profile recordings completed on Downtown West and ChemicalPlant 2: 2,844 frames each,
118.5 seconds, zero write failures and tracked pose error. Automatic fill selected x3 / x1.5;
Chemical still missed its near-black calibration threshold and is explicitly flagged. The
manual-EV bridge regression was checked against the native runtime (auto mode, bias offset 0).
Reports and scripts: `results/latest_maps_2026-09-16/`. New videos are published separately
at `/longvideo/latest-config-20260916/`; prior comparisons and videos are preserved.


## 2026-09-16: ChemicalPlant opening left tower — targeted follow-up

The user identified the distant tower on the left during the first seconds. The target
facade is about 48–60 m away in native depth. All probes replay the same camera prefix,
fixed skylight fill 1.5, instant automatic exposure, mode 4, 32 rendered warmup frames,
and 1280x720 output at 24 fps. Existing Dubai, top-view and previous render galleries remain.

An **optional ChemicalPlant TAA 2x preset** is saved as
`tasks/chemical_far_stable_template.json`, also copied to the deployed pipeline. The global
TSR template is unchanged. `r.AntiAliasingMethod 2` selects TAA; RGB renders 2560x1440 and
is reduced to 1280x720; depth is still the separate native, unfiltered capture. Fog, reflections,
geometry, texture mip bias and lighting remain at the baseline values. This profile trades
some fine wall texture detail for better moving-image stability; it does not eliminate flicker.

Measurements use the first 8 seconds, x<500/y<360 intersected with baseline depth 35–80 m,
48 adjacent-frame pairs, identical baseline optical flow and masks across candidates.
Median moving residual includes reprojection error and occlusion, not only flicker. The
Laplacian metric includes noise and is not a perceptual resolution measurement.

| Variant | Moving residual ↓ | Pixels with residual >10 (%) ↓ | >20 (%) ↓ | Static raw difference ↓ | Moving Laplacian variance |
|---|---:|---:|---:|---:|---:|
| TSR 2x baseline | 3.117 | 3.987 | 0.748 | 1.503 | 786.2 |
| TAA 2x (optional stable) | 2.636 | 2.125 | 0.308 | 1.554 | 540.0 |
| TAA 3x | 2.899 | 3.079 | 0.485 | 1.495 | 691.8 |
| TSR mip +1 | 2.887 | 3.739 | 0.688 | 1.437 | 756.3 |
| TSR mip +2 | 2.872 | 3.709 | 0.692 | 1.419 | 724.9 |
| TSR thin/history 32/period 3 | 3.094 | 3.841 | 0.667 | 1.455 | 758.2 |
| TSR 3x | 3.481 | 4.834 | 0.964 | 1.524 | 932.6 |
| TSR 4x | 3.302 | 4.544 | 0.831 | 1.419 | 936.6 |
| TSR rejection samples 8 | 3.105 | 3.824 | 0.705 | 1.507 | 800.5 |

For TAA 2x, moving residual decreases 15.4%, >10 flash pixels 46.7%, >20 flash pixels
58.8%. The sharpness proxy decreases 31.3%; static raw difference increases 3.4%.
TAA 3x retains more detail but suppresses the >10 flash metric by only 22.8%.
These numbers describe this local opening region, not a whole-video or every-map guarantee.

Other diagnostics: disabling volumetric fog or Lumen reflections alone did not help;
disabling all fog changed the look and worsened motion residual. Fixed TSR flicker cadence,
thin-geometry detection, more history/rejection samples and spatial supersampling up to 4x
were not convincing improvements on this wall. Mip bias +1/+2 gave modest residual gains,
with texture softening. Do not promote these diagnostics into production defaults.

Installed UE 5.8 `TSRVisualize.usf` (DisplayFlickeringFramePeriod, around lines 327–386)
shows pink where movement or the difference between pre-translucency luma and scene luma
limits anti-flicker behavior. Visualize 7 painted the target facade pink with fog; it no
longer did so with all fog disabled. This confirms an interaction in the diagnostic, **not**
proof of the sole root cause: removing fog did not improve the measured result. Visualize 0
showed accumulated history on the static tower. No asset/engine shader was modified.

The controlled short captures and the subsequent 60-second replay are render diagnostics,
not accepted training episodes (truncation keeps original route annotations). The actual
runtime AA method is authoritative: probe frozen inputs retained the source descriptive
`taa: tsr`, but explicit cvar 2 selected TAA; the new optional template consistently says taa.

Artifacts: `/home/ubuntu/ue_chemical_far_20260916/`; comparison and 2x nearest-neighbor
crops: `http://51.20.82.218:8500/longvideo/chemical-far/`. The output video has no temporal
postprocessing. Raw RGB, native depth, poses, actual cvars, shader debug captures and all
unsuccessful candidates are preserved. Compact reports/scripts are under
`results/chemical_far_2026-09-16/`.

### Final replay and repeat-control checks

The 60-second TAA 2x replay completed with 1,440 RGB images, 1,440 native EXRs, 1,440
pose records, no write failures, zero position and wrapped-angle error. Runtime confirms
AA method 2, history mode 4, 32 rendered warmups, 2560x1440 RGB. Twelve sampled RGBA
EXRs are finite at 1280x720, R is positive metres or -1 sky. This checks native depth
integrity, not collision equivalence or full dataset acceptance. Browser checks passed
60-second playback/seek, synchronized 8-second crops, switching, mobile width, range
requests and previous Dubai/TSR pages. Capture processes were stopped; port 8500 stays up.

A repeat TSR 2x control in the same editor session confirms the direction of improvement.
On the same opening region, final TAA / repeat TSR: moving residual 2.469 / 3.208,
>10 flash pixels 2.004% / 3.995%, >20 pixels 0.301% / 0.714%. Thus the flash proxy falls
about 50%, with Laplacian variance 501 / 781 (36% lower; softness/noise combined).
Static raw difference is essentially unchanged: 1.519 / 1.517. The initial independent
probe showed the smaller 46.7% flash reduction above. These are repeated rendered runs,
not bit-identical deterministic world-lighting simulations; clouds/lighting/cache history
may differ despite fixed exposure/fill settings. Native captures are preserved for review.


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
| Map | Candidate | Building flash change | Building edge change | Building high-frequency change | Whole-frame walk flash change | Turn residual change | Static difference change | Native fps |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| chemical | taa_balanced | -24.3% | -1.9% | -16.0% | -29.9% | -2.4% | +1.4% | 9.45 |
| chemical | tsr_detail | +6.2% | +4.4% | +9.2% | -15.6% | +2.1% | -11.7% | 7.41 |
| chemical | taa_detail | -9.9% | +3.5% | +2.2% | -26.4% | +0.9% | -7.1% | 7.80 |
| downtown | taa_balanced | -22.5% | -2.1% | -15.6% | -30.2% | -2.7% | -0.6% | 10.22 |
| downtown | tsr_detail | +29.6% | +4.4% | +16.5% | +164.5% | +3.2% | -5.6% | 7.93 |
| downtown | taa_detail | +13.9% | +3.3% | +6.8% | +101.3% | +1.7% | -1.3% | 8.45 |
| hwaseong | taa_balanced | -34.0% | -2.1% | -18.1% | -37.2% | -2.6% | +3.6% | 9.44 |
| hwaseong | tsr_detail | +13.2% | +0.7% | +3.3% | +13.6% | +1.0% | -13.8% | 7.53 |
| hwaseong | taa_detail | -11.1% | -0.2% | -6.0% | -12.2% | -0.1% | -3.6% | 7.89 |
| forest | taa_balanced | -1.0% | +0.0% | -12.6% | +1.1% | -1.6% | -2.6% | 6.92 |
| forest | tsr_detail | -22.1% | -3.4% | -15.3% | -16.9% | -4.0% | -19.4% | 5.43 |
| forest | taa_detail | -6.9% | -2.5% | -14.6% | -15.8% | -3.8% | -12.4% | 5.68 |

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

Final browser verification passed all 12 candidate/map selections against their baselines: 25-second playback, synchronized seek, both pixel-magnification canvases, mobile width, HTTP byte ranges and previous gallery URLs. Chinese UI font is served locally. Candidate task JSONs can be downloaded from the comparison page. See `browser_validation.json`.


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
