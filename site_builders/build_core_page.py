#!/usr/bin/env python3
"""Sub-page /longvideo/core/: every map ranked by the size of its built-up core.

Reads core_survey.json (survey_core.py's output) and unmeasured.json (the maps whose start
positions gave the exporter nothing to stand on, with the reason from the fleet log). Same tokens
as the parent page. Static: rebuild after each survey.
"""
import json, html, sys
from pathlib import Path
D = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/core")
rows = [r for r in json.load(open(D / "core_survey.json")) if "error" not in r]
rows.sort(key=lambda r: -r["core_m2"])
unm = json.load(open(D / "unmeasured.json"))
recorded = set(json.load(open(D / "recorded_slugs.json"))) if (D / "recorded_slugs.json").exists() else set()
new = set(json.load(open(D / "new_this_round.json"))) if (D / "new_this_round.json").exists() else set()

def label(slug):
    s = slug.removeprefix("Game_")
    return s

def tier(c):
    return ("big", "≥2000 m²") if c >= 2000 else ("mid", "500–2000") if c >= 500 else ("small", "<500")

trs = []
for i, r in enumerate(rows, 1):
    t, _ = tier(r["core_m2"])
    tags = []
    if r["map"] in recorded: tags.append('<span class="chip ok">已录</span>')
    if r["map"] in new: tags.append('<span class="chip oth">本轮新测</span>')
    trs.append(
        f'<tr class="{t}"><td class="n mono">{i}</td>'
        f'<td><a href="png/{html.escape(r["map"])}.png" target="_blank">{html.escape(label(r["map"]))}</a> {" ".join(tags)}</td>'
        f'<td class="n mono r"><b>{r["core_m2"]:,.0f}</b></td>'
        f'<td class="n mono r">{r["core_frac_of_region_safe"]*100:.0f}%</td>'
        f'<td class="n mono r">{r["region_safe_m2"]:,.0f}</td>'
        f'<td class="n mono r">{r["core_clearance_median_m"]:.1f}</td>'
        f'<td class="n mono r">{r["export_extent_m"][0]:.0f}×{r["export_extent_m"][1]:.0f}</td></tr>')
utrs = "".join(f'<tr><td>{html.escape(label(u["slug"]))}</td><td class="mut">{html.escape(u["reason"])}</td></tr>' for u in unm)
big = sum(r["core_m2"] >= 2000 for r in rows); mid = sum(500 <= r["core_m2"] < 2000 for r in rows)
small = len(rows) - big - mid
page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>地图核心大小 · 86 张地图</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--cam:#8a5a0b;--oth:#6b7280;--bad:#c2402f;--core:#3fbf4f;--obst:#e07a1f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif;overflow-x:hidden}}
header,.wrap{{max-width:1400px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}
h1{{margin:0 0 6px;font-size:23px;letter-spacing:-.01em}}h2{{margin:0;font-size:16.5px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a,td a{{color:var(--move);text-decoration:none}}td a:hover{{text-decoration:underline}}
.totals{{display:flex;gap:22px;flex-wrap:wrap;margin-top:16px;font-size:13px;color:var(--mut)}}
.totals div b{{display:block;font-size:21px;color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}}
section{{margin-top:30px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}
.sechd .note{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:baseline}}
thead th{{color:var(--mut);font-weight:500;font-size:11.5px;white-space:nowrap}}th.r,td.r{{text-align:right}}
.mono{{font-family:ui-monospace,monospace;font-size:12px;font-variant-numeric:tabular-nums}}td.n{{white-space:nowrap;width:1%}}td.mut{{color:var(--mut);font-size:13px}}
.scroll{{overflow-x:auto}}tr.big td:first-child{{box-shadow:inset 3px 0 0 var(--spd)}}tr.small td{{color:var(--mut)}}tr.small td b{{color:var(--fg);font-weight:500}}
.chip{{font-size:11px;padding:1px 7px;border-radius:99px;display:inline-flex;border:1px solid currentColor;white-space:nowrap;vertical-align:1px;margin-left:4px}}
.chip.ok{{color:var(--spd)}}.chip.oth{{color:var(--oth)}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mut);margin:0 0 12px}}.legend i{{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-2px;margin-right:6px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;padding:12px 15px}}
.card p{{margin:0 0 8px}}.card p:last-child{{margin:0}}
img.sheet{{width:100%;height:auto;display:block;border-radius:8px;border:1px solid var(--line)}}
</style></head><body>
<header>
  <h1>地图核心大小 · 86 张地图</h1>
  <p class="sub"><a href="../">← 长视频采集</a> · 2026-09-09 · 方法同 9/7 那次：<code>survey_core.py</code>，每张地图取 navmesh 上"核心最大"的那一块连续可走面</p>
  <div class="totals">
    <div>可测地图<b>{len(rows)}</b></div><div>核心 ≥ 2000 m²<b>{big}</b></div><div>500–2000 m²<b>{mid}</b></div><div>&lt; 500 m²<b>{small}</b></div><div>无法测量<b>{len(unm)}</b></div>
  </div>
</header>
<div class="wrap">
<section>
  <div class="sechd"><h2>什么叫"核心"</h2><span class="note">数字越大，一次长时间的 coverage walk 越不会走到空地上去</span></div>
  <div class="card">
    <p>购买来的演示关卡多是"内容坐在一大块平坦空地上"。空地可走、通常还是地图上最大的可走区域，按"最大可走面"规划路线就会把整集花在内容外面（Tokyo 那次：8174 m² 的空地 vs 2151 m² 有房子的街区）。</p>
    <p><b>核心</b> = 有内容包围的内部可走面。一个格子要同时满足两条：① 周围 12 m 内障碍物密度 ≥ 6%；② 8 对相反方向里至少一对两侧 25 m 内都有障碍。第二条专门去掉"沿着围墙走一条边"这种单侧有内容的边缘带。</p>
    <p>按 navmesh 的<b>每块连续区域</b>分别算，取核心最大的一块，不做跨高度合并（Downtown_West 有一块贯穿全图的地下平面，一合并所有建筑脚印就被填掉了）。</p>
  </div>
</section>
<section>
  <div class="sechd"><h2>排名</h2><span class="note">点地图名看该图的核心示意图 · 绿=核心，灰=可走但非核心，橙=障碍物脚印</span></div>
  <div class="scroll"><table>
    <thead><tr><th>#</th><th>地图</th><th class="r">核心 m²</th><th class="r">占可走面</th><th class="r">可走面 m²</th><th class="r">核心处净空中位数 m</th><th class="r">导出范围 m</th></tr></thead>
    <tbody>{"".join(trs)}</tbody>
  </table></div>
</section>
<section>
  <div class="sechd"><h2>无法测量的 {len(unm)} 张</h2><span class="note">都是起始点文件的问题，不是地图的问题</span></div>
  <div class="scroll"><table><thead><tr><th>地图</th><th>原因</th></tr></thead><tbody>{utrs}</tbody></table></div>
  <p class="sub" style="margin-top:10px">这些地图要进统计，需要在编辑器里重新采一个站得住的起始坐标。navmesh 是围着起始点建的，起始点下面没有地面就建不出任何可走面。</p>
</section>
<section>
  <div class="sechd"><h2>拼图</h2><span class="note">按核心面积排序，5 列</span></div>
  <div class="legend"><span><i style="background:var(--core)"></i>核心</span><span><i style="background:#8a8a8a"></i>可走、非核心</span><span><i style="background:var(--obst)"></i>障碍物脚印</span></div>
  <a href="core_sheet.png" target="_blank"><img class="sheet" src="core_sheet.png" alt="73 张地图核心拼图" loading="lazy"></a>
</section>
</div></body></html>'''
(D / "index.html").write_text(page)
print("wrote", D / "index.html", len(rows), "rows,", len(unm), "unmeasured")
