#!/usr/bin/env python3
"""Render the review page for pipeline episodes at /new_request/.

Everything the package records is shown, because the point of this page is to make a bad episode
obvious without opening a JSON file: the acceptance gates first (a skipped gate is drawn
differently from a passing one, so a missing modality can never read as a success), then the
per-frame panel driven by the video's own clock, then every field of every sidecar.

Both videos are offered. `preview.mp4` is the QA dashboard - route map, commanded versus measured,
clearance, collision state. `rgb.mp4` is the clean delivered frames, which is what a model would
actually see.
"""
import csv
import html
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EPS = HERE / "episodes"
# Taken from the gallery module rather than hardcoded: serve.py serves `build_gallery.GALLERY`,
# and writing to a guessed path produces a page that is built successfully and never served.
sys.path.insert(0, str(HERE.parent))
import build_gallery as _bg  # noqa: E402
OUT = _bg.GALLERY / "new_request"

# columns handed to the browser for the live panel; kept small so the page stays light
COLS = ["episode_time_s", "desired_x_cm", "desired_y_cm", "desired_z_cm", "desired_yaw_deg",
        "actual_x_cm", "actual_y_cm", "actual_z_cm", "actual_yaw_deg", "actual_pitch_deg",
        "actual_roll_deg", "pos_error_cm", "yaw_error_deg", "step_cm", "speed_m_s",
        "yaw_rate_deg_s", "quat_x", "quat_y", "quat_z", "quat_w"]


def esc(v):
    return html.escape(str(v))


def load(ep):
    d = {"id": ep.name, "dir": ep}
    for name in ("capture_summary", "camera", "trajectory", "revisits", "acceptance",
                 "sequence"):
        f = ep / f"{name}.json"
        d[name] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    rows, phases = [], []
    f = ep / "frames.csv"
    if f.exists():
        with open(f) as fh:
            for r in csv.DictReader(fh):
                rows.append([float(r[c]) if r.get(c) not in (None, "") else None
                             for c in COLS])
                phases.append(r.get("phase", ""))
    d["frames"] = rows
    d["phases"] = phases
    return d


def kv(label, value, cls=""):
    if value is None or value == "":
        value = "—"
    if isinstance(value, float):
        value = f"{value:.6g}"
    return (f'<div class="kv {cls}"><span>{esc(label)}</span>'
            f'<b>{esc(value)}</b></div>')


def section(title, body, note=None):
    n = f'<p class="note">{esc(note)}</p>' if note else ""
    return f'<section><h4>{esc(title)}</h4>{n}<div class="grid">{body}</div></section>'


def flat(prefix, obj, out, depth=0):
    """Render nested dicts as label/value rows so no recorded field is silently dropped."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat(f"{prefix}{k}." if depth else f"{k}.", v, out, depth + 1) \
                if isinstance(v, (dict, list)) else out.append(
                    kv(f"{prefix}{k}" if depth else k, v))
    elif isinstance(obj, list):
        if obj and all(not isinstance(x, (dict, list)) for x in obj):
            out.append(kv(prefix.rstrip("."), ", ".join(str(x) for x in obj[:24])
                          + (" …" if len(obj) > 24 else "")))
        else:
            for i, v in enumerate(obj):
                flat(f"{prefix}{i}.", v, out, depth + 1)
    else:
        out.append(kv(prefix.rstrip("."), obj))


def gates_html(acc):
    if not acc:
        return "<p class='note'>没有 acceptance.json</p>"
    rows = []
    for g in acc.get("gates", []):
        r = g["result"]
        cls = {"pass": "ok", "FAIL": "bad", "skip": "skip"}.get(r, "")
        rows.append(f'<div class="gate {cls}"><span class="tag">{esc(r)}</span>'
                    f'<b>{esc(g["gate"])}</b><i>{esc(g["detail"])}</i></div>')
    acc_cls = "ok" if acc.get("accepted") else "bad"
    p0 = "完整" if acc.get("p0_complete") else \
        "不完整 · 缺 " + ", ".join(acc.get("p0_missing", []))
    return (f'<div class="verdict {acc_cls}">'
            f'accepted={esc(acc.get("accepted"))} &nbsp;·&nbsp; '
            f'通过 {acc.get("gates_passed")} / 失败 {acc.get("gates_failed")} / '
            f'跳过 {acc.get("gates_skipped")} &nbsp;·&nbsp; '
            f'accepted_seconds {acc.get("accepted_seconds")} &nbsp;·&nbsp; '
            f'P0 {esc(p0)}</div>'
            f'<p class="note">{esc(acc.get("accepted_note", ""))}</p>'
            + "".join(rows))


def plot_svg(e, idx, S=340):
    rows = e["frames"]
    if not rows:
        return ""
    ix, iy = COLS.index("desired_x_cm"), COLS.index("desired_y_cm")
    ax, ay = COLS.index("actual_x_cm"), COLS.index("actual_y_cm")
    xs = [r[ix] for r in rows] + [r[ax] for r in rows]
    ys = [r[iy] for r in rows] + [r[ay] for r in rows]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    span = max(max(xs) - min(xs), max(ys) - min(ys), 200.0) * 1.3

    def px(x, y):
        return (S / 2 + (y - cy) / span * S, S / 2 - (x - cx) / span * S)

    dpath = " ".join(f"{u:.1f},{v:.1f}" for u, v in (px(r[ix], r[iy]) for r in rows))
    tr = e["trajectory"]
    aw = tr.get("anchor_window") or [0, 0]
    rw = tr.get("revisit_window") or [0, 0]
    au, av = px(rows[aw[1]][ax], rows[aw[1]][ay])
    ru, rv = px(rows[min(rw[1], len(rows) - 1)][ax], rows[min(rw[1], len(rows) - 1)][ay])
    m = span / 100.0
    return f"""
<div class="plot">
  <svg viewBox="0 0 {S} {S}" width="100%" preserveAspectRatio="xMidYMid meet">
    <rect width="{S}" height="{S}" fill="#0d1014"/>
    <line x1="{S/2}" y1="0" x2="{S/2}" y2="{S}" stroke="#1e2530"/>
    <line x1="0" y1="{S/2}" x2="{S}" y2="{S/2}" stroke="#1e2530"/>
    <polyline points="{dpath}" fill="none" stroke="#6ea8fe" stroke-width="2"/>
    <circle cx="{au:.1f}" cy="{av:.1f}" r="6" fill="none" stroke="#4ad6a0" stroke-width="2"/>
    <circle cx="{ru:.1f}" cy="{rv:.1f}" r="6" fill="none" stroke="#e0b060" stroke-width="2"
            stroke-dasharray="3 2"/>
    <line id="hd{idx}" x1="{S/2}" y1="{S/2}" x2="{S/2}" y2="{S/2-12}"
          stroke="#ff6b6b" stroke-width="2"/>
    <circle id="dot{idx}" cx="{S/2}" cy="{S/2}" r="3.5" fill="#ff6b6b"/>
    <text x="6" y="{S-6}" fill="#93a0b0" font-size="9">俯视 · 视野约 {m:.1f} m ·
      上=UE +X · 右=UE +Y</text>
  </svg>
  <div class="legend"><i style="background:#6ea8fe"></i>冻结轨迹
    <i style="background:#4ad6a0"></i>anchor
    <i style="background:#e0b060"></i>revisit
    <i style="background:#ff6b6b"></i>当前帧(实测位姿)</div>
</div>"""


def card(e, idx):
    su, cam, tr = e["capture_summary"], e["camera"], e["trajectory"]
    rv, acc, seq = e["revisits"], e["acceptance"], e["sequence"]
    base = f"../pipeline_episodes/{e['id']}"
    has_prev = (e["dir"] / "preview.mp4").exists()
    has_contact = (e["dir"] / "contact.png").exists()

    src_prev = f"{base}/preview.mp4"
    src_rgb = f"{base}/rgb.mp4"
    first = src_prev if has_prev else src_rgb
    btns = (f'<button class="on" data-src="{src_prev}" onclick="pick({idx},this)">'
            f'QA 仪表盘 (preview.mp4)</button>' if has_prev else "") + \
           f'<button {"" if has_prev else "class=on"} data-src="{src_rgb}" ' \
           f'onclick="pick({idx},this)">交付画面 (rgb.mp4)</button>'

    def sec(title, obj, note=None):
        out = []
        flat("", obj, out)
        return section(title, "".join(out), note)

    tr_nopose = {k: v for k, v in tr.items()
                 if k not in ("poses", "task", "reach_by_bearing_cm",
                              "collision")}
    coll = tr.get("collision", {})
    coll_show = {k: v for k, v in coll.items() if k != "clearance_per_frame_cm"}
    reach = tr.get("reach_by_bearing_cm", {})

    contact = (f'<a href="{base}/contact.png" target="_blank">'
               f'<img class="contact" src="{base}/contact.png" loading="lazy"></a>'
               if has_contact else "")

    return f"""
<article class="ep">
  <div class="left">
    <h3>{esc(e['id'])}</h3>
    <div class="vsrc">画面：{btns}</div>
    <video id="v{idx}" controls preload="metadata" src="{first}"></video>
    <div class="live" id="live{idx}">
      <div class="row"><span>frame_id</span><b data-f="frame">—</b>
        <span>episode_time_s</span><b data-f="t">—</b>
        <span>phase</span><b data-f="ph">—</b>
        <span>窗口</span><b data-f="win">—</b></div>
      <div class="row"><span>指令 xyz (cm)</span><b data-f="dxyz">—</b>
        <span>指令 yaw</span><b data-f="dyaw">—</b></div>
      <div class="row"><span>实测 xyz (cm)</span><b data-f="axyz">—</b>
        <span>实测 pitch/yaw/roll</span><b data-f="apyr">—</b></div>
      <div class="row"><span>位姿误差</span><b data-f="perr">—</b>
        <span>yaw 误差</span><b data-f="yerr">—</b></div>
      <div class="row"><span>平移速度</span><b data-f="spd">—</b>
        <span>yaw 速率</span><b data-f="yr">—</b>
        <span>帧间位移</span><b data-f="step">—</b></div>
      <div class="row"><span>quaternion xyzw</span><b data-f="quat">—</b></div>
      <div class="row"><span>前向净空 (cm)</span><b data-f="clr">—</b></div>
    </div>
    <p class="note">以上随播放逐帧更新，数据来自 frames.csv 的实际记录，不是重算的。</p>
    {plot_svg(e, idx)}
    {contact}
  </div>
  <div class="right">
    <section class="gates"><h4>验收门 (规范 §14)</h4>{gates_html(acc)}</section>
    {sec("采集摘要 capture_summary.json", su)}
    {sec("相机与坐标契约 camera.json (§7 §8)", cam)}
    {sec("轨迹 trajectory.json (§3 §5 §6)", tr_nopose)}
    {sec("碰撞检查 (§11)", coll_show)}
    {sec("回访 revisits.json (§12)", rv)}
    {sec("episode 元数据 sequence.json (§10)", seq)}
    {sec("各方向可通行距离 (capsule sweep, cm)", reach,
         "冻结轨迹就是从这张扫描里选方向的：只有容得下身体胶囊加余量的方向才会被采用。")}
  </div>
  <script>window.EPD = window.EPD || {{}}; window.EPD[{idx}] = {{
    fps: {su.get('fps', 24)},
    aw: {json.dumps(tr.get('anchor_window'))},
    rw: {json.dumps(tr.get('revisit_window'))},
    plot: {json.dumps({'S': 340, 'span': None})},
    phases: {json.dumps(e['phases'])},
    clr: {json.dumps(coll.get('clearance_per_frame_cm') or [])},
    rows: {json.dumps([[None if v is None else round(v, 4) for v in r]
                       for r in e['frames']])} }};</script>
</article>"""


def main():
    eps = sorted([d for d in EPS.iterdir()
                  if d.is_dir() and (d / "frames.csv").exists()]) if EPS.exists() else []
    if not eps:
        print("no pipeline episodes yet")
        return 1
    data = [load(e) for e in eps]
    cards = "".join(card(e, i) for i, e in enumerate(data))
    total = sum(e["capture_summary"].get("duration_s", 0) for e in data)
    accepted = sum(1 for e in data if e["acceptance"].get("accepted"))
    p0 = sum(1 for e in data if e["acceptance"].get("p0_complete"))

    html_doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UE5 Revisit 空间记忆数据集 · 采集流程</title>
<style>
:root{{--bg:#0f1216;--fg:#e7ebf0;--mut:#93a0b0;--card:#171c23;--line:#252d38;
  --ok:#4ad6a0;--bad:#ff6b6b;--skip:#e0b060;--acc:#6ea8fe}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 ui-sans-serif,system-ui,"Noto Sans CJK SC",sans-serif}}
header{{padding:20px 24px;border-bottom:1px solid var(--line)}}
h1{{margin:0 0 6px;font-size:20px}}
h3{{margin:0 0 10px;font-size:14px;color:var(--acc);word-break:break-all;font-weight:600}}
h4{{margin:16px 0 8px;font-size:12px;color:var(--mut);text-transform:uppercase;
  letter-spacing:.08em}}
.sum{{color:var(--mut);font-size:13px}}
.wrap{{padding:18px 24px;display:flex;flex-direction:column;gap:22px}}
.ep{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;
  background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}}
@media(max-width:1100px){{.ep{{grid-template-columns:1fr}}}}
video{{width:100%;aspect-ratio:1920/1088;background:#000;border-radius:8px;display:block}}
.vsrc{{margin:0 0 8px;font-size:12px;color:var(--mut)}}
.vsrc button{{background:#11161c;color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:4px 10px;margin-left:6px;font-size:12px;cursor:pointer}}
.vsrc button.on{{border-color:var(--acc);color:var(--acc)}}
.live{{margin-top:10px;font-size:12px;background:#11161c;border:1px solid var(--line);
  border-radius:8px;padding:8px 10px}}
.live .row{{display:flex;flex-wrap:wrap;gap:6px 14px;padding:3px 0}}
.live span{{color:var(--mut)}}
.live b{{font-variant-numeric:tabular-nums;font-weight:600}}
.note{{margin:6px 0 8px;font-size:11.5px;color:var(--mut)}}
.right{{max-height:920px;overflow:auto;padding-right:6px}}
.grid{{display:grid;grid-template-columns:1fr;gap:2px}}
.kv{{display:flex;justify-content:space-between;gap:12px;padding:3px 8px;border-radius:4px;
  background:#11161c;font-size:12px}}
.kv span{{color:var(--mut);flex:0 0 auto;max-width:58%;word-break:break-word}}
.kv b{{text-align:right;word-break:break-word;font-weight:600;
  font-variant-numeric:tabular-nums}}
.gate{{display:grid;grid-template-columns:52px 1fr;gap:4px 10px;padding:5px 8px;
  border-radius:4px;background:#11161c;margin-bottom:3px;font-size:12px}}
.gate i{{grid-column:2;color:var(--mut);font-style:normal;font-size:11px}}
.gate .tag{{font-size:10px;text-align:center;border-radius:3px;padding:1px 0;
  align-self:start}}
.gate.ok .tag{{background:rgba(74,214,160,.16);color:var(--ok)}}
.gate.bad .tag{{background:rgba(255,107,107,.16);color:var(--bad)}}
.gate.skip .tag{{background:rgba(224,176,96,.16);color:var(--skip)}}
.verdict{{padding:8px 10px;border-radius:6px;font-size:12.5px;margin-bottom:8px;
  font-weight:600}}
.verdict.ok{{background:rgba(74,214,160,.12);color:var(--ok)}}
.verdict.bad{{background:rgba(255,107,107,.12);color:var(--bad)}}
.plot{{margin-top:12px;max-width:340px}}
.legend{{font-size:11px;color:var(--mut);display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}}
.legend i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:3px}}
.contact{{width:100%;margin-top:12px;border-radius:8px;border:1px solid var(--line)}}
</style></head><body>
<header>
  <h1>UE5 Revisit 空间记忆数据集 · 采集流程</h1>
  <div class="sum">{len(data)} 个 episode · 合计 {total:.1f} s ·
    验收通过 {accepted}/{len(data)} · P0 完整 {p0}/{len(data)}
    （深度在本 build 无法产出，见每个 episode 的验收门）</div>
  <div class="sum">流程：任务 JSON → 冻结轨迹（先做碰撞验证）→ UE 只渲染并精确记录 →
    数据包 + QA 视频</div>
</header>
<div class="wrap">{cards}</div>
<script>
function pick(i, btn) {{
  const v = document.getElementById('v' + i), src = btn.dataset.src;
  if (!src || v.getAttribute('src') === src) return;
  const t = v.currentTime, playing = !v.paused;
  v.setAttribute('src', src);
  v.addEventListener('loadedmetadata', function once() {{
    v.removeEventListener('loadedmetadata', once);
    v.currentTime = t; if (playing) v.play();
  }});
  btn.parentNode.querySelectorAll('button').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
}}

const C = {json.dumps(COLS)};
const I = {{}};
C.forEach((n, k) => I[n] = k);

Object.keys(window.EPD).forEach(function (k) {{
  const d = window.EPD[k], v = document.getElementById('v' + k),
        box = document.getElementById('live' + k);
  if (!v || !box || !d.rows.length) return;
  const set = (f, s) => {{ const el = box.querySelector('[data-f="' + f + '"]');
                          if (el) el.textContent = s; }};
  // a column the recorder did not produce is null, not zero - show it as unknown
  const f = (x, n, u) => (x === null || x === undefined) ? '—' : x.toFixed(n) + (u || '');
  const dot = document.getElementById('dot' + k), hd = document.getElementById('hd' + k);
  function tick() {{
    let i = Math.round(v.currentTime * d.fps);
    if (i < 0) i = 0;
    if (i > d.rows.length - 1) i = d.rows.length - 1;
    const r = d.rows[i];
    let win = '—';
    if (d.aw && i >= d.aw[0] && i <= d.aw[1]) win = 'anchor';
    else if (d.rw && i >= d.rw[0] && i <= d.rw[1]) win = 'revisit';
    set('frame', i);
    set('t', f(r[I.episode_time_s], 6, ' s'));
    set('ph', d.phases[i] || '—');
    set('win', win);
    set('dxyz', f(r[I.desired_x_cm], 1) + ', ' + f(r[I.desired_y_cm], 1) + ', '
                + f(r[I.desired_z_cm], 1));
    set('dyaw', f(r[I.desired_yaw_deg], 3, '°'));
    set('axyz', f(r[I.actual_x_cm], 1) + ', ' + f(r[I.actual_y_cm], 1) + ', '
                + f(r[I.actual_z_cm], 1));
    set('apyr', f(r[I.actual_pitch_deg], 2) + ' / ' + f(r[I.actual_yaw_deg], 2) + ' / '
                + f(r[I.actual_roll_deg], 2));
    set('perr', f(r[I.pos_error_cm] === null ? null : r[I.pos_error_cm] * 10, 3, ' mm'));
    set('yerr', f(r[I.yaw_error_deg], 4, '°'));
    set('spd', f(r[I.speed_m_s], 3, ' m/s'));
    set('yr', f(r[I.yaw_rate_deg_s], 2, ' °/s'));
    set('step', f(r[I.step_cm], 2, ' cm'));
    set('quat', f(r[I.quat_x], 4) + ', ' + f(r[I.quat_y], 4) + ', ' + f(r[I.quat_z], 4)
                + ', ' + f(r[I.quat_w], 4));
    const c = d.clr && i < d.clr.length ? d.clr[i] : null;
    set('clr', c === null || c === undefined ? '1500+ (无命中)' : c.toFixed(1));
    if (dot && d.plot) {{
      // recompute the same mapping the SVG used, from the rows themselves
      if (!d._m) {{
        let xs = [], ys = [];
        d.rows.forEach(q => {{ xs.push(q[I.desired_x_cm], q[I.actual_x_cm]);
                               ys.push(q[I.desired_y_cm], q[I.actual_y_cm]); }});
        const cx = (Math.min(...xs) + Math.max(...xs)) / 2,
              cy = (Math.min(...ys) + Math.max(...ys)) / 2,
              sp = Math.max(Math.max(...xs) - Math.min(...xs),
                            Math.max(...ys) - Math.min(...ys), 200) * 1.3;
        d._m = {{cx: cx, cy: cy, sp: sp, S: 340}};
      }}
      const m = d._m;
      const u = m.S / 2 + (r[I.actual_y_cm] - m.cy) / m.sp * m.S;
      const w = m.S / 2 - (r[I.actual_x_cm] - m.cx) / m.sp * m.S;
      dot.setAttribute('cx', u.toFixed(1)); dot.setAttribute('cy', w.toFixed(1));
      if (hd) {{
        const a = (r[I.actual_yaw_deg] - 90) * Math.PI / 180;
        hd.setAttribute('x1', u.toFixed(1)); hd.setAttribute('y1', w.toFixed(1));
        hd.setAttribute('x2', (u + 13 * Math.cos(a)).toFixed(1));
        hd.setAttribute('y2', (w + 13 * Math.sin(a)).toFixed(1));
      }}
    }}
  }}
  v.addEventListener('timeupdate', tick);
  v.addEventListener('seeked', tick);
  v.addEventListener('loadedmetadata', tick);
  tick();
}});
</script>
</body></html>"""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(html_doc, encoding="utf-8")
    print(f"[page] {len(data)} episode(s) -> {OUT/'index.html'}  "
          f"({len(html_doc)/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
