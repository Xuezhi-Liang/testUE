# What we take from UE5-Agent-Data, and what it fixes

Read against `/home/ubuntu/UE5-Agent-Data` (WorldModelCollect) on 2026-08-11. That project
collects the same class of data for a world model and has already paid for the debugging behind
several things we are currently blocked on. This file records what transfers, what it costs, and
the two places where its design disagrees with ours on purpose.

Their `docs/PITFALLS.md` is the single most valuable file in the repository and its framing is
worth adopting wholesale: *none of these crashed. Frame counts stayed right, timestamps stayed
even, logs stayed green.* Every entry below has that shape.

---

## 1. Depth — root cause confirmed, and the exact fix

We had concluded "the Vulkan RHI cannot read back a render target". Their `PITFALLS.md` line 12
says it more precisely, and the precision matters:

> `FRenderTarget::ReadLinearColorPixels` on Vulkan calls the `FColor` overload and widens the
> result, so it **quantises to 8 bits**. For an `RTF_R32f` target it does not even get that far:
> the format is absent from the RHI's convert-to-FColor switch and it fires a `checkf`.

That is exactly our `VulkanRenderTarget.cpp:171` assertion, and it means the problem is the
**readback function**, not the RHI and not scene capture. Depth works through
`FRHIGPUTextureReadback`, which is format-agnostic — and which is not reachable from Python.

Their depth conventions, adopted verbatim so one reader works on both datasets:

| | |
|---|---|
| format | EXR, linear **metres**, in the **R** channel |
| invalid | **`-1`** for sky, unwritten pixels, and past `max_range_m` |
| kind | **planar** depth (along the view axis), not radial |
| precision | float16 is enough: relative (~5e-4), so 1 mm at 1 m, 6 cm at 100 m, 65 km range |

Two consumer-side traps they hit and we would have:

- **OpenCV decodes EXR as BGRA, so `cv2.imread(...)` puts depth at index 2, not 0.** Index 0 is
  an all-zero plane that looks exactly like a failed capture.
- `-1` rather than `0` because 0 is also an uninitialised buffer, and "sky" must not look
  identical to "the capture failed"; not NaN because NaN poisons aggregates.

Written: `cpp/SimWorldCapture.{h,cpp}` — `CaptureDepthEXR`, with both `EnqueueCopy`-rect and
row-pitch pitfalls encoded as comments, and a refusal to write a file when every pixel is
invalid.

## 2. NavMesh — we can synthesise one, but only from C++

Downtown_West ships no `ANavMeshBoundsVolume`, which is why every `NavigationSystemV1` query
returns None for us. Their exporter synthesises one for the export and does **not** save it back
to the map. Probed today on our build:

```
has_NavMeshBoundsVolume  True     has_CubeBuilder  False
has_NavigationSystemV1   True     has_Model        False
on_navigation_bounds_updated exposed   has_Polys   False
```

So Python can spawn the volume and announce it, but cannot give it a brush — and *a volume
spawned from code has no brush, and scaling an absent brush is a no-op*. Both pitfalls that make
this silently produce nothing are encoded in `EnsureNavMesh`:

- the `UModel`/`UPolys`/`UCubeBuilder` recipe, without which the bounds stay degenerate;
- `Build()` only marks tiles dirty, so idle must be observed for ~10 consecutive **ticks** that
  the caller drives by hand, or the export comes back empty.

This is what replaces our capsule-sweep route validation, which traces `TRACE_TYPE_QUERY1` —
the **Visibility** channel, not what blocks a Pawn. That mismatch cost us four capture cycles:
a route swept perfectly clean (0 collisions, 238 cm min clearance) and still contained a point
the character could not stand on.

Their `docs/DATA_FORMAT.md` is also right that these are not interchangeable, which we should
state in our own package: *occupancy asks "is something solid here", navmesh asks "can this
agent, with this radius and step height, traverse here". A ledge can be collision-free above and
still unwalkable.*

## 3. The design disagreement worth deciding deliberately

Their planner's docstring:

> What this file does NOT do is make the agent follow the plan. The most valuable signal in this
> dataset is "commanded forward, moved zero" — the moment the world refuses — so the follower
> must be allowed to fall behind, get stuck, and be pushed off the line. Several waypoints here
> are placed deliberately PAST a wall, where arrival is impossible by design. Deviation between
> plan and achieved path is the measurement, not an error to correct.

**Our capture does the opposite.** We teleport the camera onto the frozen pose every frame and
correct the residual until desired == actual to ~3 mm. That guarantees exact revisit ages and
closures, and it makes the trajectory reproducible — but it *erases* the action↔motion
disagreement, and their phase-1 acceptance treats that disagreement as the whole point
(criterion 5: 20.00 cm/step unobstructed vs 0.00 cm/step into geometry).

It also shows up as a hole in our own package: spec section 9 lists `collision/hit result` as
P0, and we write nothing for it, because a teleported camera never resolves a collision.

These are two different datasets, and both are defensible:

| | frozen + teleport (ours now) | planned + driven (theirs) |
|---|---|---|
| revisit age / closure | exact by construction | whatever the walk achieves |
| reproducible | yes, trivially | yes, via fixed timestep + seed |
| action↔motion supervision | **absent** | the primary signal |
| section 9 collision field | **empty** | populated with `actor_id` |
| routes | must be proven walkable first | may fail on purpose |

Recommendation: **drive the character along a navmesh path and record desired vs actual**, which
is what our `walk_episode.py` did before we abandoned it over wedging. The wedging was real, but
the answer is navmesh planning (routes walkable by construction) plus recording the divergence —
not teleporting past it. Keep the frozen-teleport mode for the exact-replay slice, which section
3.10 caps at ≤20% of the data anyway.

## 4. Validator thresholds — measured, not chosen

Their `check_physics_consistency` is directly applicable to our stall detector, which currently
counts zero-displacement frames without asking whether the contact could oppose the heading:

- A contact only counts as blocking when the impact normal opposes the heading at
  **`opposing_dot > 0.85`**, measured: at ≥0.85 the median forward motion is **0.00 cm** over 272
  frames; at 0.60–0.85 it is **6.67 cm** — full free speed. Their earlier 0.35 threshold marked
  sound trajectories INVALID for sliding along a wall, which is what the agent is supposed to do.
- **The first frame of a contact is not a blocked frame.** The agent moved freely through that
  step and the hit is what ended it: entry frames run at 5.97 cm/frame against 0.00 for sustained
  contact on the same wall.
- The check compares a **free-movement baseline** from the same trajectory rather than an
  absolute number, and warns when a run never tested the collision case instead of passing it.

Also worth copying: *a validator check must not contradict the physics it is checking*, and a
gate that cannot be evaluated is a WARN with a reason, never a pass.

## 5. Pitfalls to import into our own operations

Two we have already hit independently, which is a good sign the list is real:

- **Two Unreal processes on one host deadlock** — GPU at 0%, load 0.04, log stopping right after
  `LogDerivedDataCache: Maintenance finished`. We saw exactly this, and separately found that a
  relaunch issued before the old process releases the UnrealCV port comes up with no control
  channel at all.
- **Purchased levels have no PlayerStart**, so UE spawns the pawn at the world origin, which is
  usually empty air, and the agent free-falls for the whole run with every metric green. This is
  our "camera in the sky" bug. Their fix is to pick a standing spot from the occupancy grid; ours
  should come from the navmesh.

Still to import:

- `-usefixedtimestep -fps=N`, **not** `-benchmark`: `-benchmark` sets `IsBenchmarking()`, and
  while the core tick honours it, every subsystem gating on `UseFixedTimeStep()` alone keeps
  free-running. This is our section 6 fixed-timestep requirement, and it retires the
  `vrun slomo 0.10` hack — but note that a fixed timestep only aligns state to pixels if the
  capture happens **inside** the engine tick. Our external Python loop cannot guarantee one tick
  per frame; that needs their capture-actor shape.
- `bAlwaysPersistRenderingState = true` on any capture using a post-process material, or
  `OverrideBlendableSettings` returns immediately and the "mask" comes back as an ordinary
  photograph — 0.988 correlation with RGB, a full 1..255 spread of fake but plausible ids.
- `bRenderInMainRenderer = false` headless: with only scene captures there is no main renderer
  view to fold into, and the target comes back all zeros.
- `FParse::Value` stops at a comma unless `bShouldStopOnSeparator=false`.
- Occupancy `origin` is a voxel **centre**, so index with `round()`, not `floor()` — biasing
  every index down by half a voxel marked ten good runs INVALID.
- Judge a commandlet by its **outputs**, not its exit code: teardown segfaults after every file
  is flushed.

## 6. Applied already, no rebuild needed

Console variables pinned for measurement-grade capture (section 10 wants fixed exposure, motion
blur off, DoF off):

```
r.EyeAdaptation.MethodOverride 1   r.MotionBlurQuality 0
r.EyeAdaptationQuality 0           r.DepthOfFieldQuality 0
r.DefaultFeature.AutoExposure 0    r.PostProcessAAQuality 0
```

`vrun` returns "ok" for a cvar query rather than its value, so this was verified by measurement
instead: the same place, reached at three different times and via different excursions, renders
at mean grey **84.15 / 83.11 / 84.15** with identical p50 (64) and p99 (222). Brightness is a
function of the view, not of how the camera arrived — which is the property the dataset needs,
and their reason for pinning exposure at all.

## 7. Order of work

1. Build `SimWorldCapture` into `gym_citynavRuntime` — unblocks **both** P0 gaps (depth and
   navmesh) with one rebuild.
2. Navmesh route planning replaces the capsule-sweep fan; port their boundary-edge trick for
   deliberately producing contacts once we drive rather than teleport.
3. Driven capture with desired-vs-actual and hits resolved to a stable `actor_id`, so section 9's
   collision field stops being empty.
4. Port `check_physics_consistency` with the 0.85 / contact-entry rules into `package.py`.
5. Fixed timestep, once capture is inside the tick.
