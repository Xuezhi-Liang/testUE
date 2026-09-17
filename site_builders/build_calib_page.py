#!/usr/bin/env python3
"""/longvideo/calib/: per-map adaptive lighting calibration on several maps, what it chose and why.
    python3 build_calib_page.py <calib_root>   (calib_root/<slug>/{test.json, calib/, rec/})
"""
import csv, html, json, os, shutil, sys
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2, numpy as np

ROOT = Path(sys.argv[1]); SITE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/calib"); (SITE / "img").mkdir(parents=True, exist_ok=True)
luma = lambda im: 0.2126 * im[..., 2] + 0.7152 * im[..., 1] + 0.0722 * im[..., 0]
def thumb(src, dst, width=300):
    if Path(dst).exists() and Path(dst).stat().st_mtime >= Path(src).stat().st_mtime: return True
    im = cv2.imread(str(src))
    if im is None: return False
    cv2.imwrite(str(dst), cv2.resize(im, (width, int(round(im.shape[0] * width / im.shape[1]))), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 86]); return True
def same_pose_diff(d, min_gap_s=10, pos_tol=60.0, yaw_tol=10.0, max_pairs=80):
    """Median |brightness| difference between two visits of the same pose >= min_gap_s apart:
    the path-dependence of the exposure, with the viewpoint tolerance as its floor."""
    rows = list(csv.DictReader(open(d / "frames.csv")))
    P = np.array([(float(r["actual_x_cm"]), float(r["actual_y_cm"]), float(r["actual_yaw_deg"])) for r in rows])
    gap = int(min_gap_s * 24); pairs = []
    for b in range(gap + 1, len(rows), 2):
        c = np.arange(0, b - gap); dd = np.hypot(P[c, 0] - P[b, 0], P[c, 1] - P[b, 1]); dy = np.abs((P[c, 2] - P[b, 2] + 180) % 360 - 180)
        ok = np.where((dd < pos_tol) & (dy < yaw_tol))[0]
        if len(ok): pairs.append((int(c[ok[np.argmin(dd[ok])]]), b))
    if len(pairs) > max_pairs: pairs = [pairs[i] for i in np.linspace(0, len(pairs) - 1, max_pairs).round().astype(int)]
    ds = []
    for a, b in pairs:
        ia, ib = cv2.imread(str(d / "rgb" / f"{a:06d}.jpg")), cv2.imread(str(d / "rgb" / f"{b:06d}.jpg"))
        if ia is not None and ib is not None: ds.append(abs(float(luma(ia).mean()) - float(luma(ib).mean())))
    return {"n": len(ds), "median": float(np.median(ds)) if ds else float("nan"), "p90": float(np.percentile(ds, 90)) if ds else float("nan")}

def rec_stats(d, step=4):
    n = sum(1 for _ in open(d / "frames.csv")) - 1; ys = []
    for i in range(0, n, step):
        im = cv2.imread(str(d / "rgb" / f"{i:06d}.jpg"))
        if im is not None: y = luma(im); ys.append((float(y.mean()), float((y <= 5).mean()), float((y >= 250).mean())))
    a = np.array(ys); return {"frames": n, "mean": float(a[:, 0].mean()), "black": float(np.median(a[:, 1])), "black_max": float(a[:, 1].max()), "blown": float(np.median(a[:, 2])), "blown_max": float(a[:, 2].max())}

cards, summary_rows = "", ""
for mapdir in sorted(d for d in ROOT.glob("*/") if (d / "test.json").exists() or (d / "asis.json").exists()):
    td = mapdir / "test.json"; slug = mapdir.name; short = slug.removeprefix("Game_")
    if td.exists():
        t = json.loads(td.read_text()); c = t["calibration"]
    else:
        t = {"frozen": json.loads((mapdir / "asis.json").read_text())["frozen"]}
        c = {"trials": [], "chosen_factor": None, "chosen_ev": None, "quality_bar_missed": True, "poses": 0, "factors_tried": [], "took_s": 0,
             "reference": {"p10": 0, "p50": 0, "p99": 0, "black_frac": float("nan"), "blown_frac": float("nan"), "contrast": 0}, "pending": True}
    ref_dir = td.parent / "calib" / "ref"; ref_frames = sorted(ref_dir.glob("pose*_current.png"))
    chosen_F, chosen_ev, missed = c.get("chosen_factor"), c.get("chosen_ev"), c.get("quality_bar_missed")
    # probe thumbs: ref vs chosen, every 3rd pose
    grid = ""
    if chosen_F is not None:
        cdir = td.parent / "calib" / f"x{chosen_F:g}"
        for rf in ref_frames[::3]:
            k = rf.name.split("_")[0]; cf = next(iter(cdir.glob(f"{k}_*_ev{chosen_ev:+.1f}.png")), None)
            a = f"{slug}_{rf.name.replace('.png', '.jpg')}"; b = f"{slug}_{k}_chosen.jpg"
            ok_a = thumb(rf, SITE / "img" / a); ok_b = cf is not None and thumb(cf, SITE / "img" / b)
            grid += f'<div class="pp"><div><div class="lab">原样（自动曝光）</div>{"<img src=img/" + a + " loading=lazy>" if ok_a else ""}</div><div><div class="lab">选定：天光 ×{chosen_F:g} · {chosen_ev:+.1f} EV</div>{"<img src=img/" + b + " loading=lazy>" if ok_b else ""}</div></div>'
    trials = ""
    for tr in c["trials"]:
        if tr.get("chosen_ev") is None:
            trials += f'<tr><td class="mono">×{tr["factor"]:g}</td><td colspan="6" class="mut">{html.escape(str(tr.get("why") or tr.get("error")))}</td></tr>'; continue
        ch = tr["checks"]; mark = lambda k: f'<span class="{"ok" if ch[k] else "bad"}">{"✓" if ch[k] else "✗"}</span>'
        trials += (f'<tr class="{"chosen" if tr["factor"] == chosen_F else ""}"><td class="mono">×{tr["factor"]:g}</td><td class="mono">{tr["chosen_ev"]:+.1f}</td>'
                   f'<td class="r mono">{tr["black_frac"]*100:.1f}% {mark("black")}</td><td class="r mono">{tr["blown_frac"]*100:.2f}% {mark("blown")}</td>'
                   f'<td class="r mono">{tr["contrast"]:.0f} ({tr["contrast_ratio"]*100:.0f}%) {mark("contrast")}</td><td class="r mono">{tr["p10"]} / {tr["p50"]} / {tr["p99"]}</td>'
                   f'<td>{"<b>选定</b>" if tr["factor"] == chosen_F else ("达标" if tr["passes"] else "未达标")}</td></tr>')
    r = c["reference"]
    rec = rec_stats(Path(t["episode"])) if t.get("episode") else None
    asis_p = td.parent / "asis.json"; asis = json.loads(asis_p.read_text()) if asis_p.exists() else None
    arec = rec_stats(Path(asis["episode"])) if asis and Path(asis["episode"], "frames.csv").exists() else None
    def copy_vid(ep_dir, name):
        for cand in ("rgb.mp4", "rgb_proxy.mp4"):
            src = Path(ep_dir) / cand
            if src.exists():
                dst = SITE / name
                if not (dst.exists() and dst.stat().st_size == src.stat().st_size):
                    shutil.copy2(src, str(dst) + ".part"); os.replace(str(dst) + ".part", dst)   # atomic: never a half-copied mp4
                return f'<video controls preload="metadata" src="{name}"></video>'
        return ""
    v_cal = copy_vid(t["episode"], f"{slug}.mp4") if t.get("episode") else ""
    v_asis = copy_vid(asis["episode"], f"{slug}_asis.mp4") if asis else ""
    inst_p = td.parent / "instant.json"; inst = json.loads(inst_p.read_text()) if inst_p.exists() else None
    if inst is None:
        # a run still in progress: show whatever recording has landed, labelled as such
        done = lambda sub: next((d for d in (td.parent / sub).glob("*/") if (d / "frames.csv").exists() and (d / "capture_summary.json").exists() and (d / "rgb.mp4").exists()), None) if (td.parent / sub).exists() else None
        di, dl = done("instant"), done("instant_le")
        if di or dl:
            inst = {"factor": 1.0, "ae_speed": 100.0, "le_chosen": float("nan"), "le_trials": [], "partial": True,
                    "instant": {"episode": str(di)} if di else {}, "instant_le": {"episode": str(dl)} if dl else {"skipped": "录制中"}}
    inst_ep = inst and (inst.get("instant") or {}).get("episode"); le_ep = inst and (inst.get("instant_le") or {}).get("episode")
    irec = rec_stats(Path(inst_ep)) if inst_ep and Path(inst_ep, "frames.csv").exists() else None
    lrec = rec_stats(Path(le_ep)) if le_ep and Path(le_ep, "frames.csv").exists() else None
    v_inst = copy_vid(inst_ep, f"{slug}_instant.mp4") if inst_ep else ""
    v_le = copy_vid(le_ep, f"{slug}_instant_le.mp4") if le_ep else ""
    le_label = (("即时 + 局部曝光（录制中）" if inst.get("partial") else f'即时 + 局部曝光（阴影 {inst["le_chosen"]:.2f}）') if inst else "")
    nd_p = td.parent / "newdefault.json"; nd = json.loads(nd_p.read_text()) if nd_p.exists() else None
    nd_ep = nd and nd.get("episode"); ndrec = rec_stats(Path(nd_ep)) if nd_ep and Path(nd_ep, "frames.csv").exists() else None
    v_nd = copy_vid(nd_ep, f"{slug}_newdefault.mp4") if nd_ep else ""
    nd_label = "新默认（1×）：每帧新建状态 + 自动曝光 + 天光标定 + 局部曝光 0.65"
    ss_cols = []
    for ss_tag, ss_lab in (("nd_ss2", "新默认 + 2× 超采样"), ("nd_ss3", "新默认 + 3× 超采样"), ("nd_ss4", "新默认 + 4× 超采样")):
        sp_ = td.parent / f"{ss_tag}.json"
        if sp_.exists():
            j = json.loads(sp_.read_text()); ep_ = j.get("episode")
            if ep_ and Path(ep_, "frames.csv").exists():
                ss_cols.append((ss_tag, ss_lab + (f'（{j.get("engine_fps") or 0:.1f} fps）'), ep_, copy_vid(ep_, f"{slug}_{ss_tag}.mp4"), rec_stats(Path(ep_))))
    sp = {}
    for name, ep_ in [("原样（自动曝光）", asis and asis.get("episode")), ("标定固定曝光", t.get("episode")), ("即时自动曝光", inst_ep), (le_label, le_ep), (nd_label, nd_ep)] + [(sc_[1], sc_[2]) for sc_ in ss_cols]:
        if ep_ and Path(ep_, "frames.csv").exists(): sp[name] = same_pose_diff(Path(ep_))
    vid = ""
    if v_asis or v_cal or v_inst or v_le or v_nd or ss_cols:
        cols = [("原样：关卡光照 + 自动曝光（有滞后）", v_asis), (f'标定固定曝光{"" if chosen_F is None else f"：天光 ×{chosen_F:g} · {chosen_ev:+.1f} EV"}', v_cal or "<p class=sub>标定拒绝给出参数，未录制</p>")]
        if inst_ep: cols.append((f'即时自动曝光：天光 ×{inst["factor"]:g}，速度 {inst["ae_speed"]:.0f}', v_inst))
        if le_ep: cols.append((le_label, v_le))
        elif inst and (inst.get("instant_le") or {}).get("skipped"): cols.append((le_label or "即时 + 局部曝光", f'<p class=sub>{inst["instant_le"]["skipped"]}</p>'))
        if nd_ep: cols.append((nd_label + (f'（天光 ×{(nd.get("lighting") or {}).get("fill", {}).get("factor", "?")}，{nd.get("engine_fps") or 0:.1f} fps）'), v_nd))
        for sc_ in ss_cols: cols.append((sc_[1], sc_[3]))
        vid = f'<div class="two" style="grid-template-columns:repeat({len(cols)},1fr)">' + "".join(f'<div><div class="lab">{a}</div>{b}</div>' for a, b in cols) + '</div>'
    # recorded-frame pairs, same frame index
    frames_html = ""
    if arec and rec:
        n = min(arec["frames"], rec["frames"]); cells = ""
        for f in (n // 6, n // 2, (5 * n) // 6):
            a = f"{slug}_asis_f{f:06d}.jpg"; b = f"{slug}_cal_f{f:06d}.jpg"
            oa = thumb(Path(asis["episode"]) / "rgb" / f"{f:06d}.jpg", SITE / "img" / a, 440); ob = thumb(Path(t["episode"]) / "rgb" / f"{f:06d}.jpg", SITE / "img" / b, 440)
            extra = ""; ncol = 2
            for tag_, ep_, lab_ in [("inst", inst_ep, "即时自动曝光"), ("le", le_ep, le_label), ("nd", nd_ep, "新默认 1×")] + [(sc_[0], sc_[2], sc_[1].split("（")[0]) for sc_ in ss_cols]:
                if ep_:
                    ci = f"{slug}_{tag_}_f{f:06d}.jpg"
                    if thumb(Path(ep_) / "rgb" / f"{f:06d}.jpg", SITE / "img" / ci, 440): extra += f'<div><div class="lab">{lab_} · 第 {f} 帧</div><img src="img/{ci}" loading="lazy"></div>'; ncol += 1
            cells += f'<div class="pp" style="grid-template-columns:repeat({ncol},1fr)"><div><div class="lab">原样 · 第 {f} 帧</div>{"<img src=img/" + a + " loading=lazy>" if oa else ""}</div><div><div class="lab">标定固定曝光 · 第 {f} 帧</div>{"<img src=img/" + b + " loading=lazy>" if ob else ""}</div>{extra}</div>'
        frames_html = f'<div class="grid">{cells}</div>'
    recrow = ""
    if arec or rec or irec:
        fmt = lambda x: (f'近黑 {x["black"]*100:.1f}%（最坏 {x["black_max"]*100:.0f}%）· 过曝 {x["blown"]*100:.2f}%（最坏 {x["blown_max"]*100:.1f}%）· 平均亮度 {x["mean"]:.0f}') if x else "—"
        recrow = f'<p><b>录制对比</b>（{(rec or arec or irec)["frames"]} 帧）：原样 {fmt(arec)}；标定固定曝光 {fmt(rec)}' + (f'；即时自动曝光 {fmt(irec)}' if irec else "") + (f'；{le_label} {fmt(lrec)}' if lrec else "") + (f'；<b>新默认 1×</b> {fmt(ndrec)}' if ndrec else "") + "".join(f'；<b>{sc_[1].split("（")[0]}</b> {fmt(sc_[4])}' for sc_ in ss_cols) + '。</p>'
        if inst and inst.get("le_trials"):
            recrow += '<p><b>局部曝光标定</b>（12 个位姿，阴影对比缩放从 1.0 往下试）：' + "；".join(f'{tr["shadow_scale"]:.2f} → 近黑 {tr["black_frac"]*100:.1f}%、对比 {tr["contrast"]:.0f}、p50 {tr["p50"]}{" ✓" if tr["passes"] else ""}' for tr in inst["le_trials"]) + f'。选 {inst["le_chosen"]:.2f}{"（未达标，取违规最小）" if inst.get("le_bar_missed") else ""}。</p>'
        if sp:
            recrow += '<p><b>同位姿两次经过的亮度差</b>（路径依赖，位置差 &lt;60 cm、朝向差 &lt;10°、相隔 ≥10 s；中位 / p90）：' + "；".join(f'{k} <b>{v["median"]:.2f}</b> / {v["p90"]:.1f}' for k, v in sp.items()) + '。</p>' 
    verdict = ("<span class='mut'>标定进行中</span>" if c.get("pending") else ("<span class='bad'>质量线未达到</span> · 取违规最小的一档，需人工决定录不录" if missed else "达到质量线"))
    b0 = (arec["black"] * 100) if arec else r["black_frac"] * 100; w0 = (arec["blown"] * 100) if arec else r["blown_frac"] * 100
    summary_rows += f'<tr><td>{html.escape(short)}</td><td class="mono">{"×%g" % chosen_F if chosen_F is not None else "—"}</td><td class="mono">{"%+.1f" % chosen_ev if chosen_ev is not None else "—"}</td><td class="r mono">{b0:.1f}% → {(rec["black"]*100 if rec else float("nan")):.1f}%</td><td class="r mono">{w0:.2f}% → {(rec["blown"]*100 if rec else float("nan")):.2f}%</td><td class="r mono">{c["took_s"]:.0f} s</td><td>{verdict}</td></tr>'
    cards += f'''<section><div class="sechd"><h2>{html.escape(short)}</h2><span class="note">{c["poses"]} 个位姿 · 试了 {", ".join("×%g" % f for f in c["factors_tried"])} · 标定 {c["took_s"]:.0f} s</span></div>
<div class="card"><p><b>{verdict}</b>{"" if chosen_F is None else f"：天光 ×{chosen_F:g}，曝光 {chosen_ev:+.1f} EV。"} 原样：近黑 {r["black_frac"]*100:.1f}%、过曝 {r["blown_frac"]*100:.2f}%、对比 {r["contrast"]:.0f}（p10 {r["p10"]} / p50 {r["p50"]} / p99 {r["p99"]}）。{f'录制 {rec["frames"]} 帧：近黑中位 {rec["black"]*100:.1f}%（最坏 {rec["black_max"]*100:.0f}%），过曝 {rec["blown"]*100:.2f}%（最坏 {rec["blown_max"]*100:.1f}%），平均亮度 {rec["mean"]:.0f}。' if rec else ""}</p></div>
<div class="scroll" style="margin-top:12px"><table><thead><tr><th>天光</th><th>曝光</th><th class="r">近黑 ≤3%</th><th class="r">过曝 ≤1%</th><th class="r">对比 ≥80% 原图</th><th class="r">p10 / p50 / p99</th><th></th></tr></thead><tbody>{trials}</tbody></table></div>
<div class="card" style="margin-top:12px">{recrow or "<p class=sub>录制统计待生成</p>"}</div>
{f'<div class="vid">{vid}</div>' if vid else ""}{frames_html}
<details style="margin-top:12px"><summary class="sub">标定探针位姿（原样 vs 选定档）</summary><div class="grid">{grid}</div></details></section>'''
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>自适应光照标定 · 四张地图</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--cam:#8a5a0b;--oth:#6b7280;--bad:#c2402f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif;overflow-x:hidden}}
header,.wrap{{max-width:1400px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}h1{{margin:0 0 6px;font-size:23px;letter-spacing:-.01em}}h2{{margin:0;font-size:16.5px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a{{color:var(--move);text-decoration:none}}
section{{margin-top:34px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}.sechd .note{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{color:var(--mut);font-weight:500;font-size:11.5px;white-space:nowrap}}td.r,th.r{{text-align:right}}td.mut{{color:var(--mut)}}
.mono{{font-family:ui-monospace,monospace;font-size:12px;font-variant-numeric:tabular-nums}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px}}.card p{{margin:0}}
tr.chosen td{{background:rgba(11,138,95,.10)}}.ok{{color:var(--spd)}}.bad{{color:var(--bad)}}.scroll{{overflow-x:auto}}
.grid{{display:grid;grid-template-columns:1fr;gap:10px;margin-top:14px}}.pp{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}.pp img{{width:100%;height:auto;display:block;border-radius:4px}}.lab{{font-size:11.5px;color:var(--mut);margin-bottom:3px}}
.vid{{margin-top:14px}}video{{width:100%;border-radius:8px;background:#000;display:block}}.two{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
</style></head><body>
<header><h1>自适应光照标定 · 四张地图</h1>
<p class="sub"><a href="../">← 长视频采集</a> · <a href="../fill/">补天光（固定 ×2）</a> · 录制前在路线上取 12 个位姿，天光系数从 ×1 往上试，每档扫一次曝光偏移，第一个同时满足「近黑 ≤3%、过曝 ≤1%、对比 ≥ 原图 80%」的档胜出；都不满足就取近黑最少的一档并标记「质量线未达到」。整集参数固定，只在图与图之间变。</p>
<div class="scroll" style="margin-top:16px"><table><thead><tr><th>地图</th><th>天光</th><th>曝光</th><th class="r">近黑 原样录制 → 标定录制</th><th class="r">过曝 原样录制 → 标定录制</th><th class="r">标定耗时</th><th>结果</th></tr></thead><tbody>{summary_rows}</tbody></table></div>
</header><div class="wrap">
<section><div class="sechd"><h2>结论</h2></div><div class="card">
<p><b>规则</b>：图与图之间自适应，一集之内固定。天光系数从 ×1 往上试，每档扫曝光偏移（目标 = 作者自动曝光的中位灰度），第一个同时满足四条的档胜出：近黑 ≤ 3%、过曝 ≤ 1%、对比 ≥ 原图 80%、中位灰度在原图 0.6–1.6 倍之内。最后一条是这轮加的：没有它，夜景会被拉成有星空的白天。都不满足则取总违规量最小的档并标记「质量线未达到」，由人决定。</p>
<p><b>四张图</b>：Downtown ×3 / +10.5 EV 与 Tokyo ×2 / +8.5 EV 达标，颜色是作者的，阴影抬起。ChemicalPlant 未达标——14.5% 的近黑是管架和厂房内部的全遮挡区域，天光照不进去，只有把整体亮度提高一档以上才能压到 3%，亮度守卫不允许，于是取 ×1 / +12.0 并标记；要不要为这类图放宽亮度带到 2 倍，是个可以商量的数。MedievalTown 夜景：原图 79% 近黑、中位灰度 0，任何曝光都不在扫描限内，标定拒绝给出参数、不录制——这是对的，夜景不该被标定成白天。</p>
<p><b>成本</b>：每张图录制前 10–45 秒。</p></div></section>
{cards}</div></body></html>'''
(SITE / "index.html.part").write_text(page); os.replace(SITE / "index.html.part", SITE / "index.html"); print("wrote", SITE / "index.html")
