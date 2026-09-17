from pathlib import Path
import json,collections,html,shutil,math
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection

R=Path(__file__).resolve().parent
P=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline')
SITE=Path('/home/ubuntu/WM-Unreal-data-collection/local_run/site/longvideo/route-audit')
SITE.mkdir(exist_ok=True);(SITE/'plots').mkdir(exist_ok=True);(SITE/'reports').mkdir(exist_ok=True)
slugs=sorted(p.stem for p in (P.parent.parent/'batch_inference/start_positions').glob('*.json'))
core={q['map']:q for q in json.loads((P/'_core_survey/core_survey.json').read_text())}
unmeasured={q['slug']:q['reason'] for q in json.loads((SITE.parent/'core/unmeasured.json').read_text())}
selected={q['map_id']:q for q in json.loads((R/'selected_cloud.json').read_text())}
cloud=collections.defaultdict(list)
for f in (R/'cloud').glob('*/trajectory.json'):
 d=json.loads(f.read_text());cloud[d['map_id']].append(d)
local=collections.defaultdict(list)
for f in (P/'frozen').glob('Game_*.json'):local[f.name.split('__')[0]].append(f)
reviews=json.loads((R/'reviews.json').read_text()) if (R/'reviews.json').exists() else {}
rows=[]
for slug in slugs:
 row={'slug':slug,'label':slug.removeprefix('Game_'),'core':core.get(slug),'historical_start_issue':unmeasured.get(slug),'route_kind':'none','notes':[]}
 mid=next((k for k in selected if 'Game_'+k.removeprefix('/Game/').replace('/','_')==slug),None)
 d=None;poses=None
 if mid:
  e=selected[mid];folder=R/'cloud'/e['episode_id'];d=json.loads((folder/'trajectory.json').read_text());q=json.loads((folder/'route_sample.json').read_text());poses=np.array(q['poses'],float)
  row.update(route_kind='recorded',source=e['prefix'],episode=e['episode_id'],historical_accepted=e['accepted'],frames_read=q['csv_frames_read'],sample_stride=12)
  row['notes'].append('实录轨迹：来自历史 frames.csv，每 12 帧采样。每图展示一份代表片段，优先已通过验收者，再取较大种子；不是所有分片的并集。')
  row['all_runs']=[{'episode':x['episode_id'],'seed':x.get('task',{}).get('seed'),'kept':x.get('head_clearance_pruning',{}).get('kept_fraction'),'coverage':x.get('coverage',{}).get('fraction')} for x in cloud[mid]]
 elif local.get(slug):
  fs=local[slug];cs=[f for f in fs if '__coverage_walk__' in f.name]
  if cs:
   ds=[(f,json.loads(f.read_text())) for f in cs]
   # Prefer passing prechecks; otherwise show the widest preserved network and its failed checks.
   f,d=max(ds,key=lambda x:(all(x[1].get('pre_gates',{}).values()),x[1].get('head_clearance_pruning',{}).get('kept_fraction',0),x[1].get('frames',0)))
   row['route_kind']='planned_coverage'
  else:
   f=max(fs,key=lambda f:('v3' in f.name,'batch' in f.name,f.stat().st_mtime));d=json.loads(f.read_text());row['route_kind']='short_route'
  pp=d.get('poses',[])
  if pp:
   step=max(1,len(pp)//20000);ii=list(range(0,len(pp),step));ii+=[] if ii[-1]==len(pp)-1 else [len(pp)-1]
   poses=np.array([[i,pp[i]['x_cm'],pp[i]['y_cm'],pp[i]['z_cm'],pp[i]['yaw_deg']] for i in ii])
  row.update(source=str(f),episode=d.get('episode_id'),sample_stride=step if pp else None)
  row['notes'].append('本地冻结计划，非完整实录验收。' if cs else '仅有短距离往返路线，不能当成全图覆盖路线。')
 if d and row['route_kind']=='planned_coverage':
  ep=P/'episodes'/d['episode_id'];st=ep/'engine_states.jsonl'
  if st.exists() and (ep/'capture_summary.json').exists():
   pp=[];count=0
   for line in st.open():
    q=json.loads(line);count+=1
    if q['frame_id']%12==0:pp.append([q['frame_id'],*q['actual_location'],q['actual_rotation_pyr'][1]])
   pp.append([q['frame_id'],*q['actual_location'],q['actual_rotation_pyr'][1]]);poses=np.array(pp);row.update(route_kind='recorded_local',source=str(st),frames_read=count,sample_stride=12);row['notes']=['本地实录轨迹：engine_states.jsonl 每 12 帧采样。']
   ap=ep/'acceptance.json'
   if ap.exists():row['historical_accepted']=json.loads(ap.read_text()).get('accepted')
 if d:
  row.update(map_id=d.get('map_id'),duration_s=d.get('duration_s'),family=d.get('trajectory_family'),kept=d.get('head_clearance_pruning',{}).get('kept_fraction'),coverage=d.get('coverage',{}).get('fraction'),failed_prechecks=[k for k,v in d.get('pre_gates',{}).items() if not v],disconnected=d.get('head_clearance_pruning',{}).get('dropped_disconnected'),components=d.get('head_clearance_pruning',{}).get('components_after_pruning'))
  (SITE/'reports'/f'{slug}.json').write_text(json.dumps({k:v for k,v in d.items() if k!='poses'},indent=2))
  row['report']=f'reports/{slug}.json'
 if poses is not None:
  nb=P/'frozen/nav'/f'{slug}.bin';nj=nb.with_suffix('.json')
  fig,ax=plt.subplots(figsize=(8,7),facecolor='#111b25');ax.set_facecolor('#111b25')
  if nb.exists() and nj.exists():
   nm=json.loads(nj.read_text());blob=nb.read_bytes();v=nm['vertex_count'];t=nm['triangle_count'];vs=np.frombuffer(blob[:v*12],'<f4').reshape(v,3);ts=np.frombuffer(blob[v*12:],'<i4').reshape(t,3);tri=vs[ts]
   ax.add_collection(PolyCollection(tri[:,:,:2]/100,facecolors='#313d48',edgecolors='none',rasterized=True))
   ground=poses[:,3]-float(d.get('eye_height_cm',170));low,high=np.percentile(ground,[5,95]);mask=(np.median(tri[:,:,2],axis=1)>=low-150)&(np.median(tri[:,:,2],axis=1)<=high+150)
   ax.add_collection(PolyCollection(tri[mask,:,:2]/100,facecolors='#65717d',edgecolors='none',rasterized=True))
   ax.update_datalim(vs[:,:2]/100)
   row['notes'].append('灰底是本地缓存导航网格，可能与历史录制时的导航导出范围不同；俯视投影不证明跨楼层可达。')
  xy=poses[:,1:3]/100;ax.plot(xy[:,0],xy[:,1],color='#5ce0ed',lw=.7,alpha=.8,label='Route')
  ix=np.linspace(0,len(poses)-1,min(20,len(poses)),dtype=int);th=np.deg2rad(poses[ix,4]);ax.quiver(xy[ix,0],xy[ix,1],np.cos(th),np.sin(th),color='#ffc46c',scale=28,width=.003,label='Look direction')
  
  from matplotlib.patches import Rectangle
  for target in reviews.get(slug,{}).get('targets',[]):
   x,y,w,h=target['box_m'];ax.add_patch(Rectangle((x,y),w,h,fill=False,edgecolor='#ff7b7b',lw=2,ls='--'));ax.text(x,y+h,target['id'],color='#ff7b7b',fontsize=14,fontweight='bold')
  ax.scatter(*xy[0],c='#76ff83',s=55,zorder=5,label='Start');ax.scatter(*xy[-1],c='#ff8e9c',s=40,marker='x',zorder=5,label='End');ax.autoscale_view();ax.set_aspect('equal');ax.set_xlabel('UE X (m)',color='white');ax.set_ylabel('UE Y (m)',color='white');ax.tick_params(colors='#c5cdd4');ax.set_title(row['label'].replace('_Maps_',' / ')+'\n'+row['route_kind'],color='white',fontsize=10,wrap=True);ax.legend(loc='upper right',fontsize=8);fig.tight_layout();out=SITE/'plots'/f'{slug}.png';fig.savefig(out,dpi=125);plt.close(fig);row['plot']='plots/'+out.name
 if row['route_kind']=='none':row['status']='修起点 / 补导航' if slug in unmeasured else '需生成完整路线'
 elif row['route_kind']=='short_route':row['status']='需生成完整路线'
 elif row.get('kept') is not None and row['kept']<.75:row['status']='优先检查补路'
 elif row.get('failed_prechecks'):row['status']='路线预检未通过'
 else:row['status']='可作为候选，待看图'
 if slug in reviews:row.update(reviews[slug])
 draft=R/'drafts'/f'{slug}.json'
 if draft.exists():
  dd=json.loads(draft.read_text());row['draft']={k:v for k,v in dd.items() if k!='polyline_xy_cm'}
  if dd['state']=='draft_ready':
   target=SITE/'plots'/f'{slug}_draft.png';shutil.copy2(draft.with_suffix('.png'),target);row['draft_plot']='plots/'+target.name
   row['draft_report']=f'reports/{slug}_draft.json';(SITE/row['draft_report']).write_text(json.dumps(dd))
   if row['route_kind']=='none':row['route_kind']='offline_draft'
   if row['status']=='需生成完整路线':row['status']='草案待引擎验证'
   row['notes'].append('新增离线路线草案使用现有算法和本地导航缓存，仅完成路网选取与空间遍历；尚无身体碰撞、深度、完整摄像机动作与逐帧验证，不能直接录制。草案中没有黄色观察方向箭头。')
  else:row['status']='离线规划需排查'
 rows.append(row)
counts=dict(collections.Counter(x['route_kind'] for x in rows));status_counts=dict(collections.Counter(x['status'] for x in rows));result={'scope':86,'route_counts':counts,'status_counts':status_counts,'note':'Existing evidence audit, not a new 86-map route generation run. Core survey is geometry heuristic, not semantic buildings or rendered overhead image.','maps':rows}
(R/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));(SITE/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
order={'优先检查补路':0,'路线预检未通过':1,'可沿用主体路线，待预录验收':2,'可作为候选，待看图':2,'需生成完整路线':3,'修起点 / 补导航':4}
def esc(x):return html.escape(str(x))
def pct(x):return '无数据' if x is None else f'{100*x:.1f}%'
cards=[]
for row in sorted(rows,key=lambda x:(order.get(x['status'],2),x['slug'])):
 slug=row['slug'];imgs=''
 if row.get('plot'):imgs+=f'<a href="{row["plot"]}" target="_blank"><img loading="lazy" src="{row["plot"]}" alt="路线俯视图"></a>'
 if row.get('draft_plot'):imgs+=f'<a href="{row["draft_plot"]}" target="_blank"><p>新增离线草案 · 尚未引擎验证</p><img loading="lazy" src="{row["draft_plot"]}" alt="离线路线草案"></a>'
 if row.get('core'):imgs+=f'<a href="../core/png/{slug}.png" target="_blank"><img loading="lazy" src="../core/png/{slug}.png" alt="历史内容区域调查"></a>'
 note=row.get('review','') or row.get('historical_start_issue','') or ''
 cards.append(f'<article id="{slug}" data-search="{esc(row["label"]+row["status"])}" data-status="{esc(row["status"])}"><h2>{esc(row["label"])}</h2><b>{esc(row["status"])}</b><p>{esc(note)}</p><p>证据：{esc(row["route_kind"])} · 路网保留 {pct(row.get("kept"))} · 计划路网内覆盖 {pct(row.get("coverage"))}</p><div class="images">{imgs or "暂无可查看图；需要修复输入并生成。"}</div><details><summary>数据来源及限制</summary><p>{"<br>".join(esc(n) for n in row["notes"])}</p><p>{esc(row.get("episode",""))}</p><p>失败预检：{esc(row.get("failed_prechecks",[]))}；历史实录验收：{esc(row.get("historical_accepted","不适用"))}</p><p>{esc(row.get("source","只有历史导航调查，暂无本轮可审查完整路线"))}</p>'+(f'<a href="{row["report"]}">原路线报告</a>' if row.get('report') else '')+'</details></article>')
options=''.join(f'<option>{esc(x)}</option>' for x in status_counts)
page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>86 张地图 · 路线审查</title><style>@font-face{font-family:LocalChinese;src:url(chinese.woff2)}*{box-sizing:border-box}body{background:#101922;color:#e0e9ef;font:16px/1.6 LocalChinese,system-ui;margin:0}main{max-width:1450px;margin:auto;padding:24px}a{color:#7cdeeb}input,select{padding:12px;max-width:100%;margin:6px;background:#243846;color:white;border:1px solid #688090;font:inherit}article{padding:18px;background:#1c2b37;border-radius:12px;margin:20px 0;overflow-wrap:anywhere}h2{font-size:19px}b{color:#ffd391}.images{display:grid;grid-template-columns:1fr 1fr;gap:18px}.images img{width:100%;max-height:660px;object-fit:contain;background:#101922}details{margin-top:12px;color:#b6c7d1}.note{color:#b6c7d1}@media(max-width:700px){main{padding:12px}.images{grid-template-columns:1fr}}[hidden]{display:none!important}</style><main><a href="../general-stability-more/">← 抗闪测试</a> · <a href="../core/">历史导航调查</a><h1>86 张地图：先看路线，再决定补录</h1><p>本页审查已有资料，并为缺少完整路线的地图生成离线草案；尚未启动全量录制。已逐图看过 73 张路线图，另 13 张检查了输入问题；全部 86 张均有审查意见。旧结果全部保留。</p><p>COUNTS</p><p>路线图：青色路线、黄色观察方向、绿色起点、粉色终点；红色虚线框是建议复查区域，尚未验证可达；灰色为缓存导航网格。区域图：历史调查中绿色为内容密集区域估计、橙色为障碍足迹、灰色为其他可走区域。区域图不是路线，也不是建筑语义标注。</p><p class="note">“计划路网内覆盖”取自历史路线报告，本轮没有按实录重新计算；100% 不能证明全地图走完。导航缓存与历史录制可能不同；多楼层在俯视图中会重叠。实际观看过的图会注明审查意见，缺资料的地图不判为通过。</p><input id="search" placeholder="搜索地图名"><select id="filter"><option value="">全部状态</option>OPTIONS</select><span id="visible"></span><p><a href="audit.json">下载 86 张审查清单</a></p>CARDS</main><script>function filter(){let n=0;document.querySelectorAll('article').forEach(e=>{e.hidden=!(e.dataset.search.toLowerCase().includes(document.querySelector('#search').value.toLowerCase())&&(!document.querySelector('#filter').value||e.dataset.status===document.querySelector('#filter').value));if(!e.hidden)n++});document.querySelector('#visible').textContent=n+' / 86 张'}document.querySelector('#search').oninput=filter;document.querySelector('#filter').onchange=filter;filter()</script></html>'''.replace('COUNTS',esc(f"已绘图 {sum(bool(x.get('plot') or x.get('draft_plot')) for x in rows)} 张：历史实录 {counts.get('recorded',0)+counts.get('recorded_local',0)} 张，冻结覆盖计划 {counts.get('planned_coverage',0)} 张，短往返路线 {counts.get('short_route',0)} 张；另有 {sum(bool(x.get('draft_plot')) for x in rows)} 张新增离线草案；其余 {counts.get('none',0)} 张未找到可用轨迹。")).replace('OPTIONS',options).replace('CARDS',''.join(cards))
(SITE/'index.html').write_text(page)
print(json.dumps({'counts':counts,'status':status_counts},ensure_ascii=False),flush=True)
