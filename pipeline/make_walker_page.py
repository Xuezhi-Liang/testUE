#!/usr/bin/env python3
"""Build a standalone page for the third-person walker footage.

Written into `episodes/` on purpose. That directory is already mounted read-only by serve.py as
`/pipeline_episodes`, so the page and its videos are served with no change to the server and no
restart - the existing gallery keeps running untouched. It also means nothing new is exposed: the
page sits inside a tree that was already public.

Regenerate after each recording; it reads whatever `*__walker` episodes exist.
"""
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EPISODES = HERE / "episodes"
OUT = EPISODES / "walker.html"
BASE = "/pipeline_episodes"

CSS = """
:root { color-scheme: light dark;
  --bg:#faf9f7; --fg:#1a1a1a; --muted:#5f5f5f; --card:#fff; --line:#e4e1dc; --accent:#8a5a2b; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#16181c; --fg:#e9e7e3; --muted:#9a9791; --card:#1e2126; --line:#2c3037; --accent:#d7a76a; } }
:root[data-theme="dark"] {
  --bg:#16181c; --fg:#e9e7e3; --muted:#9a9791; --card:#1e2126; --line:#2c3037; --accent:#d7a76a; }
:root[data-theme="light"] {
  --bg:#faf9f7; --fg:#1a1a1a; --muted:#5f5f5f; --card:#fff; --line:#e4e1dc; --accent:#8a5a2b; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 -apple-system,
  BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",
  sans-serif; }
.wrap { max-width:1000px; margin:0 auto; padding:32px 20px 64px; }
h1 { font-size:1.6rem; margin:0 0 4px; letter-spacing:-.01em; }
.sub { color:var(--muted); margin:0 0 28px; font-size:.95rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px;
  margin:0 0 26px; }
.card h2 { font-size:1.05rem; margin:0 0 2px; font-weight:600; }
.card .map { color:var(--accent); font-size:.86rem; margin:0 0 14px; word-break:break-all; }
video { width:100%; height:auto; max-width:100%; border-radius:8px; background:#000;
  display:block; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 18px; margin:16px 0 0;
  font-size:.88rem; }
dt { color:var(--muted); }
dd { margin:0; }
.note { color:var(--muted); font-size:.85rem; margin:14px 0 0; padding-top:12px;
  border-top:1px solid var(--line); }
.warn { color:var(--accent); }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em;
  word-break:break-all; }
.empty { color:var(--muted); }
"""


def card(ep: Path) -> str:
    meta = {}
    mp = ep / "WALKER.json"
    if mp.exists():
        meta = json.loads(mp.read_text())
    # Prefer the web copy. The archival rgb.mp4 is crf 18 - 207 MiB for two minutes, which is
    # right for data and wrong for a page; rgb_web.mp4 is crf 26 at the same resolution, 44 MiB,
    # and indistinguishable at the size a browser shows it.
    web = ep / "rgb_web.mp4"
    vid = f"{BASE}/{ep.name}/{'rgb_web.mp4' if web.exists() else 'rgb.mp4'}"
    size_mib = (web if web.exists() else ep / "rgb.mp4").stat().st_size / 2 ** 20
    frames = len(list((ep / "rgb").glob("*.jpg"))) if (ep / "rgb").is_dir() else 0
    anim = meta.get("walk_animation_measured") or {}
    cam = meta.get("camera") or {}
    speed = anim.get("walk_anim_speed_cm_s") or anim.get("speed_cm_s")
    path = meta.get("path") or {}
    rows = [
        ("地图", html.escape(str(meta.get("map_id", "?")))),
        ("帧数", f"{frames} 帧 · {frames / 24:.1f} s @ 24 fps" if frames else "?"),
        ("路线来源", f"<code>{html.escape(str(meta.get('source_episode_id', '?')))}</code>"
                      + "（重新计时为步行路径：恒速、圆角、折返走弧线；episode 本身的冻结轨迹未改动）"),
        ("人物", (f"<code>{html.escape(str(meta.get('walker_blueprint','')).rsplit('/',1)[-1])}"
                   f"</code> 自己装配的市民" if meta.get("walker_blueprint") else
                   "<br>".join(f"<code>{html.escape(m.rsplit('/', 1)[-1])}</code>"
                               for m in (meta.get("meshes") or [])))),
        ("走路动画", f"<code>{html.escape(str(meta.get('walk_animation', '?')).rsplit('/', 1)[-1])}"
                     f"</code>"),
        # The play rate is read from the path stats, not recomputed against a hardcoded cruise
        # speed: the two clips run at 140 and 243.5 cm/s and a fixed divisor silently mislabels one.
        ("路径速度", (f"巡航 {path.get('cruise_cm_s', '?')} cm/s，过弯降至 "
                      f"{path.get('speed_min_cm_s', '?')} cm/s（按曲率限速，"
                      f"侧向加速度上限 2.5 m/s²）" if path else "?")),
        ("动画自身速度",
         (f"{speed:.1f} cm/s，按行进距离定位 —— 脚不打滑。"
          + (f"播放速率 {path['play_rate_min']:.2f}–{path['play_rate_max']:.2f}×"
             + ("（直道与动画原速一致；弯道放慢，因为单条动画覆盖不了速度范围，"
                "真正的解法是速度混合空间）" if path.get("play_rate_max", 0) >= 0.95
                else "")
             if path else "")
          if speed and speed >= 1 else
          f'<span class="warn">{speed if speed is not None else "?"} cm/s —— '
          f'无根位移，动画按时钟播放，脚会打滑</span>')),
        ("转弯", (f"圆角 {path.get('corner_radius_cm','?')} cm；折返走 "
                  f"{path.get('u_turn_radius_cm','?')} cm 半径的真实弧线"
                  f"（{'反向' if path.get('u_turn_flipped') else '默认侧'}，"
                  f"扫掠命中 {path.get('sweep_hits','?')} 帧）" if path else "?")),
        ("视频", f"{size_mib:.0f} MiB（网页压缩版；原始 crf 18 版在同目录 rgb.mp4）"),
        ("跟拍相机", (f"身后 {cam.get('behind_cm','?')} cm，侧移 {cam.get('side_cm','?')} cm，"
                       f"眼线上方 {cam.get('above_eye_cm','?')} cm，"
                       + (f"朝向跟随行人视线，下俯 {cam.get('pitch_deg','?')}°"
                          if cam.get("follows_walker_view")
                          else f"看向 {cam.get('look_at_height_cm','?')} cm 高"))),
    ]
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    note = html.escape(str(meta.get("note", "")))
    title = (f"{path['label']} · {path['anim']}" if path.get("label")
             else ep.name.replace("__walker", ""))
    return (f'<section class="card"><h2>{html.escape(title)}</h2>'
            f'<p class="map">{html.escape(str(meta.get("map_id", "")))}</p>'
            f'<video controls preload="metadata" playsinline src="{vid}"></video>'
            f'<dl>{dl}</dl>'
            + (f'<p class="note">{note}</p>' if note else "")
            + '</section>')


def build() -> Path:
    eps = sorted(p for p in EPISODES.glob("*__walker") if p.is_dir())
    body = "".join(card(p) for p in eps) or (
        '<p class="empty">还没有 walker 素材。跑 <code>capture_walker.py</code> 之后重新生成本页。</p>')
    OUT.write_text(
        f'<title>SimWorld · 第三人称跟拍</title>\n'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<style>{CSS}</style>\n"
        f'<div class="wrap"><h1>第三人称跟拍</h1>'
        f'<p class="sub">一个 <strong>CitySampleCrowd 市民</strong>走第一人称 episode 的同一条'
        f'路线，相机在他肩后、朝向跟着他自己的视线。'
        f'<br>路线是<strong>重新计时</strong>过的，不是 episode 的冻结轨迹：那条轨迹 40% 的帧'
        f'站着不动、累计原地转身 708.7°、移动时均速只有 72.7 cm/s —— 对空间记忆数据集全都正确'
        f'（站着观察就是内容、原地转身保证 revisit 几何闭合），但没有一条像人走路，相机怎么调都救不了。'
        f'所以跟拍用单独生成的步行路径：圆角过弯、折返走真实弧线、按曲率限速（进弯前减速而不是'
        f'在弯里甩）、朝向取路径切线，每条路径都重新跑胶囊体扫掠确认零碰撞后才录。'
        f'<strong>五个 episode 的冻结轨迹一个字节未改。</strong>'
        f'<br>人物由 <code>BP_CrowdCharacter</code> 自己装配（6 个骨骼网格，身份每次随机）：'
        f'crowd 市民是三套骨架上的五层资产，手工拼装拼不出来 —— 三次尝试分别得到空领口空袖口、'
        f'肩上一根拉伸的黑刺、胯部一颗过大的头。'
        f'<br>下面两段是同一条路线、同一个市民、同一套相机，只换动画和速度。'
        f'<strong>crowd 骨架上没有跑步动画</strong>（City Sample 的市民只走不跑），'
        f'所以「快」的那段是 <code>MTN_N_WalkQuickly_F</code> 快走 2.44 m/s —— '
        f'已在慢跑起步的速度区间，但仍是走路循环（始终一只脚着地），不是跑。</p>'
        f'{body}</div>\n', encoding="utf-8")
    return OUT


if __name__ == "__main__":
    p = build()
    n = len(sorted(EPISODES.glob("*__walker")))
    print(f"wrote {p} with {n} clip(s) -> http://13.60.235.97:8500{BASE}/walker.html")
    sys.exit(0)
