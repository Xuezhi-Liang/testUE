#!/usr/bin/env python3
"""Sub-page /longvideo/map-plan/ and revisit_pipeline/MAP_PLAN_2026-09-17.md: the per-PROJECT recording plan.

One row per delivered project (资源包), not per level: the user decided on 2026-09-17 that planning is by
map/project. Inputs: the delivery checklist, the three validation result sets, the core surveys, and the
recorded-episode list. Rebuild after any validation run:   python3 build_map_plan_page.py
"""
import json, html, time, math, sys
from pathlib import Path
S = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/claude-1000/-home-ubuntu-UE5-Agent-Data/ef273e00-f904-4c7f-a0cb-55c0cd005dbc/scratchpad/projects_plan.json')
P = json.load(open(S))
# ground clearance the validated route used, from the three job manifests
CLR = {}
for mf in ['/home/ubuntu/ue_route_validation_20260916/manifest.json', '/home/ubuntu/ue_newmap_validation_20260916/manifest.json', '/home/ubuntu/ue_newroute_20260916/manifest.json']:
    m = json.load(open(mf)); m = m if isinstance(m, list) else m.get('maps') or m.get('tasks')
    for e in m: CLR[e['slug']] = e.get('ground_clearance_cm')
for p in P: p['best']['clr'] = CLR.get(p['best']['slug'])
LV = json.load(open(S.parent / 'levels_plan.json'))
LA = sorted([x for x in LV if x['cls'].startswith('A')], key=lambda x: (-(x.get('episode_h') or 0), -(x['core'] or 0)))
ltot = dict(ep=sum(x['episode_h'] for x in LA), sh=sum(x['shards'] for x in LA), mh=sum(x['machine_h'] for x in LA), gb=sum(x['gb'] for x in LA))
LORDER = ['A 可直接录', 'B 待审', 'C 重跑', 'D 修路线', 'E 未验证', 'G 放弃', 'H 未挑选', 'F 排除']
SITE = Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/map-plan'); SITE.mkdir(parents=True, exist_ok=True)
REPO = Path('/home/ubuntu/UE5-Agent-Data/revisit_pipeline')
RES = REPO / 'results' / 'map_plan_2026-09-17'; RES.mkdir(parents=True, exist_ok=True)
json.dump(P, open(RES / 'projects.json', 'w'), ensure_ascii=False, indent=1)
json.dump(LV, open(RES / 'levels.json', 'w'), ensure_ascii=False, indent=1)
ORDER = ['A 可直接录', 'B 待审', 'C 重跑', 'E 未验证', 'G 放弃', 'F 排除']
EXPL = {'A 可直接录': '至少一张关卡路线几何全过（冻结、碰撞、深度探针），或已有录制。',
        'B 待审': '路线跑通但路网保留率低，要人看俯视图决定是否接受。',
        'C 重跑': '上次失败原因在工具侧（超时、加载、连接），已修，重跑即可。',
        'E 未验证': '按名字误判为素材图，其实是真场景；已排进补测队列但没跑到。',
        'G 放弃': '路线跑过但地图太小或结构不合适，16 Sep 判定放弃；可用别的风格再试，不在本轮。',
        'F 排除': '不是普通环境包。'}
LEXPL = dict(EXPL, **{'D 修路线': '路线失败，起点/导航/资产要逐图修。', 'H 未挑选': '总览图、素材陈列、灯光子层、套件零件，从未排进验证。'})
def n(v, f='{:,.0f}'): return '—' if v is None else f.format(v)
A = [p for p in P if p['cls'].startswith('A')]
tot = dict(ep=sum(p.get('episode_h', 0) for p in A), mh=sum(p.get('machine_h', 0) for p in A), sh=sum(p.get('shards', 0) for p in A), gb=sum(p.get('gb', 0) for p in A))
for p in A: p['_k'] = -(p.get('episode_h') or 0)
A.sort(key=lambda p: (p['_k'], -(p['best']['core'] or 0)))

# ---------- markdown ----------
md = [f"# 全部交付地图的录制规划（按项目）", "", f"2026-09-17。范围：`~/new_map/Projects` 的 87 个资源包，每个包选一张代表关卡录，不按关卡数计。",
      "参数按今天定的：TAA 2×、步速 1 m/s、转速 45°/s、离地间隙按图分 6 / 45 cm、折返 5 分钟一次。", "",
      "## 总览：442 张关卡的去向", "", "| 分类 | 张数 | 说明 |", "|---|---:|---|"]
for c in LORDER: md.append(f"| {c} | {sum(1 for x in LV if x['cls']==c)} | {LEXPL[c]} |")
md += ["", f"**A 类 {len(A)} 个项目的正式录制预算**：成片 {tot['ep']:.0f} h，{tot['sh']} 集，按实时 2.5 倍算 {tot['mh']:.0f} 机时；"
       f"10 台约 {tot['mh']/10/24:.1f} 天，20 台约 {tot['mh']/20/24:.1f} 天。存储按 1.5 GB/成片小时估 {tot['gb']:,} GB。",
       "每集规则（09-17 改）：**每张地图走两遍，一集，不封顶，不切分片**。一遍耗时取自路线验证时的完整覆盖走法帧数；Tokyo 实测相邻两遍耗时交替为巡游的 ~4× 和 ~8×，所以两遍可能到一遍估计的 3×，表里按 2× 算，机器上另有 3× 的失控保护。", "",
       "## 执行顺序", "",
       "1. **阶段 0，补验证（10 台，约 1 天）**：E 类 6 个、C 类 10 个重开补测队列，只跑各自的代表关卡；B 类 2 个人工看俯视图。队列代码不用改，清单缩到这 18 个项目。",
       "2. **阶段 1，验收片（10 台，半天）**：A 类每个项目录 2 分钟（同一冻结路线的开头），看曝光、闪动、穿模、天空空帧。今天改了抗锯齿和步速，没有一张图在新设置下录过，这一步不能省。",
       "3. **阶段 2，正式录制**：按分片表用拉取队列跑，大图先开、小图填缝（LPT）。每张地图一集两遍，跑完自动关机。",
       "4. **阶段 3，追加**：阶段 0 通过的项目按同样流程补进来。",
       "", "## 对照：按资源包，A 类 61 个（每包一张代表关卡）", "",
       "| # | 项目 | 代表关卡 | 核心 m² | 间隙 | 一遍 min | 成片 h | 集 | 机时 | 需同步的包 | 备注 |", "|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|"]
for i, p in enumerate(A, 1):
    b = p['best']
    md.append(f"| {i} | {p['fid']} {p['title'][:36]} | `{b['level'].split('/')[-1]}` | {n(b['core'])} | {n(b.get('clr'))} | {n(p.get('one_pass_min'), '{:.0f}')} | {n(p.get('episode_h'), '{:.1f}')} | {p.get('shards','—')} | {n(p.get('machine_h'), '{:.0f}')} | {', '.join(p['packs_missing']) or '—'} | {p.get('note','')}{' 已录' if p['recorded'] else ''} |")
for c in ORDER[1:]:
    rows = [p for p in P if p['cls'] == c]
    if not rows: continue
    md += ["", f"## {c}", "", EXPL[c], "", "| 项目 | 代表关卡 | 最近状态 | 核心 m² | 需同步的包 | 备注 |", "|---|---|---|---:|---|---|"]
    for p in rows:
        b = p['best']
        md.append(f"| {p['fid']} {p['title'][:40]} | `{b['level'].split('/')[-1]}` | {b['state'] or '未跑'} | {n(b['core'])} | {', '.join(p['packs_missing']) or '—'} | {p.get('note','')} |")
md += ["", "## 两种统计口径（录制口径 = 按关卡）", "",
       "| 口径 | 可直接录 | 成片 h | 集 | 机时 | 10 台天数 | 存储 GB |", "|---|---:|---:|---:|---:|---:|---:|",
       f"| 按资源包（每包一张代表关卡） | {len(A)} | {tot['ep']:.0f} | {tot['sh']} | {tot['mh']:.0f} | {tot['mh']/240:.1f} | {tot['gb']:,} |",
       f"| 按关卡（每个通过的场景都录） | {len(LA)} | {ltot['ep']:.0f} | {ltot['sh']} | {ltot['mh']:.0f} | {ltot['mh']/240:.1f} | {ltot['gb']:,} |",
       "", f"按关卡的 {len(LA)} 张分布在 {len({x['fid'] for x in LA})} 个资源包里；多出来的 {len(LA)-len(A)} 张是同一个包里的第二、第三个场景（日景夜景、不同街区、室内室外）。", "",
       "### 对照：87 个资源包的去向", "", "| 分类 | 个数 | 说明 |", "|---|---:|---|"]
for c in ORDER: md.append(f"| {c} | {sum(1 for p in P if p['cls']==c)} | {EXPL[c]} |")
md += ["", f"### 按关卡：可直接录的 {len(LA)} 张（按成片时长降序）", "", "| # | 资源包 | 关卡 | 核心 m² | 一遍 min | 成片 h | 集 | 机时 | 备注 |", "|---:|---|---|---:|---:|---:|---:|---:|---|"]
for i, x in enumerate(LA, 1):
    md.append(f"| {i} | {x['fid']} {x['title'][:30]} | `{x['level'].split('/')[-1]}` | {n(x['core'])} | {n(x.get('one_pass_min'), '{:.0f}')} | {x['episode_h']:.1f} | {x['shards']} | {x['machine_h']:.0f} | {'已录' if x['recorded'] else ''} |")
md += ["", "## 单独立项，不在上表", "",
       "- **Dubai Downtown（Fab_018）**：64 GB，需 Cesium 插件；另一会话用悬浮模式录过 30 s 演示，地面无碰撞。要录得先解决地面碰撞。",
       "- **Lyra（Fab_038）**：射击游戏示例，关卡全是测试图。",
       "- **AdditionalSamples 三个官方示例**：CitySample 82 GB、439 张关卡，是完整城市，值得单独评估但体量和插件都不同；GameAnimationSample、MetaHumanCrowdSample 不是环境。",
       "", "## 数据来源", "", "`results/map_plan_2026-09-17/projects.json`（本表的机器可读版）；验证结果来自 `~/ue_route_validation_20260916`、`~/ue_newmap_validation_20260916`、`~/ue_newroute_fleet_20260916`；核心面积来自 09-09 调查、09-16 离线补测、09-17 队列；已录列表来自 8500 的 `core/recorded_slugs.json`。页面：`/longvideo/map-plan/`。"]
def _reorder(lines, a_key, l_key, end_key):
    ia = next(i for i, x in enumerate(lines) if a_key in x); il = next(i for i, x in enumerate(lines) if l_key in x); ie = next(i for i, x in enumerate(lines) if end_key in x)
    return lines[:ia] + lines[il:ie] + lines[ia:il] + lines[ie:]
md = _reorder(md, "## 对照：按资源包", "## 两种统计口径（录制口径 = 按关卡）", "## 单独立项")
md[0] = "# 全部交付地图的录制规划（按关卡）"
md[2] = "2026-09-17。口径：**从录制的角度数，一张关卡就是一个地图，一集视频**；87 个资源包展开成 442 张关卡，逐张归类。资源包视图保留在后面作对照。"
(REPO / 'MAP_PLAN_2026-09-17.md').write_text('\n'.join(md) + '\n')

# ---------- html ----------
css = ".H{background:#f7f7f7}.D{background:#ffd6d6}body{font-family:system-ui,sans-serif;margin:24px;max-width:1500px;color:#222}table{border-collapse:collapse;font-size:13px;width:100%}th,td{border:1px solid #ddd;padding:4px 7px;text-align:left;vertical-align:top}th{background:#f3f3f3;position:sticky;top:0}td.r{text-align:right}tr.big td{background:#fff6e5}tr.low td{color:#888}.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:12px}.A{background:#d9f2d9}.B{background:#fff1b8}.C{background:#ffe0cc}.E{background:#e0e8ff}.G{background:#eee}.F{background:#eee}h2{margin-top:32px}.mut{color:#777;font-size:12px}"
h = [f"<!doctype html><meta charset=utf-8><title>地图录制规划 · 按关卡</title><style>{css}</style>",
     f"<h1>全部交付地图的录制规划（按项目）</h1><p class=mut>2026-09-17 · 87 个资源包，每包一张代表关卡 · 生成于 {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · <a href='../inventory/'>资源总表</a> · <a href='../route-queue/'>补测队列</a></p>",
     "<p>参数按今天定的：TAA 2×、步速 1 m/s、转速 45°/s、离地间隙按图分 6 / 45 cm、折返 5 分钟一次。</p>",
     "<h2>总览：442 张关卡的去向</h2><table><tr><th>分类</th><th>张数</th><th>说明</th></tr>"]
for c in LORDER: h.append(f"<tr><td><span class='tag {c[0]}'>{c}</span></td><td class=r>{sum(1 for x in LV if x['cls']==c)}</td><td>{LEXPL[c]}</td></tr>")
h.append("</table>")
h.append(f"<p><b>A 类 {len(A)} 个项目的正式录制预算</b>：成片 {tot['ep']:.0f} h，{tot['sh']} 集，实时 2.5 倍计 {tot['mh']:.0f} 机时；10 台约 {tot['mh']/10/24:.1f} 天，20 台约 {tot['mh']/20/24:.1f} 天；存储约 {tot['gb']:,} GB。每集规则（09-17 改）：每张地图走两遍，一集，不封顶，不切分片；两遍实际可能到一遍估计的 3×。</p>")
h.append("<h2>执行顺序</h2><ol><li><b>阶段 0，补验证（10 台，约 1 天）</b>：E 类 6 个、C 类 10 个重开补测队列，只跑代表关卡；B 类 2 个人工看俯视图。</li><li><b>阶段 1，验收片（10 台，半天）</b>：A 类每项目录 2 分钟看曝光、闪动、穿模、天空空帧。抗锯齿和步速今天刚改，没有一张图在新设置下录过。</li><li><b>阶段 2，正式录制</b>：拉取队列，大图先开小图填缝，每张地图一集两遍，跑完自动关机。</li><li><b>阶段 3，追加</b>：阶段 0 通过的按同样流程补进来。</li></ol>")
h.append(f"<h2>对照：按资源包，A 类 {len(A)} 个（每包一张代表关卡）</h2><table><tr><th>#</th><th>项目</th><th>代表关卡</th><th>核心 m²</th><th>间隙</th><th>一遍 min</th><th>成片 h</th><th>集</th><th>机时</th><th>需同步的包</th><th>备注</th></tr>")
for i, p in enumerate(A, 1):
    b = p['best']; cls = 'big' if p.get('tier','').startswith('大') else 'low' if (b['core'] or 0) < 50 else ''
    h.append(f"<tr class='{cls}'><td class=r>{i}</td><td><b>{html.escape(p['title'])}</b><div class=mut>{p['fid']} · {html.escape(p['project'] or '')}</div></td><td><code>{html.escape(b['level'])}</code></td><td class=r>{n(b['core'])}</td><td class=r>{n(b.get('clr'))}</td><td class=r>{n(p.get('one_pass_min'),'{:.0f}')}</td><td class=r>{n(p.get('episode_h'),'{:.1f}')}</td><td class=r>{p.get('shards','—')}</td><td class=r>{n(p.get('machine_h'),'{:.0f}')}</td><td>{', '.join(p['packs_missing']) or '—'}</td><td>{html.escape(p.get('note',''))}{' <b>已录</b>' if p['recorded'] else ''}</td></tr>")
h.append("</table>")
for c in ORDER[1:]:
    rows = [p for p in P if p['cls'] == c]
    if not rows: continue
    h.append(f"<h2><span class='tag {c[0]}'>{c}</span> {len(rows)}</h2><p>{EXPL[c]}</p><table><tr><th>项目</th><th>代表关卡</th><th>最近状态</th><th>核心 m²</th><th>需同步的包</th><th>备注</th></tr>")
    for p in rows:
        b = p['best']
        h.append(f"<tr><td><b>{html.escape(p['title'])}</b><div class=mut>{p['fid']}</div></td><td><code>{html.escape(b['level'])}</code></td><td>{b['state'] or '未跑'}</td><td class=r>{n(b['core'])}</td><td>{', '.join(p['packs_missing']) or '—'}</td><td>{html.escape(p.get('note',''))}</td></tr>")
    h.append("</table>")
h.append(f"<h2>两种统计口径（录制口径 = 按关卡）</h2><table><tr><th>口径</th><th>可直接录</th><th>成片 h</th><th>集</th><th>机时</th><th>10 台天数</th><th>存储 GB</th></tr>"
         f"<tr><td>按资源包（每包一张代表关卡）</td><td class=r>{len(A)}</td><td class=r>{tot['ep']:.0f}</td><td class=r>{tot['sh']}</td><td class=r>{tot['mh']:.0f}</td><td class=r>{tot['mh']/240:.1f}</td><td class=r>{tot['gb']:,}</td></tr>"
         f"<tr><td>按关卡（每个通过的场景都录）</td><td class=r>{len(LA)}</td><td class=r>{ltot['ep']:.0f}</td><td class=r>{ltot['sh']}</td><td class=r>{ltot['mh']:.0f}</td><td class=r>{ltot['mh']/240:.1f}</td><td class=r>{ltot['gb']:,}</td></tr></table>"
         f"<p>按关卡的 {len(LA)} 张分布在 {len({x['fid'] for x in LA})} 个资源包里；多出来的 {len(LA)-len(A)} 张是同一个包里的第二、第三个场景。</p>")
h.append("<h3>对照：87 个资源包的去向</h3><table><tr><th>分类</th><th>个数</th><th>说明</th></tr>")
for c in ORDER: h.append(f"<tr><td><span class='tag {c[0]}'>{c}</span></td><td class=r>{sum(1 for p in P if p['cls']==c)}</td><td>{EXPL[c]}</td></tr>")
h.append(f"</table><h3>按关卡：可直接录的 {len(LA)} 张（按成片时长降序）</h3><table><tr><th>#</th><th>资源包</th><th>关卡</th><th>核心 m²</th><th>一遍 min</th><th>成片 h</th><th>集</th><th>机时</th><th>备注</th></tr>")
for i, x in enumerate(LA, 1):
    h.append(f"<tr class='{'big' if x['tier']=='大' else 'low' if (x['core'] or 0) < 50 else ''}'><td class=r>{i}</td><td>{html.escape(x['title'])}<div class=mut>{x['fid']}</div></td><td><code>{html.escape(x['level'])}</code></td><td class=r>{n(x['core'])}</td><td class=r>{n(x.get('one_pass_min'),'{:.0f}')}</td><td class=r>{x['episode_h']:.1f}</td><td class=r>{x['shards']}</td><td class=r>{x['machine_h']:.0f}</td><td>{'<b>已录</b>' if x['recorded'] else ''}</td></tr>")
h.append("</table>")
h.append("<h2>单独立项</h2><ul><li><b>Dubai Downtown</b>：64 GB，需 Cesium；悬浮模式录过 30 s 演示，地面无碰撞，要录先解决碰撞。</li><li><b>Lyra</b>：射击示例，非环境。</li><li><b>AdditionalSamples</b>：CitySample 82 GB / 439 张关卡是完整城市，值得单独评估；另两个不是环境。</li></ul>")
h.append("<p class=mut>机器可读版：<code>revisit_pipeline/results/map_plan_2026-09-17/projects.json</code>；文档：<code>revisit_pipeline/MAP_PLAN_2026-09-17.md</code>。一遍耗时取自路线验证时的完整覆盖走法帧数（旧速度档），1 m/s 下会略短；存储按 1.5 GB/成片小时（166 GB / 108 h 实测）。</p>")
h = _reorder(h, "对照：按资源包", "两种统计口径", "<h2>单独立项</h2>")
h[1] = h[1].replace("全部交付地图的录制规划（按项目）", "全部交付地图的录制规划（按关卡）").replace("87 个资源包，每包一张代表关卡", "口径：一张关卡 = 一个地图 = 一集；442 张关卡逐张归类，资源包视图在后")
(SITE / 'index.html').write_text('\n'.join(h))
print('written', SITE / 'index.html', REPO / 'MAP_PLAN_2026-09-17.md', RES / 'projects.json')
