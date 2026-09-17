# Motion shimmer regression evidence, 16 Sep 2026

See `../../RENDER_QUALITY.md` section 8 for diagnosis, profile and qualifications.
Comparison JSON contains matching pose checks, 40 moving / 40 static pair samples and
12 depth samples per variant; percentages are not a pixel-level ground truth for flicker.

Native capture data, videos, build log and before-edit backups are preserved in
`/home/ubuntu/ue_flicker_20260916/`. The copied drivers document the exact local commands and
paths; they intentionally refuse to overwrite a variant directory. Start one local UE 5.8
editor for the indicated map and use one UnrealCV connection per editor lifetime. The short
probe is a truncated render diagnostic, not a repackaged accepted training episode.

Selected profile: history mode 4, TSR cvar 4, supersample 2, auto_instant metering. No fixed
GI jitter. C++ was built and loaded in the local `gym_citynav` project. Local deployed Python,
C++ sources and template were backed up and synchronized; no cloud jobs were started.

Web: http://51.20.82.218:8500/longvideo/flicker-fix/
