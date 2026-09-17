import json
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox']);page=b.new_page(viewport={'width':1440,'height':1080});errors=[];page.on('pageerror',lambda e:errors.append(str(e)));res=page.goto('http://127.0.0.1:8500/longvideo/latest-config-20260916/');assert res.status==200
 page.wait_for_selector('#chemical video',timeout=15000);checks=[]
 for name in ['downtown','chemical']:
  v=page.locator('#'+name+' video');v.evaluate('(v)=>v.play()');page.wait_for_timeout(1500);a=v.evaluate('(v)=>({time:v.currentTime,duration:v.duration,width:v.videoWidth,height:v.videoHeight,error:v.error?.message})');assert a['time']>.5 and a['duration']==118.5 and a['width']==1280 and not a['error'],a
  v.evaluate('(v)=>{v.currentTime=60}');page.wait_for_timeout(900);assert v.evaluate('(v)=>v.currentTime')>=60;v.evaluate('(v)=>v.pause()');checks.append({'map':name,**a})
 page.screenshot(path=str(root/'completed_page.png'),full_page=True);page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==390
 assert not errors,errors
 for name in ['downtown','chemical']:
  r=page.request.get('http://127.0.0.1:8500/longvideo/latest-config-20260916/'+name+'.mp4',headers={'Range':'bytes=0-1023'});assert r.status==206
 for path in ['/dubai/?view=first','/longvideo/flicker-fix/']:
  assert page.request.get('http://127.0.0.1:8500'+path).status==200
 result={'videos':checks,'mobile_overflow':False,'javascript_errors':errors,'range_requests':206,'previous_pages':200};(root/'browser_validation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));b.close()
