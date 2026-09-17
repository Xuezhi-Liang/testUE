import json
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox']);page=b.new_page(viewport={'width':1440,'height':1080});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 assert page.goto('http://127.0.0.1:8500/longvideo/chemical-far/').status==200
 page.wait_for_function("document.querySelector('#crop video').readyState >= 1")
 page.select_option('#variant','taa2');page.locator('#crop button').click();page.wait_for_timeout(2200)
 a=page.locator('#crop video').evaluate_all('(vs)=>vs.map(v=>({time:v.currentTime,duration:v.duration,width:v.videoWidth,error:v.error?.message}))')
 assert all(v['time']>1 and v['duration']==8 and v['width']==1000 and not v['error'] for v in a),a
 assert abs(a[0]['time']-a[1]['time'])<.3,a
 page.locator('#crop video').first.evaluate('(v)=>{v.currentTime=4}')
 page.wait_for_timeout(800)
 t=page.locator('#crop video').evaluate_all('(vs)=>vs.map(v=>v.currentTime)');assert abs(t[0]-t[1])<.3,t
 page.locator('#crop button').click();page.select_option('#variant','taa3');page.wait_for_timeout(500)
 assert 'taa3_crop.mp4' in page.locator('#crop video').nth(1).get_attribute('src')
 final=page.locator('#delivery video')
 if final.count():
  final.evaluate('(v)=>v.play()');page.wait_for_timeout(1700)
  f=final.evaluate('(v)=>({time:v.currentTime,duration:v.duration,width:v.videoWidth,error:v.error?.message})');assert f['time']>.5 and f['duration']==60 and f['width']==1280 and not f['error'],f
  final.evaluate('(v)=>{v.currentTime=45}');page.wait_for_timeout(700);assert final.evaluate('(v)=>v.currentTime')>=45;final.evaluate('(v)=>v.pause()')
 else:f=None
 page.screenshot(path=str(root/'completed_page.png'),full_page=True)
 page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==390
 assert not errors,errors
 assert page.request.get('http://127.0.0.1:8500/longvideo/chemical-far/taa2.mp4',headers={'Range':'bytes=0-1023'}).status==206
 for path in ['/dubai/?view=first','/longvideo/flicker-fix/','/longvideo/latest-config-20260916/']:
  assert page.request.get('http://127.0.0.1:8500'+path).status==200
 result={'crop_playback':a,'sync_seek':t,'variant_switch':True,'final_video':f,'mobile_overflow':False,'javascript_errors':errors,'range_requests':206,'previous_pages':200}
 (root/'browser_validation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));b.close()
