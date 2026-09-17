#!/usr/bin/env python3
"""Live FastAPI server for the collected-clip gallery.

Two things this gets right that `python -m http.server` + a static file did not:

1. Range requests. SimpleHTTPRequestHandler answers 200 with the whole body and no
   Accept-Ranges. The recorder's mp4s keep their `moov` atom at the END of the file
   (no faststart), so a browser must seek there before playback can start - impossible
   without Range. Starlette's StaticFiles implements Range, so seeking works.

2. Live results. `/` re-scans the clip trees on every request, so a newly finished
   recording shows up on plain refresh - no need to re-run build_gallery.py. The
   expensive part (H.264 transcoding, ~11 s per clip) happens in a background worker,
   so the page stays fast and a clip appears immediately with its metadata, gaining a
   playable preview once the worker catches up.

Run:
    python3 local_run/serve.py                  # 0.0.0.0:8500
    PORT=9000 HOST=127.0.0.1 python3 local_run/serve.py
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local_run"))

import build_gallery as bg   # noqa: E402

SITE = bg.GALLERY
MOUNTS = bg.MOUNTS

app = FastAPI(title="SimWorld 采集数据总览", docs_url=None, redoc_url=None)

# --- background transcode worker -------------------------------------------------
_state = {"working": None, "done": 0, "failed": 0, "last_scan": 0.0}
_failed_once = set()
_lock = threading.Lock()


def _worker():
    """Build any missing H.264 previews, newest clips first."""
    while True:
        try:
            todo = []
            # fpv clips are <clip>/video.mp4, the older runner's are
            # <clip>/humanoid_N/video.mp4 - match both, then keep what a mount serves
            for video in sorted(REPO.rglob("video.mp4"),
                                key=lambda p: -p.stat().st_mtime):
                if SITE in video.parents:
                    continue
                if bg.web_path(video) is None:
                    continue
                # skip clips still being written; a growing mp4 has no moov atom yet
                if time.time() - video.stat().st_mtime < 30:
                    continue
                slug = bg.slug_for(video)
                out = bg.PREVIEW / f"{slug}.mp4"
                if not out.exists() or out.stat().st_mtime < video.stat().st_mtime:
                    todo.append((video, slug))
            for video, slug in todo:
                st = video.stat()
                key = (str(video), int(st.st_mtime), st.st_size)
                if key in _failed_once:
                    continue          # do not retry a file that already failed
                with _lock:
                    _state["working"] = slug
                rel, size = bg.make_preview(video, slug)
                with _lock:
                    _state["working"] = None
                    if rel:
                        _state["done"] += 1
                    else:
                        _state["failed"] += 1
                        # One unreadable clip was retried every 5s, 1094 times. Remember
                        # it by path+mtime+size so a re-recorded file still gets a try.
                        _failed_once.add(key)
                        print(f"[worker] giving up on {slug[:60]}", flush=True)
        except Exception as e:                       # never let the worker die
            with _lock:
                _state["working"] = None
            print(f"[worker] {type(e).__name__}: {e}", flush=True)
        time.sleep(5)


@app.on_event("startup")
def _start():
    bg.ensure_mounts()
    bg.PREVIEW = SITE / "preview"
    bg.PREVIEW.mkdir(parents=True, exist_ok=True)
    (SITE / "thumbs").mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_worker, daemon=True).start()
    print("[server] transcode worker started")


@app.get("/healthz")
def healthz():
    with _lock:
        st = dict(_state)
    return JSONResponse({
        "ok": True,
        "clips": {n: (len(list(p.rglob("video.mp4"))) if p.exists() else 0)
                  for n, p in MOUNTS.items()},
        "previews": len(list(bg.PREVIEW.glob("*.mp4"))) if bg.PREVIEW else 0,
        "transcode": st,
    })


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(page: int = 1, per: int = 20, sort: str = "new", tag: str = "*"):
    # Re-scan every request so new clips appear on refresh. transcode=False keeps this
    # fast; the scan cache keeps it from re-probing every clip with cv2.
    clips = bg.collect(transcode=False)
    with _lock:
        working = _state["working"]
    pending = sum(1 for c in clips if not c["preview"])

    # Sort and filter server-side, then slice: doing it in the browser would only
    # reorder the current page, which is wrong once there is more than one.
    if sort not in bg.SORTS:
        sort = "new"
    sel = bg.sort_clips(bg.filter_clips(clips, tag), sort)
    per = max(1, min(per, 200))
    pages = max(1, -(-len(sel) // per))
    page = max(1, min(page, pages))
    start = (page - 1) * per
    html = bg.render(sel[start:start + per], ctx={
        "page": page, "pages": pages, "total": len(sel),
        "sort": sort, "tag": tag, "per": per, "all_clips": clips})
    banner = ""
    if pending:
        cur = f"，当前 <code>{working[:52]}…</code>" if working else ""
        banner = (f'<div class="live">{pending} 条视频的浏览器可播版本仍在后台转码{cur}'
                  f' · 手动刷新本页即可看到最新状态</div>')
    html = html.replace("<div class=\"bar\">", banner + "<div class=\"bar\">", 1)
    # No <meta http-equiv="refresh">: the page still re-scans on every request, but an
    # automatic reload would interrupt whatever video the viewer is watching.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


for _name, _path in MOUNTS.items():
    _path.mkdir(parents=True, exist_ok=True)
    app.mount(f"/{_name}", StaticFiles(directory=_path), name=_name)

# The revisit-dataset test page and its episode media. Kept as its own route so the
# original gallery is untouched.
_NR = REPO / "local_run" / "nr" / "episodes"
_NR.mkdir(parents=True, exist_ok=True)
app.mount("/nr_episodes", StaticFiles(directory=_NR), name="nr_episodes")

_PIPE = REPO / "local_run" / "pipeline" / "episodes"
_PIPE.mkdir(parents=True, exist_ok=True)
app.mount("/pipeline_episodes", StaticFiles(directory=_PIPE), name="pipeline_episodes")

_NRSITE = SITE / "new_request"
_NRSITE.mkdir(parents=True, exist_ok=True)


@app.get("/new_request/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/new_request", response_class=HTMLResponse, include_in_schema=False)
def new_request():
    """Rebuild on every request so a freshly recorded episode shows up on refresh.

    The pipeline builder is preferred and the older walk-recorder page is the fallback: they read
    different frames.csv schemas, so pointing one at the other's episodes only produces a
    KeyError. If the pipeline builder fails, the failure is surfaced on the page instead of
    silently serving a stale index that looks current.
    """
    err = None
    for script in ("pipeline/build_page.py", "nr/build_page.py"):
        p = REPO / "local_run" / script
        if not p.exists():
            continue
        try:
            r = subprocess.run([sys.executable, str(p)], capture_output=True, timeout=300,
                               text=True)
            if r.returncode == 0:
                err = None
                break
            err = f"{script}: {(r.stderr or r.stdout or '').strip()[-600:]}"
        except Exception as e:
            err = f"{script}: {type(e).__name__}: {e}"
        print(f"[new_request] {err}", flush=True)
    idx = _NRSITE / "index.html"
    if not idx.exists():
        return HTMLResponse(f"<h1>还没有 episode</h1><pre>{err or ''}</pre>", status_code=200)
    body = idx.read_text(encoding="utf-8")
    if err:
        body = body.replace(
            "<div class=\"wrap\">",
            f"<div style='margin:12px 24px;padding:10px;border-radius:8px;"
            f"background:rgba(255,107,107,.12);color:#ff6b6b;font:12px monospace;"
            f"white-space:pre-wrap'>页面重建失败，下面显示的是上一次成功的结果：\n"
            f"{err}</div><div class=\"wrap\">", 1)
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


for _sub in ("preview", "thumbs"):
    _d = SITE / _sub
    _d.mkdir(parents=True, exist_ok=True)
    app.mount(f"/{_sub}", StaticFiles(directory=_d), name=_sub)

# Ad-hoc pages that are written by hand rather than built from a clip tree:
#   npc       - the wandering-NPC clip from local_run/tokyo_npcs.py
#   longvideo - the long-video job's status board. Its work runs on 15 other machines and lands
#               in S3, so there is no clip tree to scan; local_run/longvideo_status.py refreshes
#               site/longvideo/status.json on a loop and the page fetches it.
# html=True so /<name>/ serves its index.html; the clip mounts deliberately do not, because a
# directory listing there would expose the whole episode tree.
for _adhoc in ("npc", "longvideo", "dubai"):
    _d = SITE / _adhoc
    _d.mkdir(parents=True, exist_ok=True)
    app.mount(f"/{_adhoc}", StaticFiles(directory=_d, html=True), name=_adhoc)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8500"))
    print(f"serving {SITE} on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
