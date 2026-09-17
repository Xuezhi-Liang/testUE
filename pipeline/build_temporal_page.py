#!/usr/bin/env python3
"""Publish local, independently recorded anti-flicker comparisons; preserve old galleries."""
from pathlib import Path
import json,shutil,sys,os
root=Path(sys.argv[1]);site=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/flicker-fix');site.mkdir(parents=True,exist_ok=True)
labels={'fresh_ss2':'修复前：每帧新建状态＋2×','temporal_ss1':'连续 TSR · 1×','temporal_ss2':'连续 TSR · 2×','taa_ss2':'连续 TAA · 2×','temporal_fixed':'连续 TSR · 2× · 固定 GI 采样','taa_fixed':'连续 TAA · 2× · 固定 GI 采样'}
result={'status':'对比验证中','maps':[],'root_cause':'UE 5.8 的场景采集组件默认关闭 TemporalAA。只设置 TSR/TAA 控制台参数会退回 FXAA；每帧新建状态又会丢失历史。修复显式打开采集组件的抗锯齿开关，保留连续历史，并在首个位置实际渲染 32 帧预热。','note':'运动指标包含光流误差和遮挡变化，不能等同于精确的闪烁像素真值。请同步播放原始速度的视频，检查建筑边缘、细节与拖影。静止噪声、清晰度和亮度单独列出。'}
full=root/'full_validation/episode/hwaseong_temporal_full/rgb.mp4'
check=root/'full_validation/validation.json'
if full.exists() and check.exists():
 shutil.copy2(full,site/'hwaseong_full.mp4')
 result['full_validation']={'src':'hwaseong_full.mp4',**json.loads(check.read_text())}
final=root/'delivery.json'
if final.exists():result.update(json.loads(final.read_text()))
for name,title in [('hwaseong','华城 · 屋瓦、砖缝与建筑边缘'),('chemical','化工厂 · 管架、栏杆与远景细线')]:
 d=root/name;measure={}
 for p in [d/'comparison.json',d/'taa_comparison.json',d/'final_comparison.json']:
  if p.exists():measure.update(json.loads(p.read_text())['episodes'])
 variants=[]
 for tag,label in labels.items():
  ep=d/tag/(name+'_probe');mp4=ep/'rgb.mp4';st=d/tag/'engine_status.json'
  if not mp4.exists() or not st.exists():continue
  status=json.loads(st.read_text());assert status['finished'] and status['frame']==status['total'] and status['write_failures']==0
  filename=f'{name}_{tag}.mp4';target=site/filename
  if not target.exists() or target.stat().st_size!=mp4.stat().st_size:shutil.copy2(mp4,target)
  poster=f'{name}_{tag}.jpg';shutil.copy2(ep/'rgb/000100.jpg',site/poster)
  variants.append({'tag':tag,'label':label,'src':filename,'poster':poster,'metrics':measure.get(tag),'runtime':status})
 if variants:result['maps'].append({'id':name,'title':title,'variants':variants})
t=site/'results.json.tmp';t.write_text(json.dumps(result,ensure_ascii=False,indent=2));os.replace(t,site/'results.json')
(site/'index.html').write_text('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>移动建筑闪烁 · 原生采集对比</title><style>body{background:#0d141c;color:#e6edf3;font:16px/1.7 system-ui,sans-serif;margin:0}main{max-width:1440px;margin:auto;padding:32px 24px}a{color:#8cd5ce}h1{font-size:30px;margin:24px 0 8px}h2{font-size:23px}h3{font-size:16px;margin:12px}.note{color:#9eacbb;font-size:14px}.status{color:#8cd5ce;padding:12px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.card{background:#17212c;border:1px solid #334352;border-radius:8px;overflow:hidden}video{width:100%;display:block}button,select{background:#233443;border:1px solid #617585;color:#e6edf3;border-radius:5px;padding:10px;margin:0 10px 10px 0;cursor:pointer}.table{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;margin:20px 0 40px}th,td{text-align:left;border-bottom:1px solid #334352;padding:10px;white-space:nowrap}summary{cursor:pointer;color:#8cd5ce}.bad{color:#ffc17c}@media(max-width:780px){.grid{grid-template-columns:1fr}main{padding:20px 14px}h1{font-size:25px}}</style><main><a href="../flicker/">← 保留的之前闪烁实验</a><h1>摄像头移动时，建筑还会不会闪？</h1><div id="status" class="status">加载结果…</div><p id="cause"></p><p class="note" id="note"></p><p class="note">取舍：华城静止帧差从 0.195 增至 0.802 / 255；化工厂从 1.458 降至 1.197。TSR 仍可能有细微噪声或遮挡边缘残影。深度一致率比较独立录制的像素：华城首帧约 98.91%，其余抽查帧 ≥99.998%；化工厂首帧约 97.59%；没有对深度做抗锯齿或平滑。详细数据见 <a href="results.json">JSON 报告</a>。</p><div id="maps"></div><div id="full"></div><p class="note">本机 UE 5.8 直接采集，1280×720、24 fps。对比路线一致；RGB 原始 JPEG、原生深度 EXR 与相机姿态均保留。旧视频和旧实验页保持可访问。</p></main><script>
const seen=new Set();let data;
function fmt(x){return x==null?'待测':x.toFixed(3)}
function addMap(m){const s=document.createElement('section');s.id=m.id;s.innerHTML=`<h2>${m.title}</h2><button class="play">同步播放 / 暂停</button><button class="reset">回到开头</button><select aria-label="选择对比方案"></select><div class="grid"><article class="card"><h3>修复前</h3><video controls muted playsinline preload="metadata"></video></article><article class="card"><h3 class="selected"></h3><video controls muted playsinline preload="metadata"></video></article></div><div class="table"></div>`;document.querySelector('#maps').append(s);const [a,b]=s.querySelectorAll('video');a.src=m.variants[0].src;a.poster=m.variants[0].poster;const select=s.querySelector('select');select.onchange=()=>{const v=data.maps.find(x=>x.id===m.id).variants.find(x=>x.tag===select.value);s.querySelector('.selected').textContent=v.label;b.src=v.src;b.poster=v.poster;b.onloadedmetadata=()=>{b.currentTime=a.currentTime;if(!a.paused)b.play()}};s.querySelector('.play').onclick=()=>{if(a.paused){b.currentTime=a.currentTime;Promise.all([a.play(),b.play()]).catch(console.error)}else{a.pause();b.pause()}};s.querySelector('.reset').onclick=()=>{a.currentTime=0;b.currentTime=0};a.onseeked=()=>{if(Math.abs(b.currentTime-a.currentTime)>.12)b.currentTime=a.currentTime};a.onended=()=>b.pause();setInterval(()=>{if(!a.paused&&!b.paused&&Math.abs(a.currentTime-b.currentTime)>.15)b.currentTime=a.currentTime},500)}
async function update(){data=await(await fetch('results.json',{cache:'no-store'})).json();document.querySelector('#status').textContent=data.status;document.querySelector('#cause').textContent=data.root_cause;document.querySelector('#note').textContent=data.note;if(data.full_validation&&!document.querySelector('#full video')){document.querySelector('#full').innerHTML='<h2>完整往返路线 · 新配置验证</h2><p class=note>118.5 秒，2844 帧。保留行走、转向、停留及返回路线。</p><video controls muted playsinline preload=metadata src=hwaseong_full.mp4></video>'}for(const m of data.maps){if(!seen.has(m.id)){addMap(m);seen.add(m.id)}const s=document.getElementById(m.id),sel=s.querySelector('select');const old=sel.value;for(const v of m.variants.slice(1)){if(![...sel.options].some(x=>x.value===v.tag)){const o=document.createElement('option');o.value=v.tag;o.textContent=v.label;sel.append(o)}}if(!old&&sel.options.length){sel.value=[...sel.options].some(x=>x.value==='temporal_ss2')?'temporal_ss2':sel.options[0].value;sel.onchange()}s.querySelector('.table').innerHTML='<table><thead><tr><th>方案</th><th>移动残差 ↓</th><th>移动闪点 % ↓</th><th>静止帧差 ↓</th><th>清晰度</th><th>平均亮度</th><th>位姿差</th><th>深度像素一致率（最低）</th></tr></thead><tbody>'+m.variants.map(v=>{const q=v.metrics;if(!q)return `<tr><td>${v.label}</td><td colspan="7">测量中</td></tr>`;return `<tr><td>${v.label}</td><td>${fmt(q.motion_residual_mean)}</td><td>${fmt(q.motion_sparkle_pct)}</td><td>${fmt(q.static_diff)}</td><td>${fmt(q.sharpness)}</td><td>${fmt(q.mean_luma)}</td><td>${q.position_difference_cm} cm / ${q.rotation_difference_deg}°</td><td>${fmt(Math.min(...q.depth_samples.map(x=>x.equal_pixel_pct??(x.exact_equal?100:0))))}%</td></tr>`}).join('')+'</tbody></table>'}}
update().catch(console.error);setInterval(()=>update().catch(console.error),10000);
</script></html>''')
print('Published',site,[m['id'] for m in result['maps']])
