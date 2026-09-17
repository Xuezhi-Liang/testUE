import json,re,urllib.request,urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright
R=Path(__file__).resolve().parent;S=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/route-validation')
chars=''.join(sorted({c for c in (S/'index.html').read_text()+(R/'manifest.json').read_text()+(R/'worker.py').read_text()+(R/'run.py').read_text() if ord(c)>127}))
cssurl='https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400&display=swap&text='+urllib.parse.quote(chars)
css=urllib.request.urlopen(cssurl,timeout=30).read().decode();fonturl=re.search(r'url\((https://[^)]+)\)',css).group(1)
(S/'chinese.woff2').write_bytes(urllib.request.urlopen(fonturl,timeout=30).read());(S/'index.html').write_text((S/'index.html').read_text().replace('../route-audit/chinese.woff2','chinese.woff2'))
(R/'font_source.json').write_text(json.dumps({'css':cssurl,'font':fonturl}))
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox']);page=b.new_page(viewport={'width':1440,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 assert page.goto('http://127.0.0.1:8500/longvideo/route-validation/').status==200
 page.wait_for_function('document.querySelectorAll("article").length===86');page.evaluate('document.fonts.ready');page.screenshot(path=str(R/'page_desktop.png'))
 page.fill('#search','ChemicalPlant_2');assert page.locator('article').count()==1
 page.fill('#search','');page.select_option('#filter','active');assert page.locator('article').count()<=11
 assert page.locator('#fleet .stat').count()==10
 page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==390;page.screenshot(path=str(R/'page_mobile.png'))
 for u in ['/dubai/?view=first','/longvideo/route-audit/','/longvideo/general-stability-more/']:
  assert page.request.get('http://127.0.0.1:8500'+u).status==200
 assert not errors,errors
 (R/'page_validation.json').write_text(json.dumps(dict(cards=86,mobile_width=390,js_errors=errors,old_pages_preserved=True),indent=2));b.close();print('Browser checks passed')
