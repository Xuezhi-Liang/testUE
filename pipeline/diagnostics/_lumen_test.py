#!/usr/bin/env python3
"""Does the image at a FIXED pose change over consecutive captures?

neighborhood came out with 50-58% of pixels crushed to pure black while the sky and sunlit grass
looked correct - the signature of indirect light that has not accumulated. Every CaptureScene is
close to a fresh render, and the camera moves every frame, so Lumen's temporal accumulation may
never converge. If that is what is happening, brightness at a fixed pose will climb over the first
frames and then flatten; if it is flat from frame 0, the darkness is the tonemapper and not
convergence, and the fix is a different one.
"""
import sys
import time
from pathlib import Path

P = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline")
sys.path.insert(0, str(P))
import engine  # noqa: E402

OUT = "/home/ue4/lumen_test"

ucv = engine.connect(1280, 720, timeout=600)
req = ucv.client.request
r = engine.simworld(req, "start_capture",
                    f'"{P}/frozen/lumen_test.json", "{OUT}", 1280, 720, 90.0, '
                    f'1000.0, True, 92, True')
print("armed:", r.get("ok"), r.get("error", ""))
if not r.get("ok"):
    sys.exit(1)
while True:
    st = engine.simworld(req, "get_capture_status", "")
    if st.get("finished"):
        break
    time.sleep(3)
print(f"captured {st['frame']} frames at one fixed pose")
