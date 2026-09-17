# Game_WinterTown_Maps_RussianWinterTownDemo01__nested_out_and_back__age35__seed5__batch5_winter_town_v2

Map `/Game/WinterTown/Maps/RussianWinterTownDemo01`, spawn `test1`, seed 5.
Recorded with `pipeline/capture.py` (pipeline-capture-engine-1) from the frozen trajectory
`9d747a6785087049`, UE 5.8.

## What is here

- `rgb.mp4` - 2844 frames at 24 FPS,
  1280x720, H.264, faststart.
- `rgb_keyframes/` - lossless PNG for the anchor and revisit windows.
- `frames.csv` - per-frame time, desired and actual pose, quaternion,
  canonical c2w, pose error, speed.
- `camera.json` - final K and the full coordinate contract.
- `trajectory.json` - the frozen route, its collision evidence and the reach fan it was chosen
  from.
- `revisits.json`, `acceptance.json`, `contact.png`, `files.sha256`.

## What is missing, and why

`depth/`, `instance/` and `semantic/` are absent. Depth is not a matter of a different request:
this build's Vulkan RHI cannot read back a render target at all - any attempt asserts at
`VulkanRenderTarget.cpp:171` and takes the editor down, and UnrealCV's own depth returns a
constant 65504, which is the fp16 maximum left in an unwritten buffer. Everything that depends
on depth (spec section 9 P0 depth, section 12 depth-visible overlap, occlusion intervals) is
therefore reported as not computed rather than estimated. This episode is **not** a complete P0
episode and does not count toward the section 17 target.
