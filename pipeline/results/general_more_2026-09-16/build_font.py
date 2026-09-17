from pathlib import Path
import urllib.request,urllib.parse,re,json
r=Path(__file__).resolve().parent;s=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/general-stability-more')
text=''.join(p.read_text() for p in [r/'publish.py',r/'config.json',r/'decision.json'] if p.exists())+'建筑细节优先通用默认配置四张地图测试静止行走转向改善不保证绝对零闪烁高精度历史缓冲最新结果复录保留已完成'
chars=''.join(sorted({c for c in text if ord(c)>127}))
u='https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400&display=swap&text='+urllib.parse.quote(chars)
css=urllib.request.urlopen(u,timeout=25).read().decode();url=re.search(r'url\((https://[^)]+)\)',css).group(1)
(s/'chinese.woff2').write_bytes(urllib.request.urlopen(url,timeout=25).read())
(r/'font_source.json').write_text(json.dumps({'css_url':u,'font_url':url,'characters':chars},ensure_ascii=False,indent=2))
