# ChemicalPlant left far-building diagnostics, 2026-09-16

Raw artifact root: `/home/ubuntu/ue_chemical_far_20260916`.
Web: http://51.20.82.218:8500/longvideo/chemical-far/

Scripts are snapshots: run them from the raw artifact root, because their paths are relative to `__file__`. `adaptive.py` requires the existing editor, one UnrealCV connection and the enroot environment; do not connect another client. Preserved job JSON, native captures, RGB/depth/poses and actual runtime cvars are in the artifact root. New script executions must use fresh output paths.

Optional render preset: `../../tasks/chemical_far_stable_template.json`. It selects TAA 2x for this map; the global TSR template is unchanged. See `../../RENDER_QUALITY.md` for local metrics, texture-detail tradeoff, failed candidates, fog diagnostic limitations and native validation.
