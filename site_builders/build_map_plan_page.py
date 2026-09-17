#!/usr/bin/env python3
"""Sub-page /longvideo/map-plan/ and revisit_pipeline/MAP_PLAN_2026-09-17.md: the recording plan, BY LEVEL.

Unit (user's decision, 2026-09-17): one level (.umap) = one map = one episode. Every statistic is over
the levels that can be recorded directly; resource packs are only named for orientation.
Inputs: <scratchpad>/levels_plan.json (built from the delivery checklist, the three validation result
sets, the core surveys and the recorded-episode list). Rebuild:   python3 build_map_plan_page.py
"""
import json, html, time, sys
from pathlib import Path
S = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/claude-1000/-home-ubuntu-UE5-Agent-Data/ef273e00-f904-4c7f-a0cb-55c0cd005dbc/scratchpad/levels_plan.json')
LV = json.load(open(S))
SITE = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/map-plan'); SITE.mkdir(parents=True, exist_ok=True)
REPO = Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline'); RES = REPO / 'results' / 'map_plan_2026-09-17'; RES.mkdir(parents=True, exist_ok=True)
json.dump(LV, open(RES / 'levels.json', 'w'), ensure_ascii=False, indent=1)
CLR = {}
for mf in ['/home/ubuntu/ue_route_validation_20260916/manifest.json', '/home/ubuntu/ue_newmap_validation_20260916/manifest.json', '/home/ubuntu/ue_newroute_20260917/manifest.json']:
    for e in json.load(open(mf)): CLR[e['slug']] = e.get('ground_clearance_cm')
ORDER = ['A 可直接录', 'B 待审', 'C 重跑', 'D 修路线', 'E 未验证', 'G 放弃', 'H 未挑选', 'F 排除']
EXPL = {'A 可直接录': '路线几何全过（冻结、碰撞、深度探针），或已有录制。', 'B 待审': '路线跑通但路网保留率低，要人看俯视图决定是否接受。',
        'C 重跑': '上次失败原因在工具侧（超时、加载、连接），已修；09-17 队列正在重跑。', 'D 修路线': '路线失败，起点/导航/资产要逐图修；09-17 队列换种子重试中。',
        'E 未验证': '按名字误判为素材图，其实是真场景；09-17 队列在跑。', 'G 放弃': '路线跑过但地图太小或结构不合适，16 Sep 判定放弃。',
        'H 未挑选': '总览图、素材陈列、灯光子层、套件零件，从未排进验证。', 'F 排除': 'Dubai（需 Cesium，单独立项）61 张、Lyra（射击示例）22 张。'}
def n(v, f='{:,.0f}'): return '—' if v is None else f.format(v)
A = sorted([x for x in LV if x['cls'].startswith('A')], key=lambda x: (-(x.get('episode_h') or 0), -(x['core'] or 0)))
tot = dict(ep=sum(x['episode_h'] for x in A), mh=sum(x['machine_h'] for x in A), gb=sum(x['gb'] for x in A))
rec = sum(1 for x in A if x['recorded']); big = sum(1 for x in A if x['tier'] == '大'); mid = sum(1 for x in A if x['tier'] == '中'); small = len(A) - big - mid
lowcore = sum(1 for x in A if (x['core'] or 0) < 50)
now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
# ---- network / route thumbnails: newest engine-checked plot available for each level ----
import glob, shutil
SL = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo'); (SITE / 'plots').mkdir(exist_ok=True)
def thumb(slug):
    for pat, tag in [(f'{SL}/route-queue-20260917/plots/{slug}.png', '09-17 队列，引擎验证路线'), (f'{SL}/route-validation/plots/{slug}_*.png', '09-16 验证，引擎验证路线'),
                     (f'{SL}/new-map-validation/plots/{slug}_*.png', '09-16 新地图验证，引擎验证路线'), (f'{SL}/route-audit/plots/{slug}.png', '09-16 审查草案（离线）'),
                     (f'{SL}/route-audit/plots/{slug}_draft.png', '09-16 审查草案（离线）'), (f'{SL}/core/png/{slug}.png', '核心图（绿=核心，灰=可走）'), (f'{SL}/inventory/png/{slug}.png', '核心图')]:
        g = sorted(glob.glob(pat))
        if g:
            dst = SITE / 'plots' / f'{slug}.png'
            if not dst.exists() or Path(g[-1]).stat().st_mtime > dst.stat().st_mtime: shutil.copy2(g[-1], dst)
            return f'plots/{slug}.png', tag
    return None, None
# ---- per resource pack (reference only; every headline number is per level) ----
RANK = {c: i for i, c in enumerate(ORDER)}
packs = {}
for x in LV:
    P = packs.setdefault(x['fid'], dict(fid=x['fid'], title=x['title'], levels=0, a=0, ep=0.0, mh=0.0, cls=None))
    P['levels'] += 1
    if x['cls'].startswith('A'): P['a'] += 1; P['ep'] += x['episode_h']; P['mh'] += x['machine_h']
    if P['cls'] is None or RANK[x['cls']] < RANK[P['cls']]: P['cls'] = x['cls']
PK = sorted(packs.values(), key=lambda P: (RANK[P['cls']], -P['ep']))
pk_cnt = {c: sum(1 for P in PK if P['cls'] == c) for c in ORDER}
pk_a = [P for P in PK if P['cls'].startswith('A')]
RULE = ("每集规则（09-17 定）：**每张地图一集，走两遍，不封顶，不切分片。** 第一遍是覆盖遍（stroll / survey / inspect 每 45 m 轮换，按预算 97% 配速，落后时先保覆盖），"
        "第二遍是填充遍（从第一遍终点重抽一次覆盖走法，整遍 study 风格，动作密度加倍，路程相同，约 2× 第一遍）。成片按一遍估计的 **3×** 算，一遍估计取自路线验证的完整覆盖走法帧数；机器上另有 2× 的失控保护上限。")
PHASES = ["**阶段 0，补验证（20 台，跑着）**：C 类 41 张、E 类 6 张、D 类 5 张在 09-17 队列里；B 类 4 张人工看俯视图。通过的进 A 类。",
          "**阶段 1，验收片**：A 类每张录 2 分钟（同一冻结路线的开头），看曝光、闪动、穿模、天空空帧。抗锯齿和步速 09-17 刚改，没有一张图在新设置下录过。",
          "**阶段 2，正式录制**：拉取队列，大图先领小图填缝，每张地图一集两遍，跑完自动关机。",
          "**阶段 3，追加**：阶段 0 通过的按同样流程补进来。"]
# ---------- markdown ----------
md = ["# 全部交付地图的录制规划（按关卡）", "", f"2026-09-17。口径：**一张关卡 = 一个地图 = 一集**；所有数字以可直接录的关卡数为基数。参数按 09-17 定的：TAA 2×、步速 1 m/s、转速 45°/s、离地间隙按图分 6 / 45 cm、折返 5 分钟一次。", "",
      "## 预算（可直接录的 %d 张）" % len(A), "",
      "| | 值 |", "|---|---:|", f"| 可直接录 | {len(A)} 张（其中 {rec} 张已录过旧设置版本） |", f"| 成片总时长 | {tot['ep']:.0f} h |", f"| 集数 | {len(A)} |",
      f"| 机时（实时 2.5 倍） | {tot['mh']:.0f} h |", f"| 10 台 | {tot['mh']/240:.1f} 天 |", f"| 20 台 | {tot['mh']/480:.1f} 天 |", f"| 存储（33.5 GB/成片小时，StorageHouse 实测） | {tot['gb']:,} GB |",
      f"| 大 / 中 / 小（成片 ≥8 h / ≥2 h / <2 h） | {big} / {mid} / {small} |", f"| 核心 <50 m²（开阔地形或单间，内容价值低） | {lowcore} |", "", RULE, "",
      "**路网列的含义**：路网 m = 选定区域中心线总长，覆盖走法要把它每条路走一遍（实际约重走 1.6 倍）；路数 = 中心线被路口切成的段数；保留率 = 头部净空剪枝后剩下的中心线比例，剪掉的路不在覆盖范围内。录制时长跟路网走，不跟核心走。", "", "## 执行顺序", ""] + [f"{i}. {p}" for i, p in enumerate(PHASES, 1)] + ["",
      "## 442 张交付关卡的去向", "", "| 分类 | 张数 | 说明 |", "|---|---:|---|"]
for c in ORDER: md.append(f"| {c} | {sum(1 for x in LV if x['cls']==c)} | {EXPL[c]} |")
md += ["", f"真正的可录场景 = 442 − H 212 − F 83 = **147 张**；现在能录 {len(A)} 张，最多能到 {147 - sum(1 for x in LV if x['cls']=='G 放弃')} 张。", "",
       f"## A 类：可直接录的 {len(A)} 张（按成片时长降序）", "", "| # | 资源包 | 关卡 | 核心 m² | 路网 m | 路数 | 保留率 | 间隙 | 一遍 min | 成片 h | 机时 | 备注 |", "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
for i, x in enumerate(A, 1):
    note = ('已录 ' if x['recorded'] else '') + ('核心≈0 ' if (x['core'] or 0) < 50 else '')
    md.append(f"| {i} | {x['fid']} {x['title'][:30]} | `{x['level'].split('/')[-1]}` | {n(x['core'])} | {n(x.get('cl'))} | {n(x.get('roads'))} | {n(x.get('kept'), '{:.0%}')} | {n(CLR.get(x['slug']))} | {n(x.get('one_pass_min'), '{:.0f}')} | {x['episode_h']:.1f} | {x['machine_h']:.0f} | {note.strip()} |")
for c in ORDER[1:6]:
    rows = [x for x in LV if x['cls'] == c]
    if not rows: continue
    md += ["", f"## {c}（{len(rows)} 张）", "", EXPL[c], "", "| 资源包 | 关卡 | 最近状态 | 核心 m² | 路网 m | 保留率 |", "|---|---|---|---:|---:|---:|"]
    for x in rows: md.append(f"| {x['fid']} {x['title'][:34]} | `{x['level'].split('/')[-1]}` | {x['state'] or '未跑'} | {n(x['core'])} | {n(x.get('cl'))} | {n(x.get('kept'), '{:.0%}')} |")
md += ["", "## 路网缩略图（A 类，按成片时长降序）", "", "每张是引擎验证过的路线叠在同一会话导出的导航网上：青线 = 路线，黄点 = 起点，灰面 = 可走导航网。图在页面 `/longvideo/map-plan/` 上看。", ""]
for x in A:
    u, tag = thumb(x['slug'])
    md.append(f"- {x['title'][:30]} / `{x['level'].split('/')[-1]}`：" + (f"[{tag}]({u})" if u else "无图"))
md += ["", "## 附录：按资源包的统计（仅供参考）", "", "主统计按关卡。这里把同一资源包的关卡合并，只用来对照采购单位；一个包的分类取其关卡里最好的一档。", "",
       "| 分类 | 资源包数 |", "|---|---:|"] + [f"| {c} | {pk_cnt[c]} |" for c in ORDER if pk_cnt[c]] + ["",
       f"有可录关卡的资源包 {len(pk_a)} 个，合计可录关卡 {sum(P['a'] for P in pk_a)} 张、成片 {sum(P['ep'] for P in pk_a):.0f} h、机时 {sum(P['mh'] for P in pk_a):.0f} h。若每包只录一张代表关卡，则为 {len(pk_a)} 集。", "",
       "| 资源包 | 关卡数 | 可录关卡 | 成片 h | 机时 | 分类 |", "|---|---:|---:|---:|---:|---|"] + [f"| {P['fid']} {P['title'][:40]} | {P['levels']} | {P['a']} | {P['ep']:.1f} | {P['mh']:.0f} | {P['cls']} |" for P in PK if P['cls'] != 'H 未挑选' or P['a']]
md += ["", "## 单独立项，不在上表", "", "- **Dubai Downtown**：64 GB，需 Cesium；悬浮模式录过 30 s 演示，地面无碰撞，要录先解决碰撞。", "- **Lyra**：射击示例，非环境。",
       "- **Factory Environment Collection 的 Demonstration**：资产全在通用目录（Maps、Meshes、Materials…），合不进 gym_citynav，要单独立工程。",
       "- **AdditionalSamples**：CitySample 82 GB / 439 张关卡是完整城市，值得单独评估；另两个不是环境。", "",
       "## 数据来源", "", f"`results/map_plan_2026-09-17/levels.json`（本表的机器可读版，每张关卡一行）。验证结果来自 `~/ue_route_validation_20260916`、`~/ue_newmap_validation_20260916`、`~/ue_newroute_fleet_20260916`、`~/ue_newroute_fleet_20260917`；核心面积来自 09-09 调查、09-16 离线补测、09-17 队列；已录列表来自 8500 的 `core/recorded_slugs.json`。页面 `/longvideo/map-plan/`，生成于 {now}。"]
(REPO / 'MAP_PLAN_2026-09-17.md').write_text('\n'.join(md) + '\n')
# ---------- html ----------
css = "body{font-family:system-ui,sans-serif;margin:24px;max-width:1500px;color:#222}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #ddd;padding:4px 7px;text-align:left;vertical-align:top}th{background:#f3f3f3;position:sticky;top:0}td.r{text-align:right}tr.big td{background:#fff6e5}tr.low td{color:#888}.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:12px}.A{background:#d9f2d9}.B{background:#fff1b8}.C{background:#ffe0cc}.D{background:#ffd6d6}.E{background:#e0e8ff}.G,.F,.H{background:#eee}h2{margin-top:32px}.mut{color:#777;font-size:12px}.kv td:first-child{color:#555}"
h = [f"<!doctype html><meta charset=utf-8><title>地图录制规划 · 按关卡</title><style>{css}</style>",
     f"<h1>全部交付地图的录制规划（按关卡）</h1><p class=mut>2026-09-17 · 一张关卡 = 一个地图 = 一集；所有数字以可直接录的关卡数为基数 · 生成于 {now} · <a href='../flicker-fix/recording-config.html'>录制配置</a> · <a href='../route-queue-20260917/'>重新验证队列（实时）</a> · <a href='../inventory/'>资源总表</a></p>",
     f"<h2>预算（可直接录的 {len(A)} 张）</h2><table class=kv>",
     f"<tr><td>可直接录</td><td class=r>{len(A)} 张（其中 {rec} 张已录过旧设置版本）</td></tr><tr><td>成片总时长</td><td class=r>{tot['ep']:.0f} h</td></tr><tr><td>集数</td><td class=r>{len(A)}</td></tr>",
     f"<tr><td>机时（实时 2.5 倍）</td><td class=r>{tot['mh']:.0f} h</td></tr><tr><td>10 台 / 20 台</td><td class=r>{tot['mh']/240:.1f} 天 / {tot['mh']/480:.1f} 天</td></tr><tr><td>存储（33.5 GB/成片小时，StorageHouse 实测）</td><td class=r>{tot['gb']:,} GB</td></tr>",
     f"<tr><td>大 / 中 / 小（成片 ≥8 h / ≥2 h / &lt;2 h）</td><td class=r>{big} / {mid} / {small}</td></tr><tr><td>核心 &lt;50 m²（开阔地形或单间）</td><td class=r>{lowcore}</td></tr></table>",
     "<p>" + RULE.replace('**', '') + "</p>", "<p><b>路网列的含义</b>：路网 m = 选定区域中心线总长，覆盖走法要把它每条路走一遍（实际约重走 1.6 倍）；路数 = 中心线被路口切成的段数；保留率 = 头部净空剪枝后剩下的中心线比例，剪掉的路不在覆盖范围内。录制时长跟路网走，不跟核心走。</p>", "<h2>执行顺序</h2><ol>" + ''.join(f"<li>{p.replace('**','')}</li>" for p in PHASES) + "</ol>",
     "<h2>442 张交付关卡的去向</h2><table><tr><th>分类</th><th>张数</th><th>说明</th></tr>"]
for c in ORDER: h.append(f"<tr><td><span class='tag {c[0]}'>{c}</span></td><td class=r>{sum(1 for x in LV if x['cls']==c)}</td><td>{EXPL[c]}</td></tr>")
h.append(f"</table><p>真正的可录场景 = 442 − H 212 − F 83 = <b>147 张</b>；现在能录 {len(A)} 张，最多能到 {147 - sum(1 for x in LV if x['cls']=='G 放弃')} 张。</p>")
h.append(f"<h2>A 类：可直接录的 {len(A)} 张（按成片时长降序）</h2><table><tr><th>#</th><th>资源包</th><th>关卡</th><th>核心 m²</th><th>路网 m</th><th>路数</th><th>保留率</th><th>间隙</th><th>一遍 min</th><th>成片 h</th><th>机时</th><th>备注</th></tr>")
for i, x in enumerate(A, 1):
    cls = 'big' if x['tier'] == '大' else 'low' if (x['core'] or 0) < 50 else ''
    note = ('<b>已录</b> ' if x['recorded'] else '') + ('核心≈0' if (x['core'] or 0) < 50 else '')
    h.append(f"<tr class='{cls}'><td class=r>{i}</td><td>{html.escape(x['title'])}<div class=mut>{x['fid']}</div></td><td><code>{html.escape(x['level'])}</code></td><td class=r>{n(x['core'])}</td><td class=r>{n(x.get('cl'))}</td><td class=r>{n(x.get('roads'))}</td><td class=r>{n(x.get('kept'),'{:.0%}')}</td><td class=r>{n(CLR.get(x['slug']))}</td><td class=r>{n(x.get('one_pass_min'),'{:.0f}')}</td><td class=r>{x['episode_h']:.1f}</td><td class=r>{x['machine_h']:.0f}</td><td>{note}</td></tr>")
h.append("</table>")
for c in ORDER[1:6]:
    rows = [x for x in LV if x['cls'] == c]
    if not rows: continue
    h.append(f"<h2><span class='tag {c[0]}'>{c}</span> {len(rows)} 张</h2><p>{EXPL[c]}</p><table><tr><th>资源包</th><th>关卡</th><th>最近状态</th><th>核心 m²</th><th>路网 m</th><th>保留率</th></tr>")
    for x in rows: h.append(f"<tr><td>{html.escape(x['title'])}<div class=mut>{x['fid']}</div></td><td><code>{html.escape(x['level'])}</code></td><td>{x['state'] or '未跑'}</td><td class=r>{n(x['core'])}</td><td class=r>{n(x.get('cl'))}</td><td class=r>{n(x.get('kept'),'{:.0%}')}</td></tr>")
    h.append("</table>")
h.append("<h2>单独立项</h2><ul><li><b>Dubai Downtown</b>：64 GB，需 Cesium；悬浮模式录过 30 s 演示，地面无碰撞，要录先解决碰撞。</li><li><b>Lyra</b>：射击示例，非环境。</li><li><b>Factory Environment Collection / Demonstration</b>：资产全在通用目录，合不进 gym_citynav，要单独立工程。</li><li><b>AdditionalSamples</b>：CitySample 82 GB / 439 张关卡是完整城市，值得单独评估；另两个不是环境。</li></ul>")
h.append("<h2>路网缩略图（A 类，按成片时长降序）</h2><p>引擎验证过的路线叠在同一会话导出的导航网上：青线 = 路线，黄点 = 起点，灰面 = 可走导航网。点图看大图。</p><div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px'>")
for i, x in enumerate(A, 1):
    u, tag = thumb(x['slug'])
    cap = f"<div style='font-size:12px;margin-top:4px'><b>{i}. {html.escape(x['title'][:34])}</b> · <code>{html.escape(x['level'].split('/')[-1])}</code><br><span class=mut>核心 {n(x['core'])} m² · 路网 {n(x.get('cl'))} m · 保留 {n(x.get('kept'),'{:.0%}')} · 一遍 {n(x.get('one_pass_min'),'{:.0f}')} min · {tag or '无图'}</span></div>"
    h.append(f"<div style='background:#fff;border:1px solid #e5e5e5;border-radius:6px;padding:6px'>" + (f"<a href='{u}'><img src='{u}' style='width:100%;border-radius:4px' loading='lazy'></a>" if u else "<div style='height:200px;background:#f3f3f3;display:flex;align-items:center;justify-content:center;color:#999'>无图</div>") + cap + "</div>")
h.append("</div>")
h.append("<h2>附录：按资源包的统计（仅供参考）</h2><p>主统计按关卡。这里把同一资源包的关卡合并，只用来对照采购单位；一个包的分类取其关卡里最好的一档。</p><table><tr><th>分类</th><th>资源包数</th></tr>" + ''.join(f"<tr><td><span class='tag {c[0]}'>{c}</span></td><td class=r>{pk_cnt[c]}</td></tr>" for c in ORDER if pk_cnt[c]) + "</table>"
         f"<p>有可录关卡的资源包 {len(pk_a)} 个，合计可录关卡 {sum(P['a'] for P in pk_a)} 张、成片 {sum(P['ep'] for P in pk_a):.0f} h、机时 {sum(P['mh'] for P in pk_a):.0f} h。若每包只录一张代表关卡，则为 {len(pk_a)} 集。</p>"
         "<table><tr><th>资源包</th><th>关卡数</th><th>可录关卡</th><th>成片 h</th><th>机时</th><th>分类</th></tr>" + ''.join(f"<tr><td>{html.escape(P['title'])}<div class=mut>{P['fid']}</div></td><td class=r>{P['levels']}</td><td class=r>{P['a']}</td><td class=r>{P['ep']:.1f}</td><td class=r>{P['mh']:.0f}</td><td><span class='tag {P['cls'][0]}'>{P['cls']}</span></td></tr>" for P in PK if P['cls'] != 'H 未挑选' or P['a']) + "</table>")
h.append("<p class=mut>机器可读版：<code>revisit_pipeline/results/map_plan_2026-09-17/levels.json</code>；文档：<code>revisit_pipeline/MAP_PLAN_2026-09-17.md</code>。</p>")
(SITE / 'index.html').write_text('\n'.join(h))
print('written', len(A), 'recordable levels;', round(tot['ep']), 'h episodes,', round(tot['mh']), 'machine-h,', tot['gb'], 'GB')
