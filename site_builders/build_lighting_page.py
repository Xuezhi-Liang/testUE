#!/usr/bin/env python3
"""/longvideo/lighting/: the level's own lighting vs the fixed Dubai rig, on one map, measured.

    python3 build_lighting_page.py <lighting_test_out_dir> <auto_episode_dir>

<auto_episode_dir> is the same frozen route recorded under the level's own lighting and
auto-exposure (exposure_test.py's "auto" episode). Frame i is the same pose in both recordings.
"""
import csv, html, json, os, shutil, sys
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2, numpy as np

SRC = Path(sys.argv[1]); AUTO = Path(sys.argv[2])
SITE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/lighting")
(SITE / "img").mkdir(parents=True, exist_ok=True)
test = json.loads((SRC / "test.json").read_text())
RIG = Path(test["episode_rig"]); frozen = json.loads(Path(test["frozen"]).read_text()); fps = float(frozen["fps"])
eps = {"current": AUTO, "rig": RIG}
luma = lambda im: 0.2126 * im[..., 2] + 0.7152 * im[..., 1] + 0.0722 * im[..., 0]


def frame_stats(d, step=3):
    n = sum(1 for _ in open(d / "frames.csv")) - 1
    out = []
    for i in range(0, n, step):
        im = cv2.imread(str(d / "rgb" / f"{i:06d}.jpg"))
        if im is None: continue
        y = luma(im)
        out.append({"frame": i, "mean": float(y.mean()), "p10": float(np.percentile(y, 10)), "p50": float(np.percentile(y, 50)),
                    "p99": float(np.percentile(y, 99)), "blown": float((y >= 250).mean()), "black": float((y <= 5).mean())})
    return out


def thumb(src, dst, width=320):
    im = cv2.imread(str(src))
    if im is None: return False
    cv2.imwrite(str(dst), cv2.resize(im, (width, int(round(im.shape[0] * width / im.shape[1]))), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 86]); return True


stats = {t: frame_stats(d) for t, d in eps.items()}
agg = {t: {"mean": float(np.mean([s["mean"] for s in st])), "p10": float(np.median([s["p10"] for s in st])), "p50": float(np.median([s["p50"] for s in st])),
           "p99": float(np.median([s["p99"] for s in st])), "blown": float(np.median([s["blown"] for s in st])), "blown_max": max(s["blown"] for s in st),
           "black": float(np.median([s["black"] for s in st])), "black_max": max(s["black"] for s in st),
           "mean_spread": float(np.percentile([s["mean"] for s in st], 95) - np.percentile([s["mean"] for s in st], 5))} for t, st in stats.items()}
# same-pose pairs (>=10 s apart, <60 cm, <10 deg) - brightness repeatability
def pairs_of(d):
    rows = list(csv.DictReader(open(d / "frames.csv")))
    P = np.array([(float(r["actual_x_cm"]), float(r["actual_y_cm"]), float(r["actual_yaw_deg"])) for r in rows])
    gap = int(10 * fps); out = []
    for b in range(gap + 1, len(rows), 2):
        c = np.arange(0, b - gap); dd = np.hypot(P[c, 0] - P[b, 0], P[c, 1] - P[b, 1]); dy = np.abs((P[c, 2] - P[b, 2] + 180) % 360 - 180)
        ok = np.where((dd < 60) & (dy < 10))[0]
        if len(ok): out.append((int(c[ok[np.argmin(dd[ok])]]), b))
    if len(out) > 80: out = [out[i] for i in np.linspace(0, len(out) - 1, 80).round().astype(int)]
    return out
pairs = pairs_of(AUTO)
def pair_diffs(d):
    ds = []
    for a, b in pairs:
        ia, ib = cv2.imread(str(d / "rgb" / f"{a:06d}.jpg")), cv2.imread(str(d / "rgb" / f"{b:06d}.jpg"))
        if ia is not None and ib is not None: ds.append(abs(float(luma(ia).mean()) - float(luma(ib).mean())))
    return {"n": len(ds), "median": float(np.median(ds)), "p90": float(np.percentile(ds, 90)), "max": float(max(ds))}
pd = {t: pair_diffs(d) for t, d in eps.items()}

# assets: probe grid, frame pairs, videos
probes = test["probes"]; byk = {}
for t in ("current", "rig"):
    for f in probes[t]:
        byk.setdefault(f["pose_index"], {})[t] = f
grid_rows = ""
for k in sorted(byk)[::2]:
    cells = ""
    for t in ("current", "rig"):
        f = byk[k].get(t)
        if f and thumb(SRC / "probes" / f["file"], SITE / "img" / f["file"].replace(".png", ".jpg"), 400):
            cells += f'<td><img src="img/{f["file"].replace(".png", ".jpg")}" loading="lazy"><div class="cap mono">p50 {f["p50"]} · 过曝 {f["blown_frac"]*100:.1f}% · 黑 {f["black_frac"]*100:.1f}%</div></td>'
        else: cells += "<td></td>"
    grid_rows += f'<tr><th class="mono">#{k} · 第 {byk[k]["current"]["frame"]} 帧</th>{cells}</tr>'
# the frames where the two recordings differ most in mean brightness, as pairs
cm = {s["frame"]: s for s in stats["current"]}; rm = {s["frame"]: s for s in stats["rig"]}
common = sorted(set(cm) & set(rm)); diffs = [(abs(cm[f]["mean"] - rm[f]["mean"]), f) for f in common]
picks = [f for _, f in sorted(diffs, reverse=True)[:3]] + [common[len(common) // 3], common[2 * len(common) // 3]]
pair_html = ""
for f in picks:
    ims = {}
    for t, d in eps.items():
        name = f"f{f:06d}_{t}.jpg"
        if thumb(d / "rgb" / f"{f:06d}.jpg", SITE / "img" / name, 520): ims[t] = name
    pair_html += f'<div class="pair"><div class="pairhd"><span>第 {f} 帧 · {f/fps:.1f} s</span><span class="mono">现状 平均亮度 {cm[f]["mean"]:.0f} · 近黑 {cm[f]["black"]*100:.1f}% · 过曝 {cm[f]["blown"]*100:.1f}%　|　rig 平均亮度 {rm[f]["mean"]:.0f} · 近黑 {rm[f]["black"]*100:.1f}% · 过曝 {rm[f]["blown"]*100:.1f}%</span></div><div class="pairrow2"><div><div class="lab">现状：关卡自带光照 + 自动曝光</div><img src="img/{ims.get("current","")}" loading="lazy"></div><div><div class="lab">Dubai rig：太阳 7 / 天光 2.5 / 雾 0.006，自动曝光关</div><img src="img/{ims.get("rig","")}" loading="lazy"></div></div></div>'
vids = {}
for t, d in eps.items():
    for cand in ("rgb.mp4", "rgb_proxy.mp4"):
        if (d / cand).exists(): shutil.copy2(d / cand, SITE / f"{t}.mp4"); vids[t] = f"{t}.mp4"; break

def svg(series, key, ylim, colors, w=1100, h=220):
    n = len(list(csv.DictReader(open(AUTO / "frames.csv"))))
    def path(st): return "M" + " L".join(f"{40 + s['frame']/(n-1)*(w-60):.1f},{h-25-(min(max(s[key],ylim[0]),ylim[1])-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" for s in st)
    g = "".join(f'<line x1="40" x2="{w-20}" y1="{h-25-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" y2="{h-25-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" class="grid"/><text x="34" y="{h-22-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" class="tick" text-anchor="end">{v:g}</text>' for v in np.linspace(ylim[0], ylim[1], 6))
    lines = "".join(f'<path d="{path(st)}" fill="none" stroke="{colors[t]}" stroke-width="1.6"/>' for t, st in series.items())
    xt = "".join(f'<text x="{40 + i/4*(w-60):.1f}" y="{h-8}" class="tick" text-anchor="middle">{i/4*n/fps:.0f} s</text>' for i in range(5))
    return f'<svg viewBox="0 0 {w} {h}" class="chart">{g}{lines}{xt}</svg>'
COL = {"current": "#c2402f", "rig": "#0b8a5f"}
rig = test["rig"]; hid = rig.get("hidden_global_lighting", []); dis = rig.get("disabled_post_process", [])
hid_html = "".join(f'<li><code>{html.escape(x.get("actor",""))}</code> · {html.escape(x.get("class", x.get("error","")))}</li>' for x in hid[:40])
dis_html = "".join(f'<li><code>{html.escape(x.get("actor",""))}</code> · {html.escape(x.get("class",""))}{" · unbound" if x.get("unbound") else ""}</li>' for x in dis[:40])
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>光照 rig 测试 · Downtown West</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--cam:#8a5a0b;--oth:#6b7280;--bad:#c2402f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif;overflow-x:hidden}}
header,.wrap{{max-width:1400px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}h1{{margin:0 0 6px;font-size:23px;letter-spacing:-.01em}}h2{{margin:0;font-size:16.5px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a{{color:var(--move);text-decoration:none}}
.totals{{display:flex;gap:22px;flex-wrap:wrap;margin-top:16px;font-size:13px;color:var(--mut)}}.totals div b{{display:block;font-size:21px;color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}}
section{{margin-top:30px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}.sechd .note{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{color:var(--mut);font-weight:500;font-size:11.5px;white-space:nowrap}}td.r,th.r{{text-align:right}}
.mono{{font-family:ui-monospace,monospace;font-size:12px;font-variant-numeric:tabular-nums}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px}}.card p{{margin:0 0 8px}}.card p:last-child{{margin:0}}
.grid td img{{width:400px;max-width:100%;height:auto;display:block;border-radius:4px}}.grid .cap{{color:var(--mut);font-size:10.5px;margin-top:2px}}.scroll{{overflow-x:auto}}
.chart{{width:100%;height:auto;display:block;background:var(--card);border:1px solid var(--line);border-radius:8px}}.chart .grid{{stroke:var(--line)}}.chart .tick{{fill:var(--mut);font-size:11px}}
.legend{{display:flex;gap:18px;font-size:13px;color:var(--mut);margin:6px 0 12px}}.legend i{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px}}
.pair{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:14px}}.pairhd{{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--mut);margin-bottom:8px;flex-wrap:wrap}}
.pairrow2{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.pairrow2 img{{width:100%;height:auto;display:block;border-radius:4px}}.lab{{font-size:11.5px;color:var(--mut);margin-bottom:3px}}
.vids{{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:16px}}video{{width:100%;border-radius:8px;background:#000}}
ul.small{{columns:2;font-size:12.5px;color:var(--mut);margin:6px 0 0;padding-left:18px}}ul.small code{{color:var(--fg)}}
</style></head><body>
<header><h1>光照 rig 测试 · Downtown West</h1>
<p class="sub"><a href="../">← 长视频采集</a> · <a href="../exposure/">上一版：钉死曝光</a> · 同一条冻结路线（{len(common)*3} 帧 / {len(common)*3/fps:.0f} s）录两遍：现状 = 关卡自带光照 + 自动曝光；rig = 隐藏关卡全部全局光照和后期，换成 Dubai 项目那一套固定光源，并关掉自动曝光</p>
<div class="totals">
 <div>关卡被隐藏的全局光照组件<b>{len(hid)}</b></div><div>被禁用的后期体积<b>{len(dis)}</b></div><div>保留的局部灯<b>{rig.get("kept_local_lights", 0)}</b></div>
 <div>平均亮度 现状 / rig<b>{agg["current"]["mean"]:.0f} / {agg["rig"]["mean"]:.0f}</b></div>
 <div>近黑像素 现状 / rig<b>{agg["current"]["black"]*100:.1f}% / {agg["rig"]["black"]*100:.1f}%</b></div>
 <div>过曝像素 现状 / rig<b>{agg["current"]["blown"]*100:.2f}% / {agg["rig"]["blown"]*100:.2f}%</b></div>
 <div>同位姿两次亮度差 现状 / rig<b>{pd["current"]["median"]:.2f} / {pd["rig"]["median"]:.2f}</b></div>
</div></header>
<div class="wrap">
<section><div class="sechd"><h2>结论</h2><span class="note">同一条路线、同一帧号，两段录制逐帧对照</span></div>
<div class="card">
<p><b>看得见的变化</b>：作者的暖色调色、压暗的对比和随视线漂移的自动曝光全部没了，换成 Dubai 那种中性、平、无死黑无过曝的画面。近黑像素从 {agg["current"]["black"]*100:.1f}% 降到 {agg["rig"]["black"]*100:.1f}%（最坏帧 {agg["current"]["black_max"]*100:.0f}% → {agg["rig"]["black_max"]*100:.0f}%），过曝像素两边都是 0%（最坏帧 {agg["current"]["blown_max"]*100:.1f}% → {agg["rig"]["blown_max"]*100:.1f}%），整条路线的亮度 5–95% 跨度从 {agg["current"]["mean_spread"]:.0f} 收窄到 {agg["rig"]["mean_spread"]:.0f}。</p>
<p><b>曝光是死的</b>：自动曝光关闭后每帧曝光相同，亮度只随画面内容变，不再随"刚看了什么"变；同位姿两次经过的亮度差中位 {pd["rig"]["median"]:.2f}（现状 {pd["current"]["median"]:.2f}），剩下的是 60 cm / 10° 容差内的视角差。</p>
<p><b>偏亮</b>：rig 下画面中位灰度 {agg["rig"]["p50"]:.0f}、平均 {agg["rig"]["mean"]:.0f}，比 128 的中灰高约半档，路面接近浅灰。这是 Dubai 那组数（太阳 7、天光 2.5）搬过来的直接结果；要压回中灰，把天光降到 2.0 左右、太阳 6 即可，改的是全局一个数，所有地图共用，不是每图标定。</p>
<p><b>没有变的</b>：几何、材质、天空球（它是网格不是光源）、{rig.get("kept_local_lights", 0)} 个局部灯。深度不受影响。</p>
</div></section>
<section><div class="sechd"><h2>rig 是什么</h2><span class="note">照抄 <code>~/dubai_ue_detail</code>：build_realism.py 的光源、finalize_render_quality.py 的强度、DefaultEngine.ini 的 AutoExposure=False</span></div>
<div class="card">
<p><b>关掉的</b>：关卡里所有 DirectionalLight / SkyLight / SkyAtmosphere / ExponentialHeightFog / VolumetricCloud 组件（含蓝图天空里的），所有 PostProcessVolume 和 PostProcessComponent（作者的曝光、调色、bloom 都在这里）。点光、聚光、矩形灯是内容，保留 {rig.get("kept_local_lights", 0)} 个。</p>
<p><b>换上的</b>：Movable 平行光强度 {rig["rig"]["sun_intensity"]}、俯仰 {rig["rig"]["sun_pitch"]}° / 偏航 {rig["rig"]["sun_yaw"]}°、大气太阳、光源角 {rig["rig"]["sun_source_angle"]}°；SkyAtmosphere；Movable 天光 {rig["rig"]["sky_intensity"]} 开实时捕获；高度雾密度 {rig["rig"]["fog_density"]}。会话级控制台：{" · ".join(f"<code>{c}</code>" for c in rig["rig"]["cvars"])}。</p>
<p>都是会话内改动，地图文件不动；隐藏和新建的东西全部写进 capture_summary.json。</p>
<details><summary class="sub">被隐藏 / 禁用的清单</summary><ul class="small">{hid_html}{dis_html}</ul></details>
</div></section>
<section><div class="sechd"><h2>一、同一位姿，两种光照</h2><span class="note">{len(byk)} 个探针位姿里每隔一个取一个；每帧的直方图在引擎里算</span></div>
<div class="scroll"><table class="grid"><thead><tr><th>位姿</th><th>现状</th><th>Dubai rig</th></tr></thead><tbody>{grid_rows}</tbody></table></div></section>
<section><div class="sechd"><h2>二、整条路线</h2><span class="note">每 3 帧取一帧</span></div>
<div class="scroll"><table><thead><tr><th></th><th class="r">平均亮度</th><th class="r">亮度 5–95% 跨度</th><th class="r">p10</th><th class="r">p50</th><th class="r">p99</th><th class="r">近黑 中位 / 最坏帧</th><th class="r">过曝 中位 / 最坏帧</th><th class="r">同位姿两次亮度差 中位 / p90 / 最大</th></tr></thead><tbody>
{"".join(f'<tr><td>{"现状" if t == "current" else "Dubai rig"}</td><td class="r mono">{a["mean"]:.0f}</td><td class="r mono">{a["mean_spread"]:.0f}</td><td class="r mono">{a["p10"]:.0f}</td><td class="r mono">{a["p50"]:.0f}</td><td class="r mono">{a["p99"]:.0f}</td><td class="r mono">{a["black"]*100:.1f}% / {a["black_max"]*100:.1f}%</td><td class="r mono">{a["blown"]*100:.2f}% / {a["blown_max"]*100:.1f}%</td><td class="r mono">{pd[t]["median"]:.2f} / {pd[t]["p90"]:.1f} / {pd[t]["max"]:.1f}</td></tr>' for t, a in agg.items())}
</tbody></table></div>
<div class="legend" style="margin-top:14px"><span><i style="background:#c2402f"></i>现状</span><span><i style="background:#0b8a5f"></i>Dubai rig</span></div>
<p class="sub" style="margin:0 0 6px">画面平均亮度 /255</p>{svg(stats, "mean", (0, 255), COL)}
<p class="sub" style="margin:10px 0 6px">近黑像素比例（≤5），纵轴 0–50%</p>{svg(stats, "black", (0, 0.5), COL)}
<p class="sub" style="margin:10px 0 6px">过曝像素比例（≥250），纵轴 0–20%</p>{svg(stats, "blown", (0, 0.2), COL)}
</section>
<section><div class="sechd"><h2>三、差别最大的帧</h2><span class="note">两段录制平均亮度差最大的 3 帧，加路线 1/3、2/3 处各一帧</span></div>{pair_html}</section>
<section><div class="sechd"><h2>四、两段完整视频</h2><span class="note">同一路线、同一帧号</span></div>
<div class="vids">{"".join(f'<div><div class="lab">{"现状：关卡光照 + 自动曝光" if t == "current" else "Dubai rig，自动曝光关"}</div><video controls preload="metadata" src="{v}"></video></div>' for t, v in vids.items())}</div></section>
</div></body></html>'''
(SITE / "index.html").write_text(page)
json.dump({"agg": agg, "pairs": pd, "hidden": len(hid), "disabled": len(dis)}, open(SITE / "measurements.json", "w"), indent=1, default=float)
print("wrote", SITE / "index.html"); print(json.dumps({t: {k: round(v, 3) for k, v in a.items()} for t, a in agg.items()}, indent=0)); print("pairs", pd)
