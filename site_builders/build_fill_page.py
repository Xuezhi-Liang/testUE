#!/usr/bin/env python3
"""/longvideo/fill/: the author's lighting kept, exposure pinned, their sky light multiplied.

    python3 build_fill_page.py <fill_test_out_dir> <current_episode_dir>
"""
import csv, html, json, os, shutil, sys
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2, numpy as np

SRC = Path(sys.argv[1]); CUR = Path(sys.argv[2])
SITE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/fill"); (SITE / "img").mkdir(parents=True, exist_ok=True)
test = json.loads((SRC / "test.json").read_text()); frozen = json.loads(Path(test["frozen"]).read_text()); fps = float(frozen["fps"])
variants = [("current", CUR, None, None)]
for tag, v in test["variants"].items():
    if v.get("episode"):
        variants.append((tag, Path(v["episode"]), v["factor"], v["chosen_ev"]))
label = {t: ("现状：关卡光照 + 自动曝光" if t == "current" else f"天光 ×{f:g} · 手动 {ev:+.1f} EV") for t, _, f, ev in variants}
COL = {"current": "#c2402f", "fill_x1": "#6b7280", "fill_x2": "#0b8a5f", "fill_x3": "#1e6fd9"}
luma = lambda im: 0.2126 * im[..., 2] + 0.7152 * im[..., 1] + 0.0722 * im[..., 0]


def frame_stats(d, step=3):
    n = sum(1 for _ in open(d / "frames.csv")) - 1; out = []
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


stats = {t: frame_stats(d) for t, d, _, _ in variants}
agg = {t: {"mean": float(np.mean([s["mean"] for s in st])), "p10": float(np.median([s["p10"] for s in st])), "p50": float(np.median([s["p50"] for s in st])),
           "p99": float(np.median([s["p99"] for s in st])), "blown": float(np.median([s["blown"] for s in st])), "blown_max": max(s["blown"] for s in st),
           "black": float(np.median([s["black"] for s in st])), "black_max": max(s["black"] for s in st),
           "spread": float(np.percentile([s["mean"] for s in st], 95) - np.percentile([s["mean"] for s in st], 5))} for t, st in stats.items()}
rows = list(csv.DictReader(open(CUR / "frames.csv"))); NF = len(rows)
P = np.array([(float(r["actual_x_cm"]), float(r["actual_y_cm"]), float(r["actual_yaw_deg"])) for r in rows]); gap = int(10 * fps); pairs = []
for b in range(gap + 1, NF, 2):
    c = np.arange(0, b - gap); dd = np.hypot(P[c, 0] - P[b, 0], P[c, 1] - P[b, 1]); dy = np.abs((P[c, 2] - P[b, 2] + 180) % 360 - 180)
    ok = np.where((dd < 60) & (dy < 10))[0]
    if len(ok): pairs.append((int(c[ok[np.argmin(dd[ok])]]), b))
if len(pairs) > 80: pairs = [pairs[i] for i in np.linspace(0, len(pairs) - 1, 80).round().astype(int)]
def pair_diffs(d):
    ds = [abs(float(luma(cv2.imread(str(d / "rgb" / f"{a:06d}.jpg"))).mean()) - float(luma(cv2.imread(str(d / "rgb" / f"{b:06d}.jpg"))).mean())) for a, b in pairs]
    return {"n": len(ds), "median": float(np.median(ds)), "p90": float(np.percentile(ds, 90)), "max": float(max(ds))}
pd = {t: pair_diffs(d) for t, d, _, _ in variants}

# probe grid: current probe vs each variant's chosen-EV probe at the same poses
def probe_frames(tag):
    if tag == "current": return {f["pose_index"]: f for f in test["probes_current"]}
    v = test["variants"][tag]; sw = json.loads((SRC / "sweep" / tag / "sweep.json").read_text())
    want = f"ev{v['chosen_ev']:+.1f}"
    return {f["pose_index"]: dict(f, dir=SRC / "sweep" / tag) for f in sw["frames"] if f["variant"] == want}
pf = {t: probe_frames(t) for t, _, _, _ in variants}
poses = sorted(pf["current"])[::2]
grid = ""
for k in poses:
    cells = ""
    for t, _, _, _ in variants:
        f = pf[t].get(k)
        if f:
            src = (f.get("dir") or (SRC / "probes")) / f["file"]; name = f"{t}_{f['file'].replace('.png', '.jpg')}"
            if thumb(src, SITE / "img" / name, 300):
                cells += f'<td><img src="img/{name}" loading="lazy"><div class="cap mono">p50 {f["p50"]} · 黑 {f["black_frac"]*100:.1f}% · 过曝 {f["blown_frac"]*100:.1f}%</div></td>'; continue
        cells += "<td></td>"
    grid += f'<tr><th class="mono">#{k}<br>第 {pf["current"][k]["frame"]} 帧</th>{cells}</tr>'
# biggest-difference frames between current and x2 (or the last variant)
ref = variants[-1][0] if len(variants) > 2 else variants[-1][0]
cm = {s["frame"]: s for s in stats["current"]}; rm = {s["frame"]: s for s in stats[ref]}; common = sorted(set(cm) & set(rm))
picks = [f for _, f in sorted(((abs(cm[f]["black"] - rm[f]["black"]), f) for f in common), reverse=True)[:3]] + [common[len(common) // 3], common[2 * len(common) // 3]]
pairs_html = ""
for f in picks:
    cells = ""
    for t, d, _, _ in variants:
        name = f"f{f:06d}_{t}.jpg"
        if thumb(d / "rgb" / f"{f:06d}.jpg", SITE / "img" / name, 420):
            s = next(x for x in stats[t] if x["frame"] == f)
            cells += f'<div><div class="lab">{label[t]}</div><img src="img/{name}" loading="lazy"><div class="cap mono">亮度 {s["mean"]:.0f} · 黑 {s["black"]*100:.1f}% · 过曝 {s["blown"]*100:.1f}%</div></div>'
    pairs_html += f'<div class="pair"><div class="pairhd">第 {f} 帧 · {f/fps:.1f} s</div><div class="pairrow" style="grid-template-columns:repeat({len(variants)},1fr)">{cells}</div></div>'
vids = {}
for t, d, _, _ in variants:
    for cand in ("rgb.mp4", "rgb_proxy.mp4"):
        if (d / cand).exists(): shutil.copy2(d / cand, SITE / f"{t}.mp4"); vids[t] = f"{t}.mp4"; break
def svg(key, ylim, w=1100, h=220):
    def path(st): return "M" + " L".join(f"{40 + s['frame']/(NF-1)*(w-60):.1f},{h-25-(min(max(s[key],ylim[0]),ylim[1])-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" for s in st)
    g = "".join(f'<line x1="40" x2="{w-20}" y1="{h-25-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" y2="{h-25-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" class="grid"/><text x="34" y="{h-22-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" class="tick" text-anchor="end">{v:g}</text>' for v in np.linspace(ylim[0], ylim[1], 6))
    lines = "".join(f'<path d="{path(st)}" fill="none" stroke="{COL.get(t, "#888")}" stroke-width="1.5"/>' for t, st in stats.items())
    xt = "".join(f'<text x="{40 + i/4*(w-60):.1f}" y="{h-8}" class="tick" text-anchor="middle">{i/4*NF/fps:.0f} s</text>' for i in range(5))
    return f'<svg viewBox="0 0 {w} {h}" class="chart">{g}{lines}{xt}</svg>'
sky = next((v["fill"]["skylights"] for v in test["variants"].values() if v.get("fill")), [])
sweep_rows = ""
for tag, v in test["variants"].items():
    for t in v["sweep"]:
        if t["ev"] is None: continue
        sweep_rows += f'<tr class="{"chosen" if t["ev"] == v["chosen_ev"] else ""}"><td>天光 ×{v["factor"]:g}</td><td class="mono">{t["ev"]:+.1f} EV</td><td class="r mono">{t["p10"]}</td><td class="r mono">{t["p50"]}</td><td class="r mono">{t["p99"]}</td><td class="r mono">{t["blown_frac"]*100:.2f}%</td><td class="r mono">{t["black_frac"]*100:.2f}%</td><td class="mut">{"<b>选中</b>" if t["ev"] == v["chosen_ev"] else ("在限内" if t["score"] is not None else "超限")}</td></tr>'
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>补天光测试 · Downtown West</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--cam:#8a5a0b;--oth:#6b7280;--bad:#c2402f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif;overflow-x:hidden}}
header,.wrap{{max-width:1500px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}h1{{margin:0 0 6px;font-size:23px;letter-spacing:-.01em}}h2{{margin:0;font-size:16.5px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a{{color:var(--move);text-decoration:none}}
.totals{{display:flex;gap:22px;flex-wrap:wrap;margin-top:16px;font-size:13px;color:var(--mut)}}.totals div b{{display:block;font-size:20px;color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}}
section{{margin-top:30px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}.sechd .note{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{color:var(--mut);font-weight:500;font-size:11.5px;white-space:nowrap}}td.r,th.r{{text-align:right}}td.mut{{color:var(--mut)}}
.mono{{font-family:ui-monospace,monospace;font-size:12px;font-variant-numeric:tabular-nums}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px}}.card p{{margin:0 0 8px}}.card p:last-child{{margin:0}}
tr.chosen td{{background:rgba(11,138,95,.10)}}.grid td img{{width:300px;max-width:100%;height:auto;display:block;border-radius:4px}}.cap{{color:var(--mut);font-size:10.5px;margin-top:2px}}.scroll{{overflow-x:auto}}
.chart{{width:100%;height:auto;display:block;background:var(--card);border:1px solid var(--line);border-radius:8px}}.chart .grid{{stroke:var(--line)}}.chart .tick{{fill:var(--mut);font-size:11px}}
.legend{{display:flex;gap:18px;font-size:13px;color:var(--mut);margin:6px 0 12px;flex-wrap:wrap}}.legend i{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px}}
.pair{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:14px}}.pairhd{{font-size:13px;color:var(--mut);margin-bottom:8px}}
.pairrow{{display:grid;gap:8px}}.pairrow img{{width:100%;height:auto;display:block;border-radius:4px}}.lab{{font-size:11.5px;color:var(--mut);margin-bottom:3px}}
.vids{{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:16px}}video{{width:100%;border-radius:8px;background:#000}}
</style></head><body>
<header><h1>补天光测试 · Downtown West</h1>
<p class="sub"><a href="../">← 长视频采集</a> · <a href="../lighting/">Dubai rig 版</a> · <a href="../exposure/">钉死曝光版</a> · 同一条冻结路线（{NF} 帧 / {NF/fps:.0f} s）录 {len(variants)} 遍。作者的太阳、天空、后期调色全部保留；只把关卡自己的天光改为实时捕获并乘以系数，采集相机钉手动曝光（偏移按每档扫描选出）</p>
<div class="totals">
 <div>天光原始强度<b>{", ".join(f"{s['base_intensity']:.2f}" for s in sky) or "?"}</b></div>
 {"".join(f'<div>{label[t]}<b>近黑 {agg[t]["black"]*100:.1f}% · 过曝 {agg[t]["blown"]*100:.2f}%</b></div>' for t, _, _, _ in variants)}
</div></header>
<div class="wrap">
<section><div class="sechd"><h2>结论</h2><span class="note">每 3 帧取一帧统计；同位姿对 = 位置差 &lt; 60 cm、朝向差 &lt; 10°、相隔 ≥ 10 s，共 {pd["current"]["n"]} 对</span></div>
<div class="scroll"><table><thead><tr><th>版本</th><th class="r">平均亮度</th><th class="r">p10</th><th class="r">p50</th><th class="r">p99</th><th class="r">近黑 中位 / 最坏帧</th><th class="r">过曝 中位 / 最坏帧</th><th class="r">亮度 5–95% 跨度</th><th class="r">同位姿两次亮度差 中位 / p90 / 最大</th></tr></thead><tbody>
{"".join(f'<tr><td>{label[t]}</td><td class="r mono">{a["mean"]:.0f}</td><td class="r mono">{a["p10"]:.0f}</td><td class="r mono">{a["p50"]:.0f}</td><td class="r mono">{a["p99"]:.0f}</td><td class="r mono">{a["black"]*100:.1f}% / {a["black_max"]*100:.1f}%</td><td class="r mono">{a["blown"]*100:.2f}% / {a["blown_max"]*100:.1f}%</td><td class="r mono">{a["spread"]:.0f}</td><td class="r mono">{pd[t]["median"]:.2f} / {pd[t]["p90"]:.1f} / {pd[t]["max"]:.1f}</td></tr>' for t, a in agg.items())}
</tbody></table></div>
<div class="card" style="margin-top:14px"><p>颜色和现状一致（同一套太阳、天空、调色），变化只在阴影的亮度和曝光的稳定性。天光 ×1 = 只钉曝光；×2、×3 = 阴影被天光抬起。每档的曝光偏移由扫描按"过曝 ≤5%、近黑 ≤20% 内两端丢失像素最少"选出，所以档与档之间整体亮度接近，差别集中在阴影。</p></div>
</section>
<section><div class="sechd"><h2>一、同一位姿，各版本</h2><span class="note">探针位姿每隔一个取一个；各档用其选中的曝光偏移渲染</span></div>
<div class="scroll"><table class="grid"><thead><tr><th>位姿</th>{"".join(f"<th>{label[t]}</th>" for t, _, _, _ in variants)}</tr></thead><tbody>{grid}</tbody></table></div></section>
<section><div class="sechd"><h2>二、整条路线</h2></div>
<div class="legend">{"".join(f'<span><i style="background:{COL.get(t, "#888")}"></i>{label[t]}</span>' for t, _, _, _ in variants)}</div>
<p class="sub" style="margin:0 0 6px">画面平均亮度 /255</p>{svg("mean", (0, 255))}
<p class="sub" style="margin:10px 0 6px">近黑像素比例（≤5），纵轴 0–50%</p>{svg("black", (0, 0.5))}
<p class="sub" style="margin:10px 0 6px">过曝像素比例（≥250），纵轴 0–20%</p>{svg("blown", (0, 0.2))}
</section>
<section><div class="sechd"><h2>三、阴影差别最大的帧</h2><span class="note">现状与 {label[ref]} 近黑比例差最大的 3 帧，加路线 1/3、2/3 处各一帧</span></div>{pairs_html}</section>
<section><div class="sechd"><h2>四、曝光扫描</h2><span class="note">每档 24 个位姿的中位数</span></div>
<div class="scroll"><table><thead><tr><th>档</th><th>曝光</th><th class="r">p10</th><th class="r">p50</th><th class="r">p99</th><th class="r">过曝</th><th class="r">近黑</th><th>判定</th></tr></thead><tbody>{sweep_rows}</tbody></table></div></section>
<section><div class="sechd"><h2>五、完整视频</h2></div><div class="vids">{"".join(f'<div><div class="lab">{label[t]}</div><video controls preload="metadata" src="{v}"></video></div>' for t, v in vids.items())}</div></section>
</div></body></html>'''
(SITE / "index.html").write_text(page)
json.dump({"agg": agg, "pairs": pd, "variants": {t: {"factor": f, "ev": ev} for t, _, f, ev in variants}}, open(SITE / "measurements.json", "w"), indent=1, default=float)
print("wrote", SITE / "index.html"); [print(t, {k: round(v, 3) for k, v in a.items()}, "pairs", {k: round(v, 2) for k, v in pd[t].items()}) for t, a in agg.items()]
