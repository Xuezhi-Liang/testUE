#!/usr/bin/env python3
"""/longvideo/exposure/: what pinning the exposure does to one map, measured.

Reads an exposure_test.py output directory (sweep/ + auto/ + manual_ev*/) and writes a static
page: the bias sweep (poses x EV thumbnails and the histogram table), the two recordings' brightness
over time, the same-pose pairs that make auto-exposure's path dependence visible, and both review
videos. Same tokens as the parent page.

    python3 build_exposure_page.py <test_out_dir> [<site_subdir>]
"""
import csv, html, json, math, os, shutil, sys
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2
import numpy as np

SRC = Path(sys.argv[1])
SITE = Path(sys.argv[2] if len(sys.argv) > 2 else "/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/exposure")
(SITE / "img").mkdir(parents=True, exist_ok=True)

test = json.loads((SRC / "test.json").read_text())
sweep = json.loads((SRC / "sweep" / "sweep.json").read_text())
eps = {}
for tag, info in test["episodes"].items():
    d = Path(info["dir"])
    eps["auto" if tag == "auto" else "manual"] = d
frozen = json.loads(Path(test["frozen"]).read_text())
fps = float(frozen["fps"])
map_label = test["map_id"].removeprefix("/Game/")


def luma(img):
    return 0.2126 * img[..., 2] + 0.7152 * img[..., 1] + 0.0722 * img[..., 0]


def read_rows(d):
    with open(d / "frames.csv") as fh:
        return list(csv.DictReader(fh))


def frame_stats(d, step=3):
    rows = read_rows(d)
    out = []
    for i in range(0, len(rows), step):
        img = cv2.imread(str(d / "rgb" / f"{i:06d}.jpg"))
        if img is None:
            continue
        y = luma(img)
        out.append({"frame": i, "mean": float(y.mean()), "p10": float(np.percentile(y, 10)),
                    "p50": float(np.percentile(y, 50)), "blown": float((y >= 250).mean()),
                    "black": float((y <= 5).mean())})
    return rows, out


def pose_of(row):
    # frames.csv carries the achieved camera pose; column names as written by finalise
    for kx, ky, kyaw in (("actual_x_cm", "actual_y_cm", "actual_yaw_deg"), ("x_cm", "y_cm", "yaw_deg"),
                         ("cam_x_cm", "cam_y_cm", "cam_yaw_deg")):
        if kx in row:
            return float(row[kx]), float(row[ky]), float(row[kyaw])
    raise KeyError(f"no pose columns in frames.csv: {list(row)[:12]}")


def same_pose_pairs(rows, min_gap=int(10 * fps), pos_tol=60.0, yaw_tol=10.0, max_pairs=80):
    P = np.array([pose_of(r) for r in rows])
    pairs = []
    for b in range(min_gap, len(rows), 2):
        cand = np.arange(0, b - min_gap)
        d = np.hypot(P[cand, 0] - P[b, 0], P[cand, 1] - P[b, 1])
        dy = np.abs((P[cand, 2] - P[b, 2] + 180) % 360 - 180)
        ok = np.where((d < pos_tol) & (dy < yaw_tol))[0]
        if len(ok):
            a = int(cand[ok[np.argmin(d[ok])]])
            pairs.append((a, b, float(d[ok].min())))
    # thin to at most max_pairs, spread over the episode
    if len(pairs) > max_pairs:
        idx = np.linspace(0, len(pairs) - 1, max_pairs).round().astype(int)
        pairs = [pairs[i] for i in idx]
    return pairs


def pair_diffs(d, pairs):
    diffs, floor = [], []
    for a, b, _ in pairs:
        ia, ib = cv2.imread(str(d / "rgb" / f"{a:06d}.jpg")), cv2.imread(str(d / "rgb" / f"{b:06d}.jpg"))
        if ia is None or ib is None:
            continue
        diffs.append(abs(float(luma(ia).mean()) - float(luma(ib).mean())))
        nb = cv2.imread(str(d / "rgb" / f"{b + 1:06d}.jpg"))
        if nb is not None:
            floor.append(abs(float(luma(ib).mean()) - float(luma(nb).mean())))
    med = lambda xs: float(np.median(xs)) if xs else float("nan")
    p90 = lambda xs: float(np.percentile(xs, 90)) if xs else float("nan")
    return {"n": len(diffs), "median": med(diffs), "p90": p90(diffs), "max": max(diffs) if diffs else float("nan"),
            "floor_median": med(floor), "per_pair": diffs}


def thumb(src, dst, width=320):
    img = cv2.imread(str(src))
    if img is None:
        return False
    h = int(round(img.shape[0] * width / img.shape[1]))
    cv2.imwrite(str(dst), cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 86])
    return True


# ---------------- measurements
stats, rows = {}, {}
for tag, d in eps.items():
    rows[tag], stats[tag] = frame_stats(d)
pairs = same_pose_pairs(rows["auto"])
pd = {tag: pair_diffs(d, pairs) for tag, d in eps.items()}
agg = {tag: {"mean": float(np.mean([s["mean"] for s in st])), "blown": float(np.median([s["blown"] for s in st])),
             "black": float(np.median([s["black"] for s in st])), "blown_max": max(s["blown"] for s in st),
             "black_max": max(s["black"] for s in st), "p10": float(np.median([s["p10"] for s in st]))}
       for tag, st in stats.items()}

# aligned frames: the two captures replay the same frozen poses, so frame i is the same view in
# both and the only difference is the exposure mode. |auto - manual| over time is auto-exposure's
# drift itself, no pose tolerance involved.
al = {}
ma = {s["frame"]: s for s in stats["manual"]}
diff_series = [{"frame": s["frame"], "d": s["mean"] - ma[s["frame"]]["mean"]} for s in stats["auto"] if s["frame"] in ma]
draw = np.array([x["d"] for x in diff_series])
# A constant offset between the two is just the chosen bias sitting a fraction of a stop from
# where auto-exposure averages - fixable by +-0.25 EV and not a defect. Auto-exposure's DRIFT is
# the deviation from that offset: how far the same frame moves depending on the recent past.
offset = float(np.median(draw))
drift = np.abs(draw - offset)
for x in diff_series:
    x["d"] = x["d"] - offset
al = {"n": int(len(draw)), "offset": offset, "offset_ev": float(np.log2(max(agg["auto"]["mean"], 1) / max(agg["manual"]["mean"], 1))),
      "median": float(np.median(drift)), "p90": float(np.percentile(drift, 90)), "max": float(drift.max()),
      "frames_over_5": int((drift > 5).sum()), "frames_over_10": int((drift > 10).sum()),
      "worst_frame": int(diff_series[int(np.argmax(drift))]["frame"])}
# how fast auto-exposure moves between two samples 3 frames (1/8 s) apart, vs manual on the same frames
def jumps(st):
    m = [x["mean"] for x in st]
    return np.abs(np.diff(m))
jm = {t: {"p90": float(np.percentile(jumps(st), 90)), "max": float(jumps(st).max()),
          "over_5": int((jumps(st) > 5).sum())} for t, st in stats.items()}

# ---------------- assets
# sweep grid thumbs
sw_frames = sweep["frames"]
poses = sorted({f["pose_index"] for f in sw_frames})
variants = ["auto"] + [f"ev{ev:+.1f}" for ev in sweep["evs"]]
grid_poses = poses[::max(1, len(poses) // 8)][:8]
for f in sw_frames:
    if f["pose_index"] in grid_poses:
        thumb(SRC / "sweep" / f["file"], SITE / "img" / f["file"].replace(".png", ".jpg"), 240)
# pair examples: 4 pairs with the largest auto difference
ex_idx = np.argsort(pd["auto"]["per_pair"])[::-1][:4] if pd["auto"]["per_pair"] else []
examples = []
for k in ex_idx:
    a, b, dist = pairs[int(k)]
    row = {"a": a, "b": b, "dist_cm": round(dist, 1), "gap_s": round((b - a) / fps, 1), "imgs": {}}
    for tag, d in eps.items():
        for which, fr in (("first", a), ("again", b)):
            name = f"pair{int(k)}_{tag}_{which}.jpg"
            if thumb(d / "rgb" / f"{fr:06d}.jpg", SITE / "img" / name, 400):
                row["imgs"][f"{tag}_{which}"] = name
        row[f"{tag}_diff"] = round(pd[tag]["per_pair"][int(k)], 1)
    examples.append(row)
# videos
vids = {}
for tag, d in eps.items():
    for cand in ("rgb.mp4", "rgb_proxy.mp4"):
        if (d / cand).exists():
            shutil.copy2(d / cand, SITE / f"{tag}.mp4"); vids[tag] = f"{tag}.mp4"; break
# timeline svg
def svg_timeline(series, key, ylim=(0, 255), w=1100, h=220, colors={"auto": "#c2402f", "manual": "#0b8a5f"}):
    n = max(len(s) for s in series.values())
    def path(st):
        pts = [(40 + (s["frame"] / (rows["auto"].__len__() - 1)) * (w - 60), h - 25 - (min(max(s[key], ylim[0]), ylim[1]) - ylim[0]) / (ylim[1] - ylim[0]) * (h - 45)) for s in st]
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    grid = "".join(f'<line x1="40" x2="{w-20}" y1="{h-25-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" y2="{h-25-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" class="grid"/><text x="34" y="{h-22-(v-ylim[0])/(ylim[1]-ylim[0])*(h-45):.1f}" class="tick" text-anchor="end">{v:g}</text>' for v in np.linspace(ylim[0], ylim[1], 6))
    marks = "".join(f'<rect x="{40 + (e["window"][0]/(len(rows["auto"])-1))*(w-60):.1f}" y="20" width="{max(2,(e["window"][1]-e["window"][0])/(len(rows["auto"])-1)*(w-60)):.1f}" height="{h-45}" class="revisit"/>' for e in frozen.get("revisit_events", []))
    lines = "".join(f'<path d="{path(st)}" fill="none" stroke="{colors[t]}" stroke-width="1.6"/>' for t, st in series.items())
    xt = "".join(f'<text x="{40 + i/4*(w-60):.1f}" y="{h-8}" class="tick" text-anchor="middle">{i/4*len(rows["auto"])/fps:.0f} s</text>' for i in range(5))
    return f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">{grid}{marks}{lines}{xt}</svg>'

tl_mean = svg_timeline(stats, "mean")
_diff_stats = {"diff": [{"frame": x["frame"], "mean": x["d"]} for x in diff_series]}
tl_diff = svg_timeline(_diff_stats, "mean", ylim=(-20, 20), colors={"diff": "#8a5a0b"})
tl_blown = svg_timeline(stats, "blown", ylim=(0, 0.2))

# ---------------- page
ev = test["chosen_ev"]
sw_rows = "".join(
    f'<tr class="{"chosen" if (t["ev"] is not None and t["ev"] == ev) else ("ref" if t["ev"] is None else "")}">'
    f'<td>{"自动曝光（现状）" if t["ev"] is None else f"手动 {t['ev']:+.1f} EV"}</td>'
    f'<td class="n mono r">{t["p10"]}</td><td class="n mono r">{t["p50"]}</td><td class="n mono r">{t["p99"]}</td>'
    f'<td class="n mono r">{t["blown_frac"]*100:.2f}%</td><td class="n mono r">{t["black_frac"]*100:.2f}%</td>'
    f'<td class="mut">{"参照，不参与选择" if t["ev"] is None else ("<b>选中</b>" if t["ev"] == ev else ("在限内" if t["score"] is not None else "超限"))}</td></tr>'
    for t in sweep["table"])
grid_head = "".join(f"<th>{'自动' if v == 'auto' else v.replace('ev', '') + ' EV'}</th>" for v in variants)
grid_body = ""
for p in grid_poses:
    cells = ""
    for v in variants:
        f = next((x for x in sw_frames if x["pose_index"] == p and x["variant"] == v), None)
        if f:
            cells += f'<td><img src="img/{f["file"].replace(".png", ".jpg")}" loading="lazy" alt=""><div class="cap mono">p50 {f["p50"]} · 过曝 {f["blown_frac"]*100:.1f}% · 黑 {f["black_frac"]*100:.1f}%</div></td>'
        else:
            cells += "<td></td>"
    grid_body += f'<tr><th class="mono">#{p}</th>{cells}</tr>'
ex_html = ""
for e in examples:
    ex_html += f'''<div class="pair"><div class="pairhd"><span>第 {e["a"]} 帧 → 第 {e["b"]} 帧，相隔 {e["gap_s"]} s，位置差 {e["dist_cm"]} cm</span>
    <span class="mono">自动 Δ亮度 <b style="color:var(--bad)">{e["auto_diff"]}</b> · 手动 Δ亮度 <b style="color:var(--spd)">{e["manual_diff"]}</b></span></div>
    <div class="pairrow"><div><div class="lab">自动曝光 · 首次</div><img src="img/{e["imgs"].get("auto_first","")}" loading="lazy"></div><div><div class="lab">自动曝光 · 再来</div><img src="img/{e["imgs"].get("auto_again","")}" loading="lazy"></div>
    <div><div class="lab">手动 {ev:+.1f} EV · 首次</div><img src="img/{e["imgs"].get("manual_first","")}" loading="lazy"></div><div><div class="lab">手动 {ev:+.1f} EV · 再来</div><img src="img/{e["imgs"].get("manual_again","")}" loading="lazy"></div></div></div>'''
lighting = sweep.get("lighting") or {}
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>曝光钉死测试 · Downtown West</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--cam:#8a5a0b;--oth:#6b7280;--bad:#c2402f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif;overflow-x:hidden}}
header,.wrap{{max-width:1400px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}h1{{margin:0 0 6px;font-size:23px;letter-spacing:-.01em}}h2{{margin:0;font-size:16.5px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a{{color:var(--move);text-decoration:none}}
.totals{{display:flex;gap:22px;flex-wrap:wrap;margin-top:16px;font-size:13px;color:var(--mut)}}.totals div b{{display:block;font-size:21px;color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}}
section{{margin-top:30px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}.sechd .note{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{color:var(--mut);font-weight:500;font-size:11.5px;white-space:nowrap}}td.r{{text-align:right}}
.mono{{font-family:ui-monospace,monospace;font-size:12px;font-variant-numeric:tabular-nums}}td.n{{white-space:nowrap;width:1%}}td.mut{{color:var(--mut);font-size:13px}}
tr.chosen td{{background:rgba(11,138,95,.10)}}tr.ref td{{color:var(--mut)}}.scroll{{overflow-x:auto}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px}}.card p{{margin:0 0 8px}}.card p:last-child{{margin:0}}
.grid td img{{width:240px;height:auto;display:block;border-radius:4px}}.grid td{{padding:4px}}.grid .cap{{color:var(--mut);font-size:10.5px;margin-top:2px}}
.chart{{width:100%;height:auto;display:block;background:var(--card);border:1px solid var(--line);border-radius:8px}}.chart .grid{{stroke:var(--line)}}.chart .tick{{fill:var(--mut);font-size:11px}}.chart .revisit{{fill:rgba(30,111,217,.10)}}
.legend{{display:flex;gap:18px;font-size:13px;color:var(--mut);margin:6px 0 12px}}.legend i{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px}}
.pair{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:14px}}.pairhd{{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--mut);margin-bottom:8px;flex-wrap:wrap}}
.pairrow{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}.pairrow img{{width:100%;height:auto;display:block;border-radius:4px}}.lab{{font-size:11.5px;color:var(--mut);margin-bottom:3px}}
.vids{{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:16px}}video{{width:100%;border-radius:8px;background:#000}}
</style></head><body>
<header><h1>曝光钉死测试 · Downtown West</h1>
<p class="sub"><a href="../">← 长视频采集</a> · {html.escape(map_label)} · 路线 {Path(test["frozen"]).stem.split("__")[1]}，{len(rows["auto"])} 帧 / {len(rows["auto"])/fps:.0f} s，同一条冻结路线录两遍：一遍按现状（关卡自带自动曝光），一遍把采集相机钉在手动 {ev:+.1f} EV</p>
<div class="totals">
 <div>选中的曝光偏移<b>{ev:+.1f} EV</b></div>
 <div>自动曝光漂移（同帧，扣偏移）<b>{al["median"]:.1f} <span style="font-size:13px;color:var(--mut)">中位 · p90 {al["p90"]:.1f} · 最大 {al["max"]:.1f}</span></b></div>
 <div>同位姿两次经过 · 自动<b style="color:var(--bad)">{pd["auto"]["median"]:.2f} <span style="font-size:13px;color:var(--mut)">中位 · 最大 {pd["auto"]["max"]:.1f}</span></b></div>
 <div>同位姿两次经过 · 手动<b style="color:var(--spd)">{pd["manual"]["median"]:.2f} <span style="font-size:13px;color:var(--mut)">中位 · 最大 {pd["manual"]["max"]:.1f}</span></b></div>
 <div>过曝像素 · 自动/手动<b>{agg["auto"]["blown"]*100:.2f}% / {agg["manual"]["blown"]*100:.2f}%</b></div>
 <div>近黑像素 · 自动/手动<b>{agg["auto"]["black"]*100:.2f}% / {agg["manual"]["black"]*100:.2f}%</b></div>
</div></header>
<div class="wrap">
<section><div class="sechd"><h2>结论</h2><span class="note">两段录制回放同一条冻结路线，帧号一一对应；同位姿对 = 位置差 &lt; 60 cm、朝向差 &lt; 10°、相隔 ≥ 10 s 的两帧，共 {pd["auto"]["n"]} 对</span></div>
<div class="card">
<p><b>逐帧对齐比较（最干净的量）</b>：同一帧、同一位姿，只有曝光模式不同。两段之间有一个恒定偏移 {al["offset"]:+.1f}/255（自动平均比手动 {ev:+.1f} EV 亮约 {al["offset_ev"]:.2f} 档，这是选档误差，调 +0.25 EV 就能对齐，不是缺陷）。扣掉这个偏移后剩下的才是自动曝光的<b>漂移</b>——同一帧随"刚看过什么"而变的部分：中位 <b>{al["median"]:.1f}</b>、p90 <b>{al["p90"]:.1f}</b>、最大 <b>{al["max"]:.1f}</b>（/255，第 {al["worst_frame"]} 帧）；{al["frames_over_5"]} 个采样帧漂移超过 5，{al["frames_over_10"]} 个超过 10。第二节第三张图画的就是这条曲线。</p>
<p><b>同一个地方两次经过</b>：自动曝光下亮度差中位 <b>{pd["auto"]["median"]:.2f}</b>、p90 <b>{pd["auto"]["p90"]:.1f}</b>、最大 <b>{pd["auto"]["max"]:.1f}</b>；手动 {ev:+.1f} EV 下 <b>{pd["manual"]["median"]:.2f}</b>、p90 <b>{pd["manual"]["p90"]:.1f}</b>、最大 <b>{pd["manual"]["max"]:.1f}</b>。60 cm / 10° 的容差里本身就有视角变化，所以手动也不为零；两者的差距才是曝光贡献的那部分。</p>
<p><b>帧间跳变</b>（相隔 1/8 s 的两个采样帧亮度差）：自动 p90 {jm["auto"]["p90"]:.1f}、最大 {jm["auto"]["max"]:.1f}、超过 5 的有 {jm["auto"]["over_5"]} 处；手动 p90 {jm["manual"]["p90"]:.1f}、最大 {jm["manual"]["max"]:.1f}、超过 5 的有 {jm["manual"]["over_5"]} 处。</p>
<p><b>怎么读这些数</b>：Downtown West 自带的后期把自动曝光的范围夹得比较窄，所以漂移是十几个灰阶而不是 Tokyo 历史数据里的 24；但 {al["frames_over_10"]} / {al["n"]} 个采样帧漂移超过 10，集中在从骑楼阴影转向开阔天空的那几段（第 {al["worst_frame"]} 帧附近）。钉死曝光把这部分归零，并且让曝光成为写在 <code>trajectory.json</code> / <code>capture_summary.json</code> 里的已知数（manual, {ev:+.1f} EV），而不是每帧各异的隐含状态。</p>
<p><b>代价</b>：手动 {ev:+.1f} EV 全程过曝像素中位 {agg["manual"]["blown"]*100:.2f}%（最坏一帧 {agg["manual"]["blown_max"]*100:.1f}%）、近黑 {agg["manual"]["black"]*100:.2f}%（最坏 {agg["manual"]["black_max"]*100:.1f}%）；自动曝光分别是 {agg["auto"]["blown"]*100:.2f}% / {agg["auto"]["blown_max"]*100:.1f}% 与 {agg["auto"]["black"]*100:.2f}% / {agg["auto"]["black_max"]*100:.1f}%。近黑多出的几个百分点是骑楼和门洞的阴影：固定曝光不再为它们提亮，这是"一个位置一种外观"的直接代价。{"天光：" + ("会话内已把 " + ", ".join(lighting.get("made_dynamic", [])) + " 改为动态" if lighting.get("applied") else "关卡自带的天光已是动态，未改动") + "。"}</p>
</div></section>
<section><div class="sechd"><h2>一、选偏移：{sweep["poses"]} 个位姿 × {len(sweep["evs"])} 档手动曝光</h2><span class="note">每帧的直方图在引擎里算；表里是 {sweep["poses"]} 个位姿的中位数。选择标准：过曝 ≤ 0.5%、近黑 ≤ 3%、p10 ≥ 30，然后 p50 最接近 100</span></div>
<div class="scroll"><table><thead><tr><th>曝光</th><th class="r">p10</th><th class="r">p50</th><th class="r">p99</th><th class="r">过曝 ≥250</th><th class="r">近黑 ≤5</th><th>判定</th></tr></thead><tbody>{sw_rows}</tbody></table></div>
<div class="scroll" style="margin-top:14px"><table class="grid"><thead><tr><th>位姿</th>{grid_head}</tr></thead><tbody>{grid_body}</tbody></table></div>
</section>
<section><div class="sechd"><h2>二、整条路线的亮度</h2><span class="note">每 3 帧取一帧的画面平均亮度；蓝色带是路线设计的回访窗口</span></div>
<div class="legend"><span><i style="background:#c2402f"></i>自动曝光（现状）</span><span><i style="background:#0b8a5f"></i>手动 {ev:+.1f} EV</span></div>
{tl_mean}
<p class="sub" style="margin:10px 0 6px">过曝像素比例（≥250），纵轴 0–20%</p>
{tl_blown}
<p class="sub" style="margin:10px 0 6px">同一帧：自动 − 手动 的亮度差，已扣除恒定偏移 {al["offset"]:+.1f}（/255），纵轴 ±20。零线附近 = 两者只差一个常数；偏离处 = 自动曝光在漂移</p>
{tl_diff}
</section>
<section><div class="sechd"><h2>三、同一个地方，两次经过</h2><span class="note">自动曝光下亮度差最大的 4 对；四张图同一位姿</span></div>{ex_html}</section>
<section><div class="sechd"><h2>四、两段完整视频</h2><span class="note">同一条路线、同一帧号；review 编码，非交付数据</span></div>
<div class="vids">{"".join(f'<div><div class="lab">{"自动曝光（现状）" if t == "auto" else f"手动 {ev:+.1f} EV"}</div><video controls preload="metadata" src="{v}"></video></div>' for t, v in vids.items())}</div></section>
</div></body></html>'''
(SITE / "index.html").write_text(page)
json.dump({"pairs": pd, "agg": agg, "aligned": al, "jumps": jm, "chosen_ev": ev}, open(SITE / "measurements.json", "w"), indent=1, default=float)
print("aligned", {k: round(v, 2) if isinstance(v, float) else v for k, v in al.items()}, "jumps", jm)
print("wrote", SITE / "index.html", "| pairs", pd["auto"]["n"], "| auto Δ", round(pd["auto"]["median"], 2), "manual Δ", round(pd["manual"]["median"], 2))
