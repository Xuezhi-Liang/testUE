from pathlib import Path
import json,urllib.request,urllib.parse,re
from playwright.sync_api import sync_playwright

r=Path(__file__).resolve().parent
site=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/route-audit')
chars=''.join(sorted({c for c in (site/'index.html').read_text() if ord(c)>127}))
url='https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400&display=swap&text='+urllib.parse.quote(chars)
css=urllib.request.urlopen(url,timeout=30).read().decode();font_url=re.search(r'url\((https://[^)]+)\)',css).group(1)
(site/'chinese.woff2').write_bytes(urllib.request.urlopen(font_url,timeout=30).read())
shutil_license=site.parent/'general-stability-more/font-license.txt'
if shutil_license.exists():(site/'font-license.txt').write_bytes(shutil_license.read_bytes())
(r/'font_source.json').write_text(json.dumps({'css':url,'font':font_url,'characters':len(chars)}))
data=json.loads((site/'audit.json').read_text());assert len(data['maps'])==86
assert sum(bool(x.get('plot')) for x in data['maps'])==23
assert sum(bool(x.get('review')) for x in data['maps'])==86
assert sum(bool(x.get('visually_reviewed')) for x in data['maps'])==73
assert sum(bool(x.get('draft_plot')) for x in data['maps'])==59
assert all((site/x['plot']).exists() for x in data['maps'] if x.get('plot'))
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox']);page=b.new_page(viewport={'width':1440,'height':1100});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 assert page.goto('http://127.0.0.1:8500/longvideo/route-audit/').status==200
 page.evaluate('document.fonts.ready');assert page.locator('article').count()==86
 page.fill('#search','ChemicalPlant_2');assert page.locator('article:visible').count()==1
 page.wait_for_function("[...document.querySelectorAll('article:not([hidden]) img')].every(x=>x.complete&&x.naturalWidth>0)")
 page.screenshot(path=str(r/'browser_chemical.png'),full_page=True)
 page.fill('#search','');page.select_option('#filter',label='优先检查补路');assert page.locator('article:visible').count()==data['status_counts']['优先检查补路']
 page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==390
 for q in data['maps']:
  if q.get('plot'):assert page.request.get('http://127.0.0.1:8500/longvideo/route-audit/'+q['plot']).status==200
  if q.get('draft_plot'):assert page.request.get('http://127.0.0.1:8500/longvideo/route-audit/'+q['draft_plot']).status==200
  if q.get('core'):assert page.request.get('http://127.0.0.1:8500/longvideo/core/png/'+q['slug']+'.png').status==200
 for u in ['/dubai/?view=first','/longvideo/general-stability/','/longvideo/general-stability-more/','/longvideo/core/']:
  assert page.request.get('http://127.0.0.1:8500'+u).status==200
 assert not errors,errors
 report={'maps':86,'route_plots':23,'visual_reviews':73,'input_reviews':13,'offline_draft_plots':59,'priority_filter':data['status_counts']['优先检查补路'],'mobile_width':390,'js_errors':errors,'previous_pages_preserved':True}
 (r/'validation.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));b.close()
