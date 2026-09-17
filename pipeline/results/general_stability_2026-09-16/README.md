# Detail-preserving cross-map stability trial

User constraint: building detail is the priority. Do not adopt a softer image merely because the flicker proxy improves.

Artifacts: `/home/ubuntu/ue_general_stability_20260916`. Live review: http://51.20.82.218:8500/longvideo/general-stability/

The trial compares the current TSR 2x profile with TAA 3x/200% history, TSR 3x, and TAA 2x/200% history. Quality candidates use denser Lumen screen probes, 16x anisotropic filtering, mip bias 0, no added sharpening, and retain fog and reflections. Capture mode 4 and its 32 rendered warmups are common. Output stays 1280x720 at 24 fps, with native unfiltered depth. All material and geometry assets are unchanged.

Routes are matching 600-frame (25 s) prefixes, containing still, translation and turning phases. Maps: ChemicalPlant 2, Downtown West, Hwaseong and ForestGasStation. Fixed fill factors within each map (1.5, 3, 4, 1) isolate render comparisons; they are not proposed as universal lighting values. The existing automatic fill behavior remains available.

`measure.py` uses the same baseline flow for every candidate and reports translation, turns, static frame differences, far geometry and manually selected opening building regions separately. Motion residual includes optical-flow error/occlusion. Edge strength and Laplacian measures include noise and cannot prove lossless detail. The per-map native RGB/depth/poses and actual cvars remain in the artifact root. Render diagnostics are not newly accepted dataset episodes; route truncation preserves original annotation metadata.

Scripts in this directory are audit snapshots and resolve paths relative to their own location. Run the originals from the artifact root. Each editor lifetime uses one UnrealCV connection; the coordinator starts one editor at a time and stops its own process group before changing maps.

Completed: sixteen 25-second videos / 9,600 frames. No global default promotion; see summary.json, validation.json and RENDER_QUALITY.md.

Browser verification passed all 12 candidate/map selections, synchronized playback/seek, building zoom canvases, mobile layout and old URLs. The site serves the two candidate task JSONs for direct review/download. All sixteen native clips have 600 RGB/depth/state frames, zero write failures and zero tracked pose error.
