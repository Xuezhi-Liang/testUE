#!/usr/bin/env python3
"""First-N-frames depth video for one episode, beside its RGB clip, on one page.

    python3 depth_clip.py <episode_id> [--frames 14400]

Depth EXR: linear metres in R (OpenCV reads BGRA, so channel 2), -1 = invalid. Colourised on a
log scale 1-300 m with the turbo colormap; invalid pixels black. All-intra-agnostic here: this is
a REVIEW artifact (H.264, inter-coded), not data.
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2, numpy as np

EID = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 14400
SLUG = EID.split("__")[0]
B = f"s3://pan-simworld/ue-revist-long-video/{SLUG}/{EID}"
L = Path("/home/ubuntu/WM-Unreal-data-collection/local_run"); CLIPS = L / "site" / "longvideo" / "clips"
SITE = L / "site" / "longvideo" / "depth" / EID; SITE.mkdir(parents=True, exist_ok=True)
WORK = Path(os.environ.get("WORK", "/tmp/claude-1000/-home-ubuntu-UE5-Agent-Data/28decfdc-704b-4ad3-8ca3-e0225954f240/scratchpad/ops/depth_work")) / EID
FF = open(L / ".." / ".." / "UE5-Agent-Data" / "revisit_pipeline" / "ffmpeg_path.txt").read().strip() if (L / ".." / ".." / "UE5-Agent-Data" / "revisit_pipeline" / "ffmpeg_path.txt").exists() else "/opt/pytorch/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
ZMIN, ZMAX = 1.0, 300.0
FPS = 24

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

# 1. download depth 000000..N-1
(WORK / "depth").mkdir(parents=True, exist_ok=True)
have = len(list((WORK / "depth").glob("*.exr")))
if have < N:
    log(f"downloading {N} depth EXRs ({have} present)")
    inc = []
    for i in range(0, N, 1000):
        inc += ["--include", f"{i//1000:03d}???.exr"]
    subprocess.run(["aws", "s3", "cp", "--recursive", "--only-show-errors", f"{B}/depth/", str(WORK / "depth"), "--exclude", "*"] + inc, check=True)
    have = len(list((WORK / "depth").glob("*.exr"))); log(f"downloaded: {have} files")
missing = [i for i in range(N) if not (WORK / "depth" / f"{i:06d}.exr").exists()]
if missing:
    raise SystemExit(f"{len(missing)} depth frames missing in the first {N}, e.g. {missing[:5]} - refusing to build a video with holes")

# 2. colourise + encode via ffmpeg stdin
lut = cv2.applyColorMap(np.arange(256, dtype=np.uint8).reshape(1, 256), cv2.COLORMAP_TURBO)[0]  # 256x3 BGR
def colour(i):
    d = cv2.imread(str(WORK / "depth" / f"{i:06d}.exr"), cv2.IMREAD_UNCHANGED)
    z = d[..., 2] if d.ndim == 3 else d
    valid = z > 0
    t = np.zeros_like(z, dtype=np.float32)
    zz = np.clip(z, ZMIN, ZMAX)
    t[valid] = (np.log(zz[valid]) - np.log(ZMIN)) / (np.log(ZMAX) - np.log(ZMIN))
    idx = (t * 255).astype(np.uint8)
    img = lut[idx]; img[~valid] = 0
    return i, np.ascontiguousarray(img), float(valid.mean())
out_mp4 = SITE / "depth.mp4"; part = str(out_mp4) + ".part"
h, w = cv2.imread(str(WORK / "depth" / "000000.exr"), cv2.IMREAD_UNCHANGED).shape[:2]
ff = subprocess.Popen([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
                       "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", part], stdin=subprocess.PIPE)
log("colourising + encoding"); t0 = time.time(); valid_fracs = []
with ThreadPoolExecutor(8) as ex:
    for k in range(0, N, 256):
        for i, img, vf in sorted(ex.map(colour, range(k, min(N, k + 256))), key=lambda r: r[0]):
            ff.stdin.write(img.tobytes()); valid_fracs.append(vf)
        if k % 2048 == 0: log(f"  {min(N, k+256)}/{N}")
ff.stdin.close(); ff.wait(); assert ff.returncode == 0, "ffmpeg failed"
os.replace(part, out_mp4); log(f"depth.mp4 done in {time.time()-t0:.0f} s, {out_mp4.stat().st_size/1e6:.0f} MB")

# 3. side-by-side with the RGB clip (same frame index, same fps)
rgb = CLIPS / f"{EID}.mp4"
if rgb.exists():
    sbs = SITE / "rgb_depth_sbs.mp4"; p2 = str(sbs) + ".part"
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(rgb), "-i", str(out_mp4),
                    "-filter_complex", f"[0:v]trim=end_frame={N},setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]hstack=inputs=2",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", p2], check=True)
    os.replace(p2, sbs); log(f"side-by-side done, {sbs.stat().st_size/1e6:.0f} MB")
    os.symlink(rgb, SITE / "rgb.mp4") if not (SITE / "rgb.mp4").exists() else None

# 4. page
vf = np.array(valid_fracs)
map_label = SLUG.removeprefix("Game_"); seed = EID.split("seed")[1].split("__")[0]
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RGB + 深度 · ChemicalPlant_2 s02</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif}}
header,.wrap{{max-width:1500px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}h1{{margin:0 0 6px;font-size:23px}}h2{{margin:0;font-size:16.5px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a{{color:var(--move);text-decoration:none}}.mono{{font-family:ui-monospace,monospace;font-size:12px}}
section{{margin-top:28px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}.sechd .note{{color:var(--mut);font-size:12px}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}video{{width:100%;border-radius:8px;background:#000;display:block}}.lab{{font-size:12px;color:var(--mut);margin-bottom:4px}}
.bar{{height:14px;border-radius:4px;background:linear-gradient(90deg,#30123b,#4662d7,#36aaf9,#1ae4b6,#72fe5e,#c8ef34,#faba39,#f66b19,#ca2a04,#7a0403);margin-top:8px}}
.ticks{{display:flex;justify-content:space-between;font-size:11.5px;color:var(--mut);font-family:ui-monospace,monospace}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px;font-size:13.5px}}.card p{{margin:0 0 6px}}.card p:last-child{{margin:0}}
button{{font:inherit;font-size:13px;padding:5px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}}
</style></head><body>
<header><h1>RGB + 深度 · ChemicalPlant_2 s02</h1>
<p class="sub"><a href="../../">← 长视频采集</a> · {map_label} · 种子 {seed} · 前 {N} 帧 / {N/FPS/60:.0f} 分钟 · <span class="mono">{EID}</span></p></header>
<div class="wrap">
<section><div class="sechd"><h2>并排同步播放</h2><span class="note">同一帧号，左 RGB 右深度；一个视频文件，天然对齐</span></div>
<video controls preload="metadata" src="rgb_depth_sbs.mp4"></video></section>
<section><div class="sechd"><h2>分开两路（联动播放）</h2><span class="note">按下面按钮两路同时播放/暂停/拖动</span></div>
<div style="margin-bottom:8px"><button id="play">播放 / 暂停</button> <button id="sync">对齐到左侧进度</button></div>
<div class="two"><div><div class="lab">RGB（交付的 JPEG 重编码）</div><video id="a" preload="metadata" src="rgb.mp4" controls></video></div>
<div><div class="lab">深度（EXR R 通道，线性米，对数着色）</div><video id="b" preload="metadata" src="depth.mp4" controls></video></div></div>
<div class="bar"></div><div class="ticks"><span>1 m</span><span>3 m</span><span>10 m</span><span>30 m</span><span>100 m</span><span>300 m</span></div>
<p class="sub" style="margin-top:6px">黑色 = 无效深度（天空、超出 1000 m 量程），写入值为 −1。着色为对数刻度：1 m 深紫，300 m 深红。</p></section>
<section><div class="sechd"><h2>这段深度的统计</h2></div><div class="card">
<p>有效像素比例：中位 {np.median(vf)*100:.1f}%，最低帧 {vf.min()*100:.1f}%，最高帧 {vf.max()*100:.1f}%。其余是天空。</p>
<p>深度文件：float16 EXR、单通道、单位米、平面深度（非径向）；每帧与同帧号的 RGB 在同一引擎 tick 内读出，对齐是结构性的。视频只是回看用的有损编码，数据以 S3 上的逐帧文件为准。</p></div></section>
</div>
<script>
const a=document.getElementById('a'),b=document.getElementById('b');
document.getElementById('play').onclick=()=>{{if(a.paused){{b.currentTime=a.currentTime;a.play();b.play();}}else{{a.pause();b.pause();}}}};
document.getElementById('sync').onclick=()=>{{b.currentTime=a.currentTime;}};
a.addEventListener('seeked',()=>{{b.currentTime=a.currentTime;}});
a.addEventListener('pause',()=>b.pause());a.addEventListener('play',()=>{{b.currentTime=a.currentTime;b.play();}});
</script></body></html>'''
(SITE / "index.html").write_text(page)
json.dump({"episode_id": EID, "frames": N, "valid_fraction_median": float(np.median(vf)), "valid_min": float(vf.min()), "valid_max": float(vf.max()), "zmin_m": ZMIN, "zmax_m": ZMAX}, open(SITE / "depth_meta.json", "w"), indent=1)
log("wrote", SITE / "index.html"); print("DEPTH CLIP DONE")
