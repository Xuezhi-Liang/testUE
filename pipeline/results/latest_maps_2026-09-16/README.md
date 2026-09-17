# Latest-profile 118.5-second map tests, 16 Sep 2026

Maps: `/Game/Downtown_West/Maps/Demo_Environment` and
`/Game/ChemicalPlantEnv/Maps/Map_ChemicalPlant_2`. Each replays its original 2844-frame
validated route at 24 fps, with the exact render section of `tasks/longvideo_template.json`.
The sky-light factor is actually calibrated automatically in each scene (not a numeric override).

Recordings and logs: `/home/ubuntu/ue_latest_two_maps_20260916_0639/`.
Live videos and reports: http://51.20.82.218:8500/longvideo/latest-config-20260916/

`record_map.py` records one map after its local editor is launched; it uses one UnrealCV
connection with synchronous setup. Run one editor at a time and wait for complete shutdown
before switching maps. The first startup exited and the next client stalled during the legacy
wrapper handshake; logs are retained under the artifact directory. The successful driver
uses a bounded handshake and synchronous commands. No rendering configuration was changed
for this connection workaround.

`test_exposure_bridge.py` exercises the actual capture entrypoint with fake engine IO for
five exposure-selection cases. The manual calibration EV must not leak into auto exposure,
nor into subsequent captures. Explicit task/environment offsets still take precedence.

Both recordings completed with 2,844 RGB/depth/pose frames (118.5 s each), zero write failures
and zero tracked position/yaw error. Runtime confirms mode 4, TemporalAA, 32 warmup frames,
2560x1440 RGB target and no manual-EV offset applied to auto exposure. Downtown calibration
selected x3 and passed its bar. Chemical selected x1.5 but failed the near-black calibration
bar; this is flagged on the page and retained in the report, not reported as a lighting pass.
Downtown's 11 depth-ray differences above 15 cm all hit distant Landscape_0 at 177–233 m;
the remaining 21 samples agree. Possible render/collision terrain LOD differences are not
a proof of depth equivalence. Raw discrepancies for both scenes remain in the JSON reports.
Browser QA verifies playback/seek, 118.5 s duration, 1280x720 size, byte ranges and mobile width.
