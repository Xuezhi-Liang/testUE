#!/usr/bin/env python3
"""Publish the recording configuration snapshot without rebuilding historical results."""
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import hashlib
import json
import os

from markdown_it import MarkdownIt

HERE = Path(__file__).resolve().parent
SITE = HERE / "site/longvideo/flicker-fix"
TEMPLATE = Path("/home/ubuntu/UE5-Agent-Data/revisit_pipeline/tasks/stability_detail_candidate.json")
CARD = '''<section id="recording-config" class="card" style="padding:18px 20px;margin:20px 0">
<h2 style="margin:0 0 8px">当前录制配置 · 2026-09-16</h2>
<p style="margin:0 0 10px">TAA · 2× 超采样 · 720p / 24fps · 按地图补光。包含镜头、光照、曝光、抗锯齿、深度与编码参数。</p>
<p style="margin:0 0 10px"><a href="recording-config.html">在线阅读配置</a> · <a href="recording-config.md" download="UE-recording-config-2026-09-16.md">下载 Markdown</a> · <a href="stability_detail_candidate.json" download>下载模板 JSON</a></p>
<p class="note" style="margin:0">当前推荐模板与下方历史对比视频分别记录。文档注明曝光开关冲突与景深实现待核验；发布文档未修改录制任务。</p>
</section>'''
ANCHOR = '<h1>摄像头移动时，建筑还会不会闪？</h1>'


def atomic_write(path, content):
    tmp = path.with_name(path.name + ".config-tmp")
    tmp.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    os.replace(tmp, path)


def main():
    md = (HERE / "recording-config.md").read_text()
    template = TEMPLATE.read_bytes()
    digest = hashlib.sha256(template).hexdigest()
    md += f"\n推荐模板快照 SHA-256：`{digest}`。\n"
    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(md)
    title = "UE 地图录制配置 · 2026-09-16"
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>''' + escape(title) + '''</title>
<style>*{box-sizing:border-box}body{margin:0;background:#0d141c;color:#e6edf3;font:16px/1.8 system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:28px 24px 64px}a{color:#8cd5ce}h1{font-size:30px}h2{font-size:23px;margin-top:36px}code{overflow-wrap:anywhere;font-size:.9em}pre{padding:18px;background:#17212c;border:1px solid #334352;border-radius:8px;overflow:auto;line-height:1.6}pre code{overflow-wrap:normal}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #334352;min-width:140px}nav{display:flex;flex-wrap:wrap;gap:12px}.meta{color:#9eacbb}li{margin:8px 0}@media(max-width:600px){main{padding:20px 14px}h1{font-size:25px}h2{font-size:21px}}</style></head><body><main>
<nav><a href="./">← 返回闪烁修复页面</a><a href="recording-config.md" download="UE-recording-config-2026-09-16.md">下载 Markdown</a><a href="stability_detail_candidate.json" download>下载模板 JSON</a></nav>
''' + body.replace("<table>", '<div class="table-scroll"><table>').replace("</table>", "</table></div>") + "</main></body></html>"
    targets = [SITE / "index.html", HERE / "pipeline/build_temporal_page.py"]
    # Validate both insertion points before publishing; never invoke the old video generator.
    updated = []
    for path in targets:
        old = path.read_text()
        if 'id="recording-config"' not in old:
            if old.count(ANCHOR) != 1:
                raise RuntimeError(f"Expected exactly one insertion point in {path}")
            updated.append((path, old, old.replace(ANCHOR, ANCHOR + CARD)))
    backup = HERE / "config_publish_backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True)
    for path, old, _ in updated:
        (backup / path.name).write_text(old)
    atomic_write(SITE / "recording-config.md", md)
    atomic_write(SITE / "recording-config.html", page)
    atomic_write(SITE / "stability_detail_candidate.json", template)
    for path, _, new in updated:
        atomic_write(path, new)
    print(json.dumps({"published": str(SITE), "template_sha256": digest,
                      "updated_pages": [str(p) for p, _, _ in updated]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
