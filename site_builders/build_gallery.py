#!/usr/bin/env python3
"""Scan collected SimWorld clips and emit a static gallery site.

Videos are 8 MB+ each, so they are referenced by relative path rather than embedded;
serve the repo root over HTTP and open /local_run/gallery/index.html.

Re-run this after more clips land to refresh the gallery.
"""
import csv
import collections
import subprocess
import time
import html
import json
import re
from pathlib import Path

import cv2

REPO = Path("/home/ubuntu/WM-Unreal-data-collection")
# Served publicly, so the web root deliberately contains ONLY the page, thumbnails and
# symlinks to the clip trees - never the repo source.
GALLERY = REPO / "local_run" / "site"
THUMBS = GALLERY / "thumbs"
# web-root-relative mount points -> real directories
MOUNTS = {
    "fpv": REPO / "local_run" / "fpv_data",             # first-person look-around (pixel-verified)
    "arch": REPO / "local_run" / "fpv_archive",         # earlier batch, recovered from previews
    "data": REPO / "local_run" / "ue_data",            # patrol clips
    "look": REPO / "local_run" / "lookaround_data",    # earlier humanoid look-around
    "single": REPO / "output",                         # minimal_usage single runs
}


def ensure_mounts():
    GALLERY.mkdir(parents=True, exist_ok=True)
    for name, target in MOUNTS.items():
        link = GALLERY / name
        if link.is_symlink() or link.exists():
            continue
        if target.exists():
            link.symlink_to(target)


def slug_for(video):
    """Stable id for a clip, used for preview/thumbnail filenames."""
    return re.sub(r"[^A-Za-z0-9]+", "-",
                  str(video.parent.relative_to(REPO))).strip("-")


def web_path(video):
    """Map an on-disk clip path to its URL under the served web root."""
    for name, target in MOUNTS.items():
        try:
            return f"{name}/{video.relative_to(target)}"
        except ValueError:
            continue
    return None

# angle -> canonical direction, per minimal_usage/ACTIONS.md
ANGLE_DIR = {
    1: "前进 W", -90: "左移 A", 179: "后退 S", 90: "右移 D",
    -45: "左前 W+A", 45: "右前 W+D", -135: "左后 S+A", 135: "右后 S+D",
}
SPEED_NAME = {100: "慢走", 150: "中速", 200: "快速"}


def classify(actions_csv):
    """Turn a per-frame action log into category tags with frame counts."""
    move, cam, speeds, other = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    total = 0
    with open(actions_csv) as fh:
        for row in csv.DictReader(fh):
            a = row.get("action", "")
            total += 1
            m = re.match(r"move_strafe_(-?\d+)_(-?\d+)", a)
            if m:
                spd, ang = int(m.group(1)), int(m.group(2))
                move[ANGLE_DIR.get(ang, f"角度 {ang}°")] += 1
                speeds[SPEED_NAME.get(spd, f"{spd} u/s")] += 1
                continue
            m = re.match(r"camera_rotate_incremental_dp(-?[\d.]+)_dy(-?[\d.]+)", a)
            if m:
                dp, dy = float(m.group(1)), float(m.group(2))
                parts = []
                if dp < -0.1:
                    parts.append("镜头下")
                elif dp > 0.1:
                    parts.append("镜头上")
                if dy > 0.1:
                    parts.append("镜头右")
                elif dy < -0.1:
                    parts.append("镜头左")
                cam[" + ".join(parts) if parts else "镜头静止"] += 1
                continue
            other[a.split("_")[0]] += 1
    return dict(move=move, cam=cam, speeds=speeds, other=other, total=total)


def travel_distance(camera_csv):
    """Total XY path length from the per-frame camera log, in Unreal units."""
    pts = []
    try:
        with open(camera_csv) as fh:
            for row in csv.DictReader(fh):
                loc = row.get("camera_loc", "").split()
                if len(loc) >= 2:
                    pts.append((float(loc[0]), float(loc[1])))
    except Exception:
        return None
    if len(pts) < 2:
        return None
    return sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
               for a, b in zip(pts, pts[1:]))


PREVIEW = None  # set in collect(); site/preview
PREVIEW_W = 1280   # keep native width; H.264 makes it affordable


def _ffmpeg():
    """Static ffmpeg from the imageio-ffmpeg wheel - no root, no system package."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def make_preview(video, slug):
    """Transcode the recorder's mp4v into a browser-playable H.264 mp4.

    cv2.VideoWriter_fourcc(*'mp4v') is MPEG-4 Part 2, which no browser plays in
    <video>, and this opencv build has no H.264 encoder. VP8 via opencv "worked" but
    was unusable: a 2-minute 720p clip took minutes and came out at 365 MB - larger
    than the source. ffmpeg does the same clip in ~11 s at ~45 MB, and +faststart puts
    the moov atom up front so playback starts without seeking to the end of the file.
    Incremental: skips when the preview is already newer than the source.
    """
    out = PREVIEW / f"{slug}.mp4"
    if out.exists() and out.stat().st_mtime >= video.stat().st_mtime:
        return f"preview/{out.name}", out.stat().st_size

    ff = _ffmpeg()
    if ff is None:
        return None, 0
    tmp = out.with_suffix(".part.mp4")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
           "-vf", f"scale={PREVIEW_W}:-2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-an", str(tmp)]
    try:
        subprocess.run(cmd, check=True, timeout=1800,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"  ffmpeg failed for {slug[:50]}: {e}", flush=True)
        return None, 0
    if tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(out)
        return f"preview/{out.name}", out.stat().st_size
    tmp.unlink(missing_ok=True)
    return None, 0


CACHE = None          # set in collect(); site/scan_cache.json
_cache_data = {}
_cache_dirty = False


def _cache_key(video):
    st = video.stat()
    return f"{video}|{int(st.st_mtime)}|{st.st_size}"


def cache_load():
    """Probing every clip with cv2 on every page request does not scale: at ~850 clips
    that is 1700 video opens per refresh. Results are keyed by path+mtime+size, so a
    re-recorded clip invalidates its own entry."""
    global _cache_data
    if _cache_data:
        return                      # long-lived server process: load the file once
    if CACHE and CACHE.exists():
        try:
            _cache_data = json.load(open(CACHE, encoding="utf-8"))
            return
        except Exception:
            pass
    _cache_data = {}


def cache_save():
    global _cache_dirty
    if CACHE and _cache_dirty:
        try:
            tmp = CACHE.with_suffix(".part")
            tmp.write_text(json.dumps(_cache_data), encoding="utf-8")
            tmp.replace(CACHE)
            _cache_dirty = False
        except Exception:
            pass


def probe(video):
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    info = dict(
        frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        w=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        h=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        fps=cap.get(cv2.CAP_PROP_FPS) or 0,
    )
    info["seconds"] = info["frames"] / info["fps"] if info["fps"] else 0
    # grab a poster frame ~15% in so it isn't a black fade-in
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(info["frames"] * 0.15)))
    ok, frame = cap.read()
    cap.release()
    info["_poster"] = frame if ok else None
    return info


def read_meta(traj_dir):
    """Read either the batch runner's meta.txt or record_session.py's meta.json."""
    meta = {}
    j = traj_dir / "meta.json"
    if j.exists():
        try:
            for k, v in json.load(open(j, encoding="utf-8")).items():
                meta[k] = v
        except Exception:
            pass
    t = traj_dir / "meta.txt"
    if t.exists():
        for line in t.read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta.setdefault(k.strip(), v.strip())
    return meta


def collect(transcode=True):
    """Scan clips. transcode=False keeps this fast enough to run per HTTP request;
    the server then builds missing previews in a background thread."""
    global PREVIEW
    ensure_mounts()
    THUMBS.mkdir(parents=True, exist_ok=True)
    PREVIEW = GALLERY / "preview"
    PREVIEW.mkdir(parents=True, exist_ok=True)
    global CACHE
    CACHE = GALLERY / "scan_cache.json"
    cache_load()
    clips = []
    # freecam clips live at <clip>/video.mp4 (no humanoid_* level), the recorder-based
    # ones at <clip>/humanoid_N/video.mp4 - match both, then keep only paths that fall
    # under a declared mount.
    for video in sorted(REPO.rglob("video.mp4")):
        if GALLERY in video.parents:      # don't re-scan through our own symlinks
            continue
        url = web_path(video)
        if url is None:
            continue
        hum = video.parent
        traj = hum.parent
        global _cache_dirty
        slug = slug_for(video)
        ck = _cache_key(video)
        hit = _cache_data.get(ck)
        if hit is not None:
            info, thumb_rel = dict(hit["info"]), hit["thumb"]
            if thumb_rel and not (GALLERY / thumb_rel).exists():
                thumb_rel = None       # thumbnail was pruned; fall through and redo it
        else:
            info, thumb_rel = None, None
        if info is None:
            info = probe(video)
            if not info:
                continue
            frame = info.pop("_poster")
            if frame is not None:
                h, w = frame.shape[:2]
                scale = 480 / max(w, 1)
                small = cv2.resize(frame, (480, max(1, int(h * scale))))
                out = THUMBS / f"{slug}.jpg"
                cv2.imwrite(str(out), small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                thumb_rel = f"thumbs/{out.name}"
            _cache_data[ck] = {"info": info, "thumb": thumb_rel}
            _cache_dirty = True
        info.pop("_poster", None)

        actions_csv = hum / "actions.csv"
        cats = classify(actions_csv) if actions_csv.exists() else None
        # the batch runner puts meta.txt on the trajectory dir, freecam puts meta.json
        # inside the clip dir itself - read both, clip-level wins
        meta = read_meta(traj)
        meta.update(read_meta(hum))

        # Which run/source bucket this belongs to
        rel = video.relative_to(REPO)
        mount = url.split("/", 1)[0]
        if mount == "single":
            source, run, behavior = "单条测试", "minimal_usage", rel.parts[1]
        else:
            source = {"fpv": "第一人称环顾", "arch": "早期批次(已找回)", "look": "第一人称环顾(旧)",
                      "data": "巡逻采集"}.get(mount, "采集")
            run = rel.parts[2] if len(rel.parts) > 2 else "?"
            # record_session.py writes the behavior name; the older batch runner only
            # has a policy, and the dir name is the fallback for both.
            behavior = (meta.get("behavior") or meta.get("policy")
                        or traj.name or "?")

        variants = {p.name: f"{url.rsplit('/', 1)[0]}/{p.name}"
                    for p in sorted(hum.glob("*.mp4"))}

        existing = PREVIEW / f"{slug}.mp4"
        if transcode:
            print(f"  transcoding {slug[:60]} ...", flush=True)
            preview_rel, preview_size = make_preview(video, slug)
        elif existing.exists() and existing.stat().st_mtime >= video.stat().st_mtime:
            preview_rel, preview_size = f"preview/{existing.name}", existing.stat().st_size
        else:
            preview_rel, preview_size = None, 0   # server will build it shortly

        clips.append(dict(
            slug=slug, source=source, run=run, behavior=behavior,
            mtime=video.stat().st_mtime,
            rel=url, preview=preview_rel, preview_size=preview_size,
            thumb=thumb_rel, info=info,
            cats=cats, meta=meta, variants=variants,
            distance=travel_distance(hum / "camera.csv"),
            map=meta.get("map", "—"),
        ))
    cache_save()
    return clips


def chips(counter, cls):
    if not counter:
        return ""
    return "".join(
        f'<span class="chip {cls}">{html.escape(str(k))}<b>{v}</b></span>'
        for k, v in counter.most_common()
    )


SORTS = {"new": "最新优先", "old": "最早优先", "name": "按名称", "long": "时长"}


def sort_clips(clips, mode):
    if mode == "old":
        return sorted(clips, key=lambda c: c["mtime"])
    if mode == "name":
        return sorted(clips, key=lambda c: c["slug"])
    if mode == "long":
        return sorted(clips, key=lambda c: -c["info"]["seconds"])
    return sorted(clips, key=lambda c: -c["mtime"])          # "new" is the default


def filter_clips(clips, tag):
    if not tag or tag == "*":
        return clips
    return [c for c in clips
            if c["cats"] and tag in (list(c["cats"]["move"])
                                     + list(c["cats"]["speeds"]))]


def render(clips, ctx=None):
    # Header totals and the filter chips describe the WHOLE dataset, not the page being
    # shown, so they are computed from the full list when one is supplied.
    everything = (ctx or {}).get("all_clips") or clips
    all_tags = sorted({k for c in everything if c["cats"]
                       for k in list(c["cats"]["move"]) + list(c["cats"]["speeds"])})
    total_sec = sum(c["info"]["seconds"] for c in everything)
    total_frames = sum(c["info"]["frames"] for c in everything)

    cards = []
    for c in clips:
        tags = []
        if c["cats"]:
            tags = list(c["cats"]["move"]) + list(c["cats"]["speeds"])
        extra = "".join(
            f'<a href="{html.escape(v)}" download>{html.escape(n)}</a>'
            for n, v in c["variants"].items())
        dist = f'{c["distance"]:.0f} uu' if c["distance"] else "—"
        stamp = time.strftime('%m-%d %H:%M', time.localtime(c['mtime']))
        meta_rows = "".join(
            f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
            for k, v in c["meta"].items()
            if k in ("map", "start_position", "policy", "steps", "seed",
                     "noise_xy", "trajectory_index", "resolution", "fps"))
        cards.append(f"""
<article class="card" data-tags="{html.escape('|'.join(tags))}" data-source="{html.escape(c['source'])}" data-mtime="{c['mtime']:.0f}" data-name="{html.escape(c['slug'])}" data-seconds="{c['info']['seconds']:.1f}">
  {(f'''<video controls preload="none" {f'poster="{html.escape(c["thumb"])}"' if c["thumb"] else ""}>
    <source src="{html.escape(c['preview'])}" type="video/mp4">
  </video>''') if c['preview'] else (
   f'''<div class="noplay" {f'style="background-image:url({html.escape(c["thumb"])})"' if c["thumb"] else ""}>
    <span>浏览器可播版本转码中 · 稍后刷新，或用下方原始 mp4 下载</span></div>''')}
  <div class="body">
    <div class="hd">
      <h3>{html.escape(c['behavior'])}</h3>
      <span class="src">{html.escape(c['source'])}</span>
    </div>
    <p class="path">{html.escape(c['slug'])}</p>
    <div class="stats">
      <span><b>{c['info']['frames']}</b> 帧</span>
      <span><b>{c['info']['seconds']:.1f}</b> 秒</span>
      <span>{c['info']['w']}×{c['info']['h']}</span>
      <span>{c['info']['fps']:.0f} fps</span>
      <span>位移 <b>{dist}</b></span>
      <span>录制 <b>{stamp}</b></span>
    </div>
    <div class="cats">
      <div class="row"><span class="lbl">移动</span>{chips(c['cats']['move'], 'move') if c['cats'] else '—'}</div>
      <div class="row"><span class="lbl">速度</span>{chips(c['cats']['speeds'], 'spd') if c['cats'] else '—'}</div>
      <div class="row"><span class="lbl">镜头</span>{chips(c['cats']['cam'], 'cam') if c['cats'] else '—'}</div>
      <div class="row"><span class="lbl">其他</span>{chips(c['cats']['other'], 'oth') if c['cats'] else '—'}</div>
    </div>
    {f'<div class="variants">原始文件（mp4v 编码，浏览器无法直接播放，需下载）：{extra}</div>' if extra else ''}
    <details><summary>meta</summary><table>{meta_rows}</table></details>
  </div>
</article>""")

    maps = sorted({(c["map"] or "—").split("/")[-1] for c in everything})
    kinds = sorted({c["source"] for c in everything})
    maps_line = (f"{len(maps)} 个地图 · " + "、".join(kinds)
                 if len(maps) > 4 else
                 "地图 " + "、".join(maps) + " · " + "、".join(kinds))

    walk = [c for c in everything if c["source"] != "第一人称环顾"]
    warn_html = ""
    if walk:
        warn_html = (
            '<p class="warn"><b>行走类片段的动作覆盖不完整。</b>'
            f'当前 {len(walk)} 条行走/巡逻片段只有「走」类动作：跳跃两端都不支持'
            '（humanoid 蓝图没有 Jump 节点，Python 动作表也没有），'
            '「跑」只是 <code>move_strafe</code> 提高速度值，动画是否真的切到 run 状态尚未验证。'
            '第一人称环顾片段不受此影响，它们是纯摄像头运动。</p>')

    ctx = ctx or {}
    page = ctx.get("page", 1)
    pages = ctx.get("pages", 1)
    total = ctx.get("total", len(clips))
    cur_sort = ctx.get("sort", "new")
    cur_tag = ctx.get("tag", "*")
    per = ctx.get("per", len(clips) or 1)
    paged = bool(ctx)

    def url(**kw):
        q = {"sort": cur_sort, "tag": cur_tag, "page": page, "per": per}
        q.update(kw)
        if q["tag"] == "*":
            q.pop("tag")
        return "?" + "&".join(f"{k}={html.escape(str(v))}" for k, v in q.items())

    if paged:
        sorts = "".join(
            f'<a class="btn{" on" if k == cur_sort else ""}" '
            f'href="{url(sort=k, page=1)}">{v}</a>' for k, v in SORTS.items())
        filters = "".join(
            f'<a class="btn{" on" if t == cur_tag else ""}" '
            f'href="{url(tag=t, page=1)}">{html.escape(t)}</a>' for t in all_tags)
        all_btn = (f'<a class="btn{" on" if cur_tag == "*" else ""}" '
                   f'href="{url(tag="*", page=1)}">全部</a>')
    else:
        # no backslash inside the f-string expression: the container runs python 3.10,
        # where that is a syntax error
        def _sbtn(k, v):
            on = ' class="on"' if k == "new" else ""
            return f'<button data-s="{k}"{on}>{v}</button>'
        sorts = "".join(_sbtn(k, v) for k, v in SORTS.items())
        filters = "".join(f'<button data-f="{html.escape(t)}">{html.escape(t)}</button>'
                          for t in all_tags)
        all_btn = '<button data-f="*" class="on">全部</button>'

    # window of page links around the current page, so 43 pages do not print 43 links
    pager = ""
    if paged and pages > 1:
        lo, hi = max(1, page - 3), min(pages, page + 3)
        links = []
        if page > 1:
            links.append(f'<a class="btn" href="{url(page=1)}">« 首页</a>')
            links.append(f'<a class="btn" href="{url(page=page-1)}">‹ 上一页</a>')
        if lo > 1:
            links.append('<span class="gap">…</span>')
        for i in range(lo, hi + 1):
            links.append(f'<a class="btn{" on" if i == page else ""}" '
                         f'href="{url(page=i)}">{i}</a>')
        if hi < pages:
            links.append('<span class="gap">…</span>')
        if page < pages:
            links.append(f'<a class="btn" href="{url(page=page+1)}">下一页 ›</a>')
            links.append(f'<a class="btn" href="{url(page=pages)}">末页 »</a>')
        pager = (f'<div class="bar pager"><span class="grp">第 {page} / {pages} 页 · '
                 f'共 {total} 段</span>{"".join(links)}</div>')

    # python -m http.server sends "text/html" with no charset, so the meta tag is what
    # keeps the Chinese labels from being decoded as latin-1.
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SimWorld 采集数据 · 视频总览</title>
<style>
:root {{
  --bg:#f6f7f9; --fg:#15181d; --mut:#5b6472; --card:#fff; --line:#e2e6ec;
  --move:#1e6fd9; --spd:#0b8a5f; --cam:#8a5a0b; --oth:#6b7280;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0e1116; --fg:#e6e9ee; --mut:#9aa4b2; --card:#161b22; --line:#242b35;
    --move:#6ea8fe; --spd:#4ad6a0; --cam:#e0b060; --oth:#9aa4b2; }}
}}
:root[data-theme="dark"] {{
  --bg:#0e1116; --fg:#e6e9ee; --mut:#9aa4b2; --card:#161b22; --line:#242b35;
  --move:#6ea8fe; --spd:#4ad6a0; --cam:#e0b060; --oth:#9aa4b2;
}}
:root[data-theme="light"] {{
  --bg:#f6f7f9; --fg:#15181d; --mut:#5b6472; --card:#fff; --line:#e2e6ec;
  --move:#1e6fd9; --spd:#0b8a5f; --cam:#8a5a0b; --oth:#6b7280;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif;
  overflow-x:hidden}}
header{{padding:28px 20px 12px;max-width:1400px;margin:0 auto}}
h1{{margin:0 0 6px;font-size:23px;letter-spacing:-.01em}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}
.totals{{display:flex;flex-wrap:wrap;gap:18px;margin:16px 0 0;padding:14px 16px;
  background:var(--card);border:1px solid var(--line);border-radius:10px}}
.totals div{{font-size:13px;color:var(--mut)}}
.totals b{{display:block;font-size:19px;color:var(--fg)}}
.warn{{margin:14px 0 0;padding:11px 14px;border-radius:8px;font-size:13.5px;
  background:color-mix(in srgb,var(--cam) 12%,transparent);
  border:1px solid color-mix(in srgb,var(--cam) 35%,transparent)}}
.live{{max-width:1400px;margin:16px auto 0;padding:10px 16px;border-radius:8px;
  font-size:13.5px;background:color-mix(in srgb,var(--move) 12%,transparent);
  border:1px solid color-mix(in srgb,var(--move) 35%,transparent)}}
.live code{{font-size:12px}}
.bar{{max-width:1400px;margin:18px auto 0;padding:0 20px;display:flex;
  flex-wrap:wrap;gap:7px;align-items:center}}
.bar button{{font:inherit;font-size:12.5px;padding:5px 11px;border-radius:99px;cursor:pointer;
  background:var(--card);color:var(--fg);border:1px solid var(--line)}}
.bar button.on{{background:var(--move);border-color:var(--move);color:#fff}}
.bar .grp{{font-size:12px;color:var(--mut);margin-right:4px}}
.bar .btn{{font-size:12.5px;padding:5px 11px;border-radius:99px;cursor:pointer;
  background:var(--card);color:var(--fg);border:1px solid var(--line);
  text-decoration:none;display:inline-block}}
.bar .btn.on{{background:var(--move);border-color:var(--move);color:#fff}}
.bar .gap{{color:var(--mut);font-size:12.5px}}
.bar.pager{{margin-top:14px}}
.grid{{max-width:1400px;margin:18px auto 60px;padding:0 20px;display:grid;gap:18px;
  grid-template-columns:repeat(auto-fill,minmax(420px,1fr))}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
  display:flex;flex-direction:column}}
.card video{{width:100%;aspect-ratio:16/9;background:#000;display:block}}
.noplay{{width:100%;aspect-ratio:16/9;background:#000 center/cover no-repeat;display:flex;
  align-items:flex-end;justify-content:center}}
.noplay span{{background:rgba(0,0,0,.72);color:#fff;font-size:12px;padding:6px 10px;
  width:100%;text-align:center}}
.body{{padding:14px 15px 15px}}
.hd{{display:flex;justify-content:space-between;align-items:baseline;gap:10px}}
.hd h3{{margin:0;font-size:15.5px;font-family:ui-monospace,monospace}}
.src{{font-size:11.5px;color:var(--mut);white-space:nowrap}}
.path{{margin:4px 0 10px;font-size:11px;color:var(--mut);
  font-family:ui-monospace,monospace;word-break:break-all}}
.stats{{display:flex;flex-wrap:wrap;gap:12px;font-size:12.5px;color:var(--mut);
  padding-bottom:11px;border-bottom:1px solid var(--line)}}
.stats b{{color:var(--fg)}}
.cats{{padding-top:10px;display:flex;flex-direction:column;gap:6px}}
.row{{display:flex;gap:7px;align-items:flex-start;flex-wrap:wrap}}
.lbl{{font-size:11.5px;color:var(--mut);min-width:32px;padding-top:3px}}
.chip{{font-size:11.5px;padding:2px 8px;border-radius:99px;display:inline-flex;gap:5px;
  border:1px solid currentColor}}
.chip b{{opacity:.65;font-weight:600}}
.chip.move{{color:var(--move)}} .chip.spd{{color:var(--spd)}}
.chip.cam{{color:var(--cam)}} .chip.oth{{color:var(--oth)}}
.variants{{margin-top:11px;font-size:12px;color:var(--mut)}}
.variants a{{color:var(--move);margin-right:10px}}
details{{margin-top:11px;font-size:12.5px}}
summary{{cursor:pointer;color:var(--mut)}}
details table{{margin-top:8px;width:100%;border-collapse:collapse;
  display:block;overflow-x:auto}}
details th{{text-align:left;color:var(--mut);font-weight:500;padding:3px 10px 3px 0;
  white-space:nowrap;vertical-align:top}}
details td{{font-family:ui-monospace,monospace;font-size:11.5px;word-break:break-all}}
</style>
</head>
<body>

<header>
  <h1>SimWorld 采集数据总览</h1>
  <p class="sub">{html.escape(maps_line)} · UnrealCV 9208</p>
  <div class="totals">
    <div>片段<b>{len(everything)}</b></div>
    <div>总帧数<b>{total_frames}</b></div>
    <div>总时长<b>{total_sec:.0f} 秒</b></div>
    <div>动作类别<b>{len(all_tags)}</b></div>
  </div>
  {warn_html}
</header>

<div class="bar">
  <span class="grp">排序</span>{sorts}
</div>

<div class="bar">
  <span class="grp">筛选</span>{all_btn}{filters}
</div>
{pager}

<div class="grid">{''.join(cards)}</div>
{pager}

{'' if paged else '''<script>
const grid=document.querySelector('.grid');
const cards=[...document.querySelectorAll('.card')];
const fBtns=[...document.querySelectorAll('.bar button[data-f]')];
const sBtns=[...document.querySelectorAll('.bar button[data-s]')];
const keys={
  new:  c => -Number(c.dataset.mtime||0),
  old:  c =>  Number(c.dataset.mtime||0),
  long: c => -Number(c.dataset.seconds||0),
};
function sortBy(mode){
  const k=keys[mode];
  const ordered=[...cards].sort(k
    ? (a,b)=>k(a)-k(b)
    : (a,b)=>(a.dataset.name||'').localeCompare(b.dataset.name||''));
  ordered.forEach(c=>grid.appendChild(c));
}
sBtns.forEach(b=>b.onclick=()=>{
  sBtns.forEach(x=>x.classList.toggle('on',x===b));
  sortBy(b.dataset.s);
});
fBtns.forEach(b=>b.onclick=()=>{
  fBtns.forEach(x=>x.classList.toggle('on',x===b));
  const f=b.dataset.f;
  cards.forEach(c=>{
    c.style.display = (f==='*' || (c.dataset.tags||'').split('|').includes(f)) ? '' : 'none';
  });
});
sortBy('new');
</script>'''}
</body>
</html>
"""


if __name__ == "__main__":
    clips = collect()
    GALLERY.mkdir(parents=True, exist_ok=True)
    (GALLERY / "index.html").write_text(render(clips), encoding="utf-8")
    print(f"clips: {len(clips)}")
    for c in clips:
        print(f"  {c['slug']}  {c['info']['frames']}f "
              f"{c['info']['seconds']:.1f}s  {c['behavior']}")
    print(f"wrote {GALLERY/'index.html'}")
