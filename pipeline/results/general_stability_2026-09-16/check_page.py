import json
from pathlib import Path
from playwright.sync_api import sync_playwright
r=Path(__file__).resolve().parent
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox']);page=b.new_page(viewport={'width':1440,'height':1100});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 assert page.goto('http://127.0.0.1:8500/longvideo/general-stability/').status==200
 page.wait_for_function("document.querySelector('#variant').options.length>0")
 data=page.request.get('http://127.0.0.1:8500/longvideo/general-stability/results.json').json();checks=[]
 for name,row in data['maps'].items():
  if len(row['variants'])<2:continue
  page.select_option('#map',name)
  for v in row['variants']:
   if v['id']=='baseline':continue
   page.select_option('#variant',v['id']);page.wait_for_function("document.querySelector('#b').readyState>=2")
   page.locator('#play').click();page.wait_for_timeout(1000)
   q=page.locator('video').evaluate_all('(vs)=>vs.slice(0,2).map(v=>({time:v.currentTime,duration:v.duration,width:v.videoWidth,error:v.error?.message}))')
   assert all(x['time']>.2 and x['width']==1280 and x['duration']==25 and not x['error'] for x in q),q
   assert abs(q[0]['time']-q[1]['time'])<.3,q
   page.locator('#a').evaluate('(v)=>{v.currentTime=10}');page.wait_for_timeout(600)
   t=page.locator('video').evaluate_all('(vs)=>vs.slice(0,2).map(v=>v.currentTime)');assert abs(t[0]-t[1])<.3,t
   page.locator('#play').click();checks.append({'map':name,'variant':v['id'],'playback':q,'seek':t})
 page.select_option('#map','chemical');page.select_option('#variant','taa_detail');page.locator('#a').evaluate('(v)=>{v.currentTime=6.25}');page.wait_for_timeout(600)
 for id in ['za','zb']:
  assert page.locator('#'+id).evaluate('(c)=>{let d=c.getContext("2d").getImageData(0,0,c.width,c.height).data;return d.some((x,i)=>i%4!==3&&x>0)}')
 page.screenshot(path=str(r/'web_desktop.png'),full_page=True);page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==390
 assert not errors,errors
 for url in ['/dubai/?view=first','/longvideo/chemical-far/','/longvideo/latest-config-20260916/']:
  assert page.request.get('http://127.0.0.1:8500'+url).status==200
 assert page.request.get('http://127.0.0.1:8500/longvideo/general-stability/chemical_taa_detail.mp4',headers={'Range':'bytes=0-1023'}).status==206
 report={'checks':checks,'zoom_canvases':True,'mobile_overflow':False,'javascript_errors':errors,'previous_pages':200,'range_requests':206}
 (r/'browser_validation.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));b.close()
