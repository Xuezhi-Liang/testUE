#!/usr/bin/env python3
"""/longvideo/flicker/: frame-to-frame shimmer on static-camera frames, per anti-aliasing setting.
    python3 build_flicker_page.py <baseline_episode_dir> <flicker_root>   (flicker_root/<tag>/asis/<ep>/)
"""
import csv, json, os, shutil, subprocess, sys
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2, numpy as np

BASE = Path(sys.argv[1]); ROOT = Path(sys.argv[2])
SITE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/flicker"); (SITE / "img").mkdir(parents=True, exist_ok=True)
FF = "/opt/pytorch/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
variants = [("baseline", "现状：TSR（时域抗锯齿，按需渲染无历史）", BASE)]
LABEL = {"fxaa": "FXAA（逐帧独立抗锯齿）", "noaa": "关闭抗锯齿", "tsr_nojitter": "TSR · 历史 100% · TAA 质量 0",
         "rtshadow_off": "关光追阴影", "lumen_off": "关 Lumen（全局光 + 反射）", "vsm_off": "关虚拟阴影图", "lumen_rtshadow_off": "关 Lumen + 关光追阴影", "camcut": "每帧标记相机切换（丢历史）", "nopersist": "不保留渲染状态（无 GI，阴影全黑，弃）", "nopersist_fixed": "不保留状态 + 固定曝光", "persist_fixed": "保留状态 + 固定曝光 + 局部曝光", "fixedjitter": "钉住时域抖动序列（TAA 1 样本、Lumen 固定抖动索引）", "fixedjitter_novsm": "钉住抖动 + 关虚拟阴影图", "fresh_state": "每帧新建采集组件（探针同款，含 GI）", "fresh_1x": "新默认：每帧新建状态 + 自动曝光 + 天光 ×4 + 局部曝光（无超采样）", "fresh_ss2": "新默认 + 2× 超采样", "fresh_ss3": "新默认 + 3× 超采样"}
for tag in LABEL:
    d = next(iter((ROOT / tag / "asis").glob("*/")), None) if (ROOT / tag / "asis").exists() else None
    if d and (d / "frames.csv").exists(): variants.append((tag, LABEL[tag], d))
luma = lambda im: 0.2126 * im[..., 2] + 0.7152 * im[..., 1] + 0.0722 * im[..., 0]


def static_pairs(d):
    rows = list(csv.DictReader(open(d / "frames.csv")))
    idx = [int(r["frame_id"]) for r in rows if float(r["step_cm"]) < 0.01 and abs(float(r["yaw_rate_deg_s"])) < 0.01]
    S = set(idx); return [i for i in idx if i + 1 in S]


def measure(d, pairs, maxn=200):
    ds, hi, sharp = [], [], []
    for i in pairs[:maxn]:
        a = cv2.imread(str(d / "rgb" / f"{i:06d}.jpg")); b = cv2.imread(str(d / "rgb" / f"{i+1:06d}.jpg"))
        if a is None or b is None: continue
        diff = np.abs(a.astype(np.float32) - b.astype(np.float32)).mean(axis=2)
        ds.append(float(diff.mean())); hi.append(float((diff > 20).mean()))
        sharp.append(float(cv2.Laplacian(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()))
    return {"n": len(ds), "diff_median": float(np.median(ds)), "diff_p90": float(np.percentile(ds, 90)), "sparkle_pct": float(np.median(hi)) * 100,
            "sharpness": float(np.median(sharp))}


exec(open("/home/ubuntu/WM-Unreal-data-collection/local_run/motion_shimmer.py").read().split('if __name__')[0])   # metric()

def moving_measure(d, start=600, n=300):
    ds = []
    for i in range(start, start + n):
        a = cv2.imread(str(d / "rgb" / f"{i:06d}.jpg")); b = cv2.imread(str(d / "rgb" / f"{i+1:06d}.jpg"))
        if a is None or b is None: break
        ds.append(float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()))
    return float(np.median(ds))


pairs = static_pairs(BASE)
res = {}; rows_html = ""; media = ""
# a static stretch to loop: the longest run of consecutive static frames
runs, cur = [], [pairs[0]] if pairs else []
for i in pairs[1:]:
    if i == cur[-1] + 1: cur.append(i)
    else: runs.append(cur); cur = [i]
if cur: runs.append(cur)
seg = max(runs, key=len) if runs else []
seg = seg[:72]
for tag, label, d in variants:
    m = measure(d, pairs); m["moving_diff"] = moving_measure(d)
    mm = metric(str(d), n_moving=60, n_static=10); m["motion_residual"] = mm["motion_residual_mean"]; m["motion_sparkle"] = mm["motion_sparkle_pct"]; res[tag] = m
    # heatmap of one static pair + the frame
    i = seg[len(seg) // 2] if seg else pairs[0]
    a = cv2.imread(str(d / "rgb" / f"{i:06d}.jpg")); b = cv2.imread(str(d / "rgb" / f"{i+1:06d}.jpg"))
    diff = np.abs(a.astype(np.float32) - b.astype(np.float32)).mean(axis=2)
    heat = cv2.applyColorMap(np.clip(diff * 8, 0, 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    cv2.imwrite(str(SITE / "img" / f"{tag}_frame.jpg"), cv2.resize(a, (640, 360)), [cv2.IMWRITE_JPEG_QUALITY, 90])
    cv2.imwrite(str(SITE / "img" / f"{tag}_diff.jpg"), cv2.resize(heat, (640, 360)), [cv2.IMWRITE_JPEG_QUALITY, 90])
    # a 2x-magnified crop of the static stretch, looped, so the shimmer is visible in a browser
    if seg:
        crop_dir = SITE / "img" / f"{tag}_crop"; crop_dir.mkdir(exist_ok=True)
        h, w = a.shape[:2]; y0, x0 = h // 2 - 90, w // 2 - 160
        for k, f in enumerate(seg):
            im = cv2.imread(str(d / "rgb" / f"{f:06d}.jpg"))[y0:y0 + 180, x0:x0 + 320]
            cv2.imwrite(str(crop_dir / f"{k:04d}.png"), cv2.resize(im, (640, 360), interpolation=cv2.INTER_NEAREST))
        out = SITE / f"{tag}_static_crop.mp4"
        subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-framerate", "24", "-i", str(crop_dir / "%04d.png"), "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(out) + ".part"], check=True); os.replace(str(out) + ".part", out)
        shutil.rmtree(crop_dir)
    rows_html += f'<tr><td>{label}</td><td class="r mono">{m["diff_median"]:.2f}</td><td class="r mono">{m["diff_p90"]:.2f}</td><td class="r mono">{m["sparkle_pct"]:.2f}%</td><td class="r mono"><b>{m["motion_residual"]:.2f}</b></td><td class="r mono"><b>{m["motion_sparkle"]:.2f}%</b></td><td class="r mono">{m["sharpness"]:.0f}</td></tr>'
    media += f'''<div class="card"><h3>{label}</h3><div class="three"><div><div class="lab">静止帧</div><img src="img/{tag}_frame.jpg"></div><div><div class="lab">与下一帧的差 ×8</div><img src="img/{tag}_diff.jpg"></div><div><div class="lab">静止 {len(seg)} 帧的中心 320×180 放大 2 倍，循环播放</div><video autoplay muted loop playsinline src="{tag}_static_crop.mp4"></video></div></div></div>'''
base = res["baseline"]
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>渲染抖动 · 抗锯齿对比</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--bad:#c2402f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--bad:#ff8272}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--bad:#ff8272}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif}}
header,.wrap{{max-width:1400px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}h1{{margin:0 0 6px;font-size:23px}}h2{{margin:0;font-size:16.5px}}h3{{margin:0 0 8px;font-size:15px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a{{color:var(--move);text-decoration:none}}.mono{{font-family:ui-monospace,monospace;font-size:12px;font-variant-numeric:tabular-nums}}
section{{margin-top:28px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}.sechd .note{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line)}}thead th{{color:var(--mut);font-weight:500;font-size:11.5px}}td.r,th.r{{text-align:right}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px;margin-bottom:14px}}.card p{{margin:0 0 6px}}
.three{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}}.three img,.three video{{width:100%;height:auto;display:block;border-radius:4px;background:#000}}.lab{{font-size:11.5px;color:var(--mut);margin-bottom:3px}}
</style></head><body>
<header><h1>渲染抖动 · 抗锯齿对比</h1>
<p class="sub"><a href="../">← 长视频采集</a> · Hwaseong 同一条路线，关卡光照 + 自动曝光不变，只改抗锯齿方式。尺子：相机<b>完全静止</b>的相邻两帧逐像素差（相机不动、光照不变，任何差都是渲染抖动），共 {len(pairs)} 对静止帧，取前 200 对</p>
</header><div class="wrap">
<section><div class="sechd"><h2>结果</h2><span class="note">静止列：相机不动时相邻帧的逐像素差。运动列：相机移动时，用光流把下一帧对齐回当前帧后剩下的差——真实运动被补偿掉，剩下的就是闪（纹理爬行、噪声）；"闪点"= 变化超过 20/255 的像素比例。清晰度 = 拉普拉斯方差</span></div>
<div class="scroll"><table><thead><tr><th>设置</th><th class="r">静止帧逐像素差 中位</th><th class="r">p90</th><th class="r">静止闪点</th><th class="r">运动补偿残差（相机移动时的闪）</th><th class="r">运动闪点</th><th class="r">清晰度</th></tr></thead><tbody>{rows_html}</tbody></table></div>
<div class="card" style="margin-top:12px"><p>现状 {base["diff_median"]:.2f}/255。换 FXAA、改 TSR 参数完全不变，彻底关抗锯齿反而略增（画面更锐，噪点更显）——所以闪的不是抗锯齿抖动，是时域降噪的效果（Lumen 全局光/反射、光追阴影、虚拟阴影图）每帧没有收敛。同一位姿用全新视图状态重渲 6 次，帧间差为 0.00：抖动只出现在带历史状态的连续录制里。下面几行是逐个关掉这些效果后的录制。这个抖动和曝光方案无关，四种曝光录法里都是这个量级，已录的数据也带着它。</p></div></section>
<section><div class="sechd"><h2>看图</h2><span class="note">每组：静止帧 · 与下一帧的差（放大 8 倍，越亮变化越大）· 中心区域放大循环播放</span></div>{media}</section>
</div></body></html>'''
(SITE / "index.html.part").write_text(page); os.replace(SITE / "index.html.part", SITE / "index.html")
json.dump(res, open(SITE / "measurements.json", "w"), indent=1)
print("wrote", SITE / "index.html"); [print(t, {k: round(v, 3) for k, v in m.items()}) for t, m in res.items()]
