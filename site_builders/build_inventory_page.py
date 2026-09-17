#!/usr/bin/env python3
"""Sub-page /longvideo/inventory/: everything we have, old catalogue and new delivery in one table.

Per map: where it came from, core size (the 09-09 survey where it exists, otherwise the offline
re-measurement from the 2026-09-16 navmesh exports, otherwise none), route-validation state, kept
fraction, network length, recorded episodes, overlap / drop notes. Static: rebuild after each run.

    python3 build_inventory_page.py
"""
import json, html, glob, shutil, time
from pathlib import Path

SITE = Path("/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo")
D = SITE / "inventory"; (D / "png").mkdir(parents=True, exist_ok=True)
CORE09 = Path("/home/ubuntu/UE5-Agent-Data/revisit_pipeline/results/core_survey_2026-09-09/core_survey.json")
OFFLINE = Path("/home/ubuntu/ue_newmap_validation_20260916/core_offline")
OLD = Path("/home/ubuntu/ue_route_validation_20260916"); NEW = Path("/home/ubuntu/ue_newmap_validation_20260916")
CORE_PNG_OLD = SITE / "core" / "png"

core09 = {r["map"]: r for r in json.load(open(CORE09)) if "error" not in r}
import csv
DELIV = list(csv.DictReader(open("/home/ubuntu/new_map/验收清单.csv", encoding="utf-8-sig")))
def slug_of(lv): return "Game_" + lv.lstrip("/").removeprefix("Game/").replace("/", "_")
deliv_levels = [(r["项目编号"], r["资源名称"], lv.strip()) for r in DELIV for lv in r["关卡路径"].split(";") if lv.strip()]
offline = json.load(open(OFFLINE / "core_offline.json")) if (OFFLINE / "core_offline.json").exists() else {}
# The 09-17 queue re-measured the 42 maps whose core size or route was missing; its results
# supersede both validation jobs for those maps, so read them first.
NEWROUTE = Path("/home/ubuntu/ue_newroute_fleet_20260916/maps")
newroute = {}
for rp in sorted(NEWROUTE.glob("*/result.json")) if NEWROUTE.exists() else []:
    try: newroute[rp.parent.name] = json.loads(rp.read_text())
    except (OSError, ValueError): pass
recorded = set(json.load(open(SITE / "core" / "recorded_slugs.json")))
TERMINAL = {"geometry_pass", "coverage_review", "route_failed", "failed", "timeout", "blocked_asset"}
STATE_CN = {"geometry_pass": "几何全过", "coverage_review": "待审(保留率低)", "route_failed": "路线失败", "failed": "起点/加载失败",
            "timeout": "超时", "blocked_asset": "资产未挂载", "interrupted": "中断", "running": "中断", "starting": "中断",
            "connecting": "中断", None: "未跑"}

def result(root, slug):
    r = newroute.get(slug)
    if r is None:
        p = root / "maps" / slug / "result.json"
        if not p.exists(): return {}, {}
        r = json.load(open(p))
    at = r.get("attempts") or []
    a = next((x for x in at if x.get("attempt") == r.get("chosen_attempt")), at[-1] if at else {})
    return r, a

def core_of(slug):
    nr = (newroute.get(slug) or {}).get("core")
    if nr and nr.get("core_m2") is not None:
        src = NEWROUTE / slug / "core.png"
        if src.exists(): shutil.copy2(src, D / "png" / f"{slug}.png")
        return nr["core_m2"], "09-17 队列补测", (f"png/{slug}.png" if src.exists() else None)
    if slug in core09: return core09[slug]["core_m2"], "09-09 调查", f"../core/png/{slug}.png"
    r = offline.get(slug)
    if r and "error" not in r:
        src = OFFLINE / "png" / f"{slug}.png"
        if src.exists(): shutil.copy2(src, D / "png" / f"{slug}.png")
        return r["core_m2"], "09-16 导航网离线补测", f"png/{slug}.png"
    return None, "未测", None

rows = []
for mc in json.load(open(OLD / "manifest.json")):
    r, a = result(OLD, mc["slug"]); c, csrc, png = core_of(mc["slug"])
    note = mc.get("rerun_reason", "") if mc.get("rerun") == "drop" else ""
    rows.append(dict(src="old", slug=mc["slug"], title="", core=c, core_src=csrc, png=png, state=r.get("state"),
                     kept=a.get("kept_fraction"), cl=(a.get("road_network") or {}).get("centreline_m"),
                     frames=a.get("frames"), recorded=mc["slug"] in recorded, note=note, drop=mc.get("rerun") == "drop"))
for mc in json.load(open(NEW / "manifest.json")):
    r, a = result(NEW, mc["slug"]); c, csrc, png = core_of(mc["slug"])
    note = mc.get("overlap") or mc.get("overlap_partial") or ""
    rows.append(dict(src="new", slug=mc["slug"], title=mc["title"], core=c, core_src=csrc, png=png, state=r.get("state"),
                     kept=a.get("kept_fraction"), cl=(a.get("road_network") or {}).get("centreline_m"),
                     frames=a.get("frames"), recorded=False, note=note, drop=bool(mc.get("overlap"))))

slugs_rows = {x["slug"] for x in rows}
old_slugs = {x["slug"] for x in rows if x["src"] == "old"}
excluded = [(p, n, lv) for p, n, lv in deliv_levels if slug_of(lv) not in slugs_rows]
from collections import OrderedDict
excl_by_proj = OrderedDict()
for p, n, lv in excluded: excl_by_proj.setdefault((p, n), []).append(lv.split("/")[-1])
n_deliv = len(deliv_levels); n_in_old = sum(1 for _, _, lv in deliv_levels if slug_of(lv) in old_slugs)
NO_SCENE = [("City Park Environment Collection LITE", "只有 Overview / Showcase"), ("Desert Ruins", "只有 Overview / Showcase"),
            ("Modular Asian Medieval City", "只有 L_Assets / L_Showcase_map"), ("Science Fiction Valley Town", "只有 L_assets / L_showcase_level"),
            ("Sunset - Modular Medieval Brick Buildings", "只有 AssetShowcase / ThirdPersonExampleMap"),
            ("University Classroom Interior", "只有 Overview / Showcase"), ("Lyra Starter Game", "射击游戏示例，非环境"),
            ("Dubai Downtown City", "64 GB，需 Cesium 插件；另一会话已用悬浮模式录 30 s 演示，地面无碰撞")]

def label(s): return s.removeprefix("Game_")
def fmt(v, f="{:,.0f}"): return "—" if v is None else f.format(v)
def tier(c): return "big" if (c or 0) >= 2000 else "mid" if (c or 0) >= 500 else "small"

def tr(i, x):
    tags = []
    if x["src"] == "new": tags.append('<span class="chip new">新交付</span>')
    if x["recorded"]: tags.append('<span class="chip ok">已录长视频</span>')
    if x["drop"]: tags.append('<span class="chip bad">重合/放弃</span>')
    name = html.escape(label(x["slug"]))
    if x["png"]: name = f'<a href="{x["png"]}" target="_blank">{name}</a>'
    st = STATE_CN.get(x["state"], x["state"] or "未跑")
    cls = {"几何全过": "ok", "待审(保留率低)": "mid", "未跑": "mut", "中断": "mut"}.get(st, "bad")
    return (f'<tr class="{tier(x["core"])}{" drop" if x["drop"] else ""}"><td class="n mono">{i}</td>'
            f'<td>{name} {" ".join(tags)}<div class="mut small">{html.escape(x["title"])}</div></td>'
            f'<td class="n thumbs">{f'<a href="{x["png"]}" target="_blank"><img class="th core" src="{x["png"]}" loading="lazy" alt=""></a>' if x["png"] else '<span class="th none">未测</span>'}</td>'
            f'<td class="n mono r"><b>{fmt(x["core"])}</b></td><td class="mut n">{x["core_src"]}</td>'
            f'<td class="st {cls}">{st}</td><td class="n mono r">{fmt(x["kept"], "{:.0%}")}</td>'
            f'<td class="n mono r">{fmt(x["cl"])}</td><td class="n mono r">{fmt(x["frames"] / 24 / 60 if x["frames"] else None, "{:.0f}")}</td>'
            f'<td class="mut small">{html.escape(x["note"])}</td></tr>')

rows.sort(key=lambda x: -(x["core"] if x["core"] is not None else -1))
old_rows = [x for x in rows if x["src"] == "old"]; new_rows = [x for x in rows if x["src"] == "new"]
def counts(rs):
    return dict(n=len(rs), core=sum(x["core"] is not None for x in rs), core09=sum(x["core_src"] == "09-09 调查" for x in rs),
                core16=sum(x["core_src"].startswith("09-16") for x in rs), nocore=sum(x["core"] is None for x in rs),
                passed=sum(x["state"] == "geometry_pass" for x in rs), review=sum(x["state"] == "coverage_review" for x in rs),
                drop=sum(x["drop"] for x in rs), big=sum((x["core"] or 0) >= 2000 for x in rs), mid=sum(500 <= (x["core"] or 0) < 2000 for x in rs))
co, cn = counts(old_rows), counts(new_rows)
distinct = len(old_rows) + len(new_rows) - sum(1 for x in new_rows if x["drop"])
nocore_old = [x for x in old_rows if x["core"] is None]; nocore_new = [x for x in new_rows if x["core"] is None]

# ---- project level: one row per delivered project (the unit the user calls "a map") ----
by_slug = {x["slug"]: x for x in rows}
projects = []
for r in DELIV:
    pid, name = r["项目编号"], r["资源名称"]
    lvs = [lv.strip() for lv in r["关卡路径"].split(";") if lv.strip()]
    scenes = [by_slug[slug_of(lv)] for lv in lvs if slug_of(lv) in by_slug]
    old_here = [x for x in scenes if x["src"] == "old"]
    kind = "旧" if old_here else ("新" if scenes else "无场景")
    measured = [x for x in scenes if x["core"] is not None and not x["drop"]]
    rep = max(measured, key=lambda x: x["core"]) if measured else (max(scenes, key=lambda x: (x["state"] == "geometry_pass", x["kept"] or 0)) if scenes else None)
    reason = next((b for a, b in NO_SCENE if name.startswith(a[:12])), "只有总览/素材图")
    projects.append(dict(pid=pid, name=name, kind=kind, n_levels=len(lvs), scenes=scenes, rep=rep, measured=len(measured),
                         passed=sum(x["state"] == "geometry_pass" for x in scenes), recorded=any(x["recorded"] for x in scenes),
                         core=(rep["core"] if rep and rep["core"] is not None else None), reason=reason if not scenes else ""))
projects.sort(key=lambda P: -(P["core"] if P["core"] is not None else -1))
P_old = [P for P in projects if P["kind"] == "旧"]; P_new = [P for P in projects if P["kind"] == "新"]; P_none = [P for P in projects if P["kind"] == "无场景"]
def ptr(i, P):
    rep = P["rep"]; tags = []
    tags.append(f'<span class="chip {"ok" if P["kind"] == "旧" else "new" if P["kind"] == "新" else "bad"}">{P["kind"]}</span>')
    if P["recorded"]: tags.append('<span class="chip ok">已录长视频</span>')
    repname = "—"
    if rep:
        repname = html.escape(rep["slug"].removeprefix("Game_").split("_")[-1] if False else rep["slug"].removeprefix("Game_"))
        if rep["png"]: repname = f'<a href="{rep["png"]}" target="_blank">{repname}</a>'
    st = STATE_CN.get(rep["state"], rep["state"] or "未跑") if rep else "—"
    cls = {"几何全过": "ok", "待审(保留率低)": "mid", "未跑": "mut", "中断": "mut", "—": "mut"}.get(st, "bad")
    others = len(P["scenes"]) - (1 if rep else 0)
    core_src = rep["core_src"] if rep else ""
    scene = f'<a href="scene_full/{P["pid"]}.jpg" target="_blank"><img class="th" src="scene/{P["pid"]}.jpg" loading="lazy" alt=""></a>' if (D / "scene" / f'{P["pid"]}.jpg').exists() else '<span class="th none">无截图</span>'
    corepng = f'<a href="{rep["png"]}" target="_blank"><img class="th core" src="{rep["png"]}" loading="lazy" alt=""></a>' if rep and rep["png"] else '<span class="th none">未测</span>'
    return (f'<tr class="{tier(P["core"])}"><td class="n mono">{i}</td><td>{html.escape(P["pid"])} {html.escape(P["name"][:46])} {" ".join(tags)}</td>'
            f'<td class="n thumbs">{scene}{corepng}</td><td class="small">{repname}</td><td class="n mono r"><b>{fmt(P["core"])}</b></td><td class="mut n small">{core_src}</td>'
            f'<td class="st {cls}">{st}</td><td class="n mono r">{fmt(rep["kept"], "{:.0%}") if rep else "—"}</td><td class="n mono r">{fmt(rep["cl"]) if rep else "—"}</td>'
            f'<td class="n mono r">{others if others else ""}</td><td class="mut small">{html.escape(P["reason"] or (rep["note"] if rep else ""))}</td></tr>')
pc = dict(total=len(projects), old=len(P_old), new=len(P_new), none=len(P_none),
          core=sum(P["core"] is not None for P in projects), nocore=sum(P["core"] is None and P["kind"] != "无场景" for P in projects),
          big=sum((P["core"] or 0) >= 2000 for P in projects), mid=sum(500 <= (P["core"] or 0) < 2000 for P in projects),
          passed=sum(P["passed"] > 0 for P in projects), recorded=sum(P["recorded"] for P in projects))
stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


def card(P):
    rep = P["rep"]
    scene = (f'<a href="scene_full/{P["pid"]}.jpg" target="_blank">'
             f'<img src="scene/{P["pid"]}.jpg" loading="lazy" alt=""></a>') if (D / "scene" / f'{P["pid"]}.jpg').exists() else '<div class="noimg">无截图</div>'
    st = STATE_CN.get(rep["state"], rep["state"] or "未跑") if rep else "无可录场景"
    cls = {"几何全过": "ok", "待审(保留率低)": "mid"}.get(st, "mut" if st in ("未跑", "中断", "无可录场景") else "bad")
    core = f'<b>{P["core"]:,.0f}</b> m² 核心' if P["core"] is not None else '<b>—</b> 未测出核心'
    lv = f'{len(P["scenes"])} 张可录关卡' if P["scenes"] else (P["reason"] or "只有总览/素材图")
    mini = f'<img class="mini" src="{rep["png"]}" loading="lazy" alt="">' if rep and rep["png"] else ''
    return (f'<figure class="cd"><div class="shot">{scene}{mini}</div>'
            f'<figcaption><div class="nm">{html.escape(P["name"][:52])}</div>'
            f'<div class="meta">{core} · <span class="st {cls}">{st}</span></div>'
            f'<div class="mut small">{html.escape(P["pid"])} · {html.escape(lv)}</div></figcaption></figure>')

new_cards = "".join(card(P) for P in P_new)
nocore_cards = "".join(card(P) for P in projects if P["core"] is None and P["kind"] != "无场景")
noscene_cards = "".join(card(P) for P in P_none)
old_cards = "".join(card(P) for P in P_old)
GALLERY = f'''
<section>
  <div class="sechd"><h2>这次新增的 {len(P_new)} 个地图</h2><span class="note">旧目录里没有的环境包；大图是交付验收时的实际 Vulkan 画面，右下角小图是核心示意图（绿=核心）</span></div>
  <div class="grid">{new_cards}</div>
</section>
<section>
  <div class="sechd"><h2>还没统计出核心大小的 {sum(1 for P in projects if P["core"] is None and P["kind"] != "无场景")} 个</h2><span class="note">有可录场景，但核心大小还没测出来；09-17 的补测队列正在跑这些</span></div>
  <div class="grid">{nocore_cards}</div>
</section>
<section>
  <div class="sechd"><h2>没有可录场景的 {len(P_none)} 个</h2><span class="note">只交付了总览或素材陈列图，或需要单独处理</span></div>
  <div class="grid">{noscene_cards}</div>
</section>
<section>
  <div class="sechd"><h2>之前就有的 {len(P_old)} 个</h2><span class="note">旧目录里已经统计过的环境包，按核心大小排</span></div>
  <div class="grid">{old_cards}</div>
</section>
'''
page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>地图资源总表 · 新交付 {n_deliv} 张关卡</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5b6472;--card:#fff;--line:#e2e6ec;--move:#1e6fd9;--spd:#0b8a5f;--cam:#8a5a0b;--oth:#6b7280;--bad:#c2402f;--new:#7c3aed}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272;--new:#c4a5ff}}}}
:root[data-theme="dark"]{{--bg:#0e1116;--fg:#e6e9ee;--mut:#9aa4b2;--card:#161b22;--line:#242b35;--move:#6ea8fe;--spd:#4ad6a0;--cam:#e0b060;--oth:#9aa4b2;--bad:#ff8272;--new:#c4a5ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif;overflow-x:hidden}}
header,.wrap{{max-width:1500px;margin:0 auto;padding:0 20px}}header{{padding-top:28px}}
h1{{margin:0 0 6px;font-size:23px;letter-spacing:-.01em}}h2{{margin:0;font-size:16.5px}}
.sub{{color:var(--mut);font-size:13.5px;margin:0}}.sub a,td a{{color:var(--move);text-decoration:none}}td a:hover{{text-decoration:underline}}
.totals{{display:flex;gap:22px;flex-wrap:wrap;margin-top:16px;font-size:13px;color:var(--mut)}}
.totals div b{{display:block;font-size:21px;color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}}
section{{margin-top:30px}}.sechd{{display:flex;gap:12px;align-items:baseline;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}
.sechd .note{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:baseline}}
thead th{{color:var(--mut);font-weight:500;font-size:11.5px;white-space:nowrap}}th.r,td.r{{text-align:right}}
.mono{{font-family:ui-monospace,monospace;font-size:12px;font-variant-numeric:tabular-nums}}td.n{{white-space:nowrap;width:1%}}.mut{{color:var(--mut)}}.small{{font-size:12px}}
.scroll{{overflow-x:auto}}tr.big td:first-child{{box-shadow:inset 3px 0 0 var(--spd)}}tr.small td{{color:var(--mut)}}tr.small td b{{color:var(--fg);font-weight:500}}
tr.drop td{{opacity:.55}}
.chip{{font-size:11px;padding:1px 7px;border-radius:99px;display:inline-flex;border:1px solid currentColor;white-space:nowrap;vertical-align:1px;margin-left:4px}}
.chip.ok{{color:var(--spd)}}.chip.new{{color:var(--new)}}.chip.bad{{color:var(--bad)}}
td.st.ok{{color:var(--spd)}}td.st.mid{{color:var(--cam)}}td.st.bad{{color:var(--bad)}}td.st.mut{{color:var(--mut)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;padding:12px 15px}}.card p{{margin:0 0 8px}}.card p:last-child{{margin:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}}
figure.cd{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}}
figure.cd .shot{{position:relative;aspect-ratio:16/9;background:#000}}
figure.cd .shot img{{width:100%;height:100%;object-fit:cover;display:block}}
figure.cd .shot .mini{{position:absolute;right:6px;bottom:6px;width:64px;height:48px;object-fit:contain;background:#2d2d2dcc;border:1px solid var(--line);border-radius:4px}}
figure.cd .noimg{{display:flex;align-items:center;justify-content:center;height:100%;color:var(--mut);font-size:12px}}
figure.cd figcaption{{padding:9px 11px}}figure.cd .nm{{font-weight:600;font-size:13.5px;line-height:1.35}}
figure.cd .meta{{font-size:12.5px;margin-top:3px;font-variant-numeric:tabular-nums}}
figure.cd .st{{font-size:11.5px}}.st.ok{{color:var(--spd)}}.st.mid{{color:var(--cam)}}.st.bad{{color:var(--bad)}}.st.mut{{color:var(--mut)}}
td.thumbs{{white-space:nowrap}}img.th{{height:84px;width:auto;max-width:150px;object-fit:cover;border-radius:4px;border:1px solid var(--line);vertical-align:middle;margin-right:6px;background:#000}}img.th.core{{background:#2d2d2d;object-fit:contain}}
.th.none{{display:inline-flex;align-items:center;justify-content:center;height:84px;width:120px;border:1px dashed var(--line);border-radius:4px;color:var(--mut);font-size:11px;margin-right:6px;vertical-align:middle}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px}}
</style></head><body>
<header>
  <h1>地图资源总表 · 87 个地图（环境包）</h1>
  <p class="sub"><a href="../">← 长视频采集</a> · <a href="../core/">核心大小排名（09-09）</a> · <a href="../route-validation/">86 张路线验证</a> · <a href="../new-map-validation/">新交付 55 张</a> · 生成于 {stamp}</p>
  <div class="totals">
    <div>地图（工程）<b>{pc["total"]}</b></div>
    <div>之前的旧目录里有<b>{pc["old"]}</b></div>
    <div>这次新增<b>{pc["new"]}</b></div>
    <div>没有可录场景<b>{pc["none"]}</b></div>
    <div>有核心大小<b>{pc["core"]}</b></div>
    <div>该测还没测出来<b>{pc["nocore"]}</b></div>
    <div>核心 ≥ 2000 m²<b>{pc["big"]}</b></div>
    <div>500–2000 m²<b>{pc["mid"]}</b></div>
    <div>路线几何全过<b>{pc["passed"]}</b></div>
    <div>已录长视频<b>{pc["recorded"]}</b></div>
  </div>
</header>
<div class="wrap">
{GALLERY}
<section>
  <div class="sechd"><h2>全部 87 个地图的明细表</h2><span class="note">左图：交付验收时的 Vulkan 实际画面（点开看大图）；右图：代表关卡的核心示意图，绿=核心、灰=可走非核心、橙=障碍。"其他场景"是同一地图里另外可录的关卡数</span></div>
  <div class="scroll"><table>
    <thead><tr><th>#</th><th>地图（工程）</th><th>实际画面 · 核心示意图</th><th>代表关卡</th><th class="r">核心 m²</th><th>核心来源</th><th>路线验证</th><th class="r">保留率</th><th class="r">路网 m</th><th class="r">其他场景</th><th>备注</th></tr></thead>
    <tbody>{"".join(ptr(i, P) for i, P in enumerate(projects, 1))}</tbody>
  </table></div>
</section>
<section>
  <div class="sechd"><h2>口径：一个地图 = 一个交付工程（环境包），一个工程里可以有多张关卡</h2><span class="note">下面的关卡级统计是同一批数据的展开</span></div>
  <div class="card"><p>87 个工程共列了 {n_deliv} 张已加载检查的关卡。其中 {n_in_old} 张是旧目录里的 86 张（旧目录是这次交付的子集，分布在 {pc["old"]} 个工程里）；{len(new_rows)} 张是新增的真正场景（分布在 {pc["new"]} 个新工程和 {sum(1 for P in P_old if any(x["src"] == "new" for x in P["scenes"]))} 个旧工程里）；其余 {len(excluded)} 张是总览、素材陈列、灯光/内容子层和套件零件，不可走，不算。</p>
  <p>核心大小按关卡算：旧目录 86 张里 83 张有；新增 55 张里今天补测出 41 张。折到工程上：{pc["core"]} 个地图至少有一张关卡测出了核心，{pc["nocore"]} 个有场景但还没测出来，{pc["none"]} 个没有可录场景。</p></div>
</section>
<section>
  <div class="sechd"><h2>旧目录是新交付的子集：{n_in_old} 张旧图全部在这 {n_deliv} 张里</h2><span class="note">"核心" = 有内容包围的内部可走面，定义见核心大小页；新交付的核心用 09-16 路线验证导出的同一份 navmesh 离线算，方法不变</span></div>
  <div class="grid2">
    <div class="card"><p><b>旧目录 {co["n"]} 张（新交付里已经统计过的部分）</b></p>
      <p>核心大小：09-09 调查测了 {co["core09"]} 张；当时因起点文件损坏测不了的 13 张里，本次用路线验证的 navmesh 补测了 {co["core16"]} 张，仍缺 {co["nocore"]} 张。</p>
      <p>路线验证（两遍，第二遍 45 cm 离地间隙）：几何全过 {co["passed"]}，待审 {co["review"]}，判定放弃 {co["drop"]}。已录长视频 {sum(x["recorded"] for x in old_rows)} 张。</p></div>
    <div class="card"><p><b>新增场景 {cn["n"]} 张（新交付里旧目录没有、且是真正场景的关卡）</b></p>
      <p>核心大小：全部是新的，09-09 没有测过；本次从 navmesh 导出离线补测了 {cn["core16"]} 张，{cn["nocore"]} 张还没有（未跑到、起点失败或开阔地形抽不出走廊）。</p>
      <p>路线验证：几何全过 {cn["passed"]}，待审 {cn["review"]}；与旧图重合 {cn["drop"]} 张（Courtyard 两个灯光子层、Hangar）。8 台机器已按要求停机，未跑完的保持中断状态。</p></div>
  </div>
</section>
<section>
  <div class="sechd"><h2>关卡级明细：{len(rows)} 张场景关卡，按核心大小排</h2><span class="note">点地图名看核心示意图（绿=核心，灰=可走非核心，橙=障碍）· 时长 = 一遍覆盖路线的分钟数</span></div>
  <div class="scroll"><table>
    <thead><tr><th>#</th><th>地图</th><th>核心示意图</th><th class="r">核心 m²</th><th>核心来源</th><th>路线验证</th><th class="r">保留率</th><th class="r">路网 m</th><th class="r">一遍 min</th><th>备注</th></tr></thead>
    <tbody>{"".join(tr(i, x) for i, x in enumerate(rows, 1))}</tbody>
  </table></div>
</section>
<section>
  <div class="sechd"><h2>没有核心大小的 {len(nocore_old) + len(nocore_new)} 张</h2><span class="note">要补的话：旧图需要能站住的起点，新图需要重开机器把没跑的跑完</span></div>
  <div class="grid2">
    <div class="card"><p><b>旧目录 {len(nocore_old)} 张</b></p><p class="small">{"<br>".join(html.escape(label(x["slug"])) + " · " + html.escape(STATE_CN.get(x["state"], x["state"] or "未跑")) + (" · " + html.escape(x["note"][:70]) if x["note"] else "") for x in nocore_old) or "无"}</p></div>
    <div class="card"><p><b>新交付 {len(nocore_new)} 张</b></p><p class="small">{"<br>".join(html.escape(label(x["slug"])) + " · " + html.escape(STATE_CN.get(x["state"], x["state"] or "未跑")) + (" · " + html.escape(x["note"][:70]) if x["note"] else "") for x in nocore_new) or "无"}</p></div>
  </div>
</section>
<section>
  <div class="sechd"><h2>新交付里另外 {len(excluded)} 张关卡：总览、素材陈列、灯光/内容子层、套件零件</h2><span class="note">按工程折叠；这些不是可走的场景，没有算核心大小</span></div>
  <div class="scroll"><table><thead><tr><th>工程</th><th class="r">关卡数</th><th>关卡名</th></tr></thead><tbody>{"".join(f"<tr><td>{html.escape(p)} {html.escape(n[:48])}</td><td class='n mono r'>{len(v)}</td><td class='mut small'>{html.escape(', '.join(v[:12]))}{' …' if len(v) > 12 else ''}</td></tr>" for (p, n), v in excl_by_proj.items())}</tbody></table></div>
</section>
<section>
  <div class="sechd"><h2>新交付里没有可录场景的 {len(NO_SCENE)} 个工程</h2><span class="note">只交付了总览 / 素材陈列图，或需要单独处理</span></div>
  <div class="scroll"><table><thead><tr><th>工程</th><th>原因</th></tr></thead><tbody>{"".join(f"<tr><td>{html.escape(a)}</td><td class='mut'>{html.escape(b)}</td></tr>" for a, b in NO_SCENE)}</tbody></table></div>
</section>
</div></body></html>'''
(D / "index.html").write_text(page)
print("wrote", D / "index.html", len(rows), "rows; core:", co["core"] + cn["core"], "nocore:", co["nocore"] + cn["nocore"], "distinct:", distinct)
