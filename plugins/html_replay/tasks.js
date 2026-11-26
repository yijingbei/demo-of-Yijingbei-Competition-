/* Shared mission inspection for combat, reconnaissance and dispatch replays. */
let taskZoneMeshes=[];
function isCombat(d=DATA){return !d.meta.track||d.meta.track==='interception'}
function trackName(d=DATA){return {interception:'拦截交战',reconnaissance:'协同侦察',emergency:'应急响应'}[d.meta.track]||'任务'}
function buildTaskZones(){
 taskZoneMeshes=[];
 for(const z of DATA.mission?.zones||[]){
  const color={search:0x66dce8,risk:0xff9c76,service:0xe8cc86,watch:0xb9a1e7}[z.kind]||0x66dce8;
  const pts=z.coordinates_m.map(p=>new THREE.Vector3(p[0]/1000,.16,-p[1]/1000));
  const line=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts),new THREE.LineDashedMaterial({color,transparent:true,opacity:.8,dashSize:.04,gapSize:.025}));
  line.computeLineDistances();line.userData.zone=z;world.add(line);taskZoneMeshes.push(line);
 }
}
function taskChapters(d){
 const name=PACK.scenarios.find(s=>s.id===d.meta.scenario).name;
 const chapters=[{t:0,name:'初始任务',title:name+' · '+trackName(d),copy:d.public.task||'按公开任务执行；位置和消息均来自实验记录。',...focusFor(d,[],0)}];
 for(const ev of d.events.filter(e=>['dispatch','message','spawn','loss'].includes(e.type)).slice(0,20))chapters.push({t:ev.t,name:ev.type==='dispatch'?'任务通知':ev.type==='message'?'报告投递':'状态变化',title:ev.title,copy:ev.detail,...focusFor(d,[ev.a].filter(Boolean),ev.t)});
 chapters.push({t:d.meta.end,name:'裁判结算',title:outcomeName(d),copy:'查看原始裁判分项。已收到消息、进入区域与完成任务分别检查，不按画面推断任务完成。',...focusFor(d,[],d.meta.end)});
 return chapters.sort((a,b)=>a.t-b.t);
}
function mountPublicTask(){
 const brief=DATA.public, card=$('public-task'), list=$('public-task-rules');
 list.replaceChildren();
 $('public-task-copy').textContent=brief.task||'';
 function field(label,values){
  const term=document.createElement('dt');term.textContent=label;list.appendChild(term);
  for(const value of values){const detail=document.createElement('dd');detail.textContent=value;list.appendChild(detail)}
 }
 if(brief.protected_facilities?.length){
  field('保护设施',brief.protected_facilities.map(f=>{
   const name=DATA.entities.find(e=>e.id===f.entity_id)?.name;
   const label=name&&name!==f.entity_id?name+'（'+f.entity_id+'）':f.entity_id;
   return label+' · 权重 '+f.weight+(Array.isArray(f.position_m)?' · 坐标 ('+f.position_m.join(', ')+') m':'');
  }));
 }
 const metrics={'metric.settled':'计划威胁均已停止且原生结算完成','metric.protection':'无突破','metric.facilities':'加权设施完整度'};
 if(brief.task_success_criteria?.length){
  field('通关条件（须全部满足）',brief.task_success_criteria.map(c=>
   (metrics[c.metric_id]?metrics[c.metric_id]+' ('+c.metric_id+')':c.metric_id)+' '+c.comparison+' '+c.value));
 }
 for(const [key,label] of [['adjudication_tick','决策截止时间'],['duration_ticks','最长世界时间']]){
  const tick=brief[key];
  if(Number.isInteger(tick))field(label,[tick*tickSeconds()+' 秒（tick '+tick+'）']);
 }
 card.hidden=!list.childElementCount;
}

function neutralBuoyLosses(episode){
 const audit=episode.engagement?.final;
 const hasFinal=episode.checks?.finalCoordinatesVerified||
  (episode.checks?.replayFrameHashVerified&&episode.recording?.metadata?.complete);
 if(episode.meta.participantOnly||episode.meta.status!=='completed'||
    audit?.schema_version!=='autonomous-engagement-audit@2'||!hasFinal)return null;
 const own=episode.meta.factionConvention.participant_faction_id, by=audit.by_faction||{};
 const opponents=Object.keys(by).filter(id=>id!==own);
 if(opponents.length!==1)return null;
 const ownIds=by[own]?.attributable_third_party_loss_ids, otherIds=by[opponents[0]]?.attributable_third_party_loss_ids;
 if(!Array.isArray(ownIds)||!Array.isArray(otherIds))return null;
 const ours=new Set(ownIds), theirs=new Set(otherIds), lost=new Set([...ours,...theirs]);
 for(const e of episode.entities){
  if(e.side==='neutral'&&e.states.some(state=>['disabled','destroyed','wreck'].includes(state[2])))lost.add(e.id);
 }
 for(const e of episode.engagement.events){
  if(e.type==='third_party_damage'&&e.loss===true)lost.add(e.target_entity_id);
 }
 // Each buoy counts once. Only a unique native attribution assigns it to a side.
 const counts={own:0,opponent:0,unattributed:0};
 for(const id of lost){
  if(ours.has(id)===theirs.has(id))counts.unattributed++;
  else if(ours.has(id))counts.own++;
  else counts.opponent++;
 }
 return counts;
}

function mountTaskPanel(){
 const combat=isCombat();$('task-panel').hidden=combat;$('facility-panel').hidden=!combat;
 mountPublicTask();
 for(const side of ['blue','red']){$('view-'+side).textContent=sideName(side)+'视角';$('legend-'+side).textContent=sideName(side)}
 $('neutral-label').textContent=combat?'已停止威胁（含自爆）':'收到任务消息';
 $('shot-label').textContent=combat?'双方累计武器动作':'已有裁判指标';
 $('events-heading').textContent=combat?'交战事件':'任务与消息事件';
 $('task-description').textContent=DATA.public.task||'按公开任务执行。';
 $('task-evidence-note').textContent=DATA.mission?.notice||'旧日志缺少专属任务记录；不推断完成情况。';
 if(!combat){
  $('match-meta').textContent=DATA.meta.sourceVersion+' · '+trackName()+' · 策略 '+DATA.meta.policy+' · 种子 '+DATA.meta.seed;
  $('finish-copy').textContent='任务成败及得分读取原始裁判；过程包含实际轨迹、探测、消息投递与任务区域。';
 }
 $('task-zone-list').replaceChildren();
 for(const z of DATA.mission?.zones||[]){const row=document.createElement('button');row.className='task-zone';row.dataset.tick=z.availableTick;row.textContent=({search:'搜索区',risk:'风险区',service:'服务区',watch:'监视区'}[z.kind]||'任务区')+' · '+z.id;row.addEventListener('click',()=>{stopCameraAutomation();const pts=z.coordinates_m;targetOrbit.center.set(pts.reduce((n,p)=>n+p[0],0)/pts.length/1000,0,-pts.reduce((n,p)=>n+p[1],0)/pts.length/1000);targetOrbit.span=Math.min(targetOrbit.span,6)});$('task-zone-list').appendChild(row)}
}
function updateTaskPanel(){
 const t=displayTime();
 for(const mesh of taskZoneMeshes)mesh.visible=t>=mesh.userData.zone.availableTick&&(perspective==='god'||perspective===participantSide());
 if(isCombat())return;
 const messages=(DATA.mission?.messages||[]).filter(m=>(m.availableTick??m.tick)<=t&&(perspective==='god'||m.side===perspective));
 const score=perspective==='god'?(DATA.mission?.scores||[]).filter(s=>s.tick<=t).at(-1):null;
 $('neutral-count').textContent=messages.length;$('neutral-max').textContent=' 条实际投递';
 $('shot-count').textContent=Object.keys(score?.values||{}).length;$('shot-max').textContent=' 项';
 const list=$('task-score-list');list.replaceChildren();
 if(score)for(const [id,value] of Object.entries(score.values)){const row=document.createElement('div');row.className='task-metric';const label=document.createElement('span');label.textContent=DATA.metrics.find(m=>m.id===id)?.name||id;const output=document.createElement('b');output.textContent=Number.isFinite(value)?value.toFixed(3):JSON.stringify(value);row.append(label,output);list.appendChild(row)}
 else {const row=document.createElement('p');row.textContent=perspective==='god'?'尚无裁判指标记录':'裁判指标在双方上帝视角查看';list.appendChild(row)}
 const inbox=$('task-message-list');inbox.replaceChildren();
 for(const m of messages.slice(-5).reverse()){const row=document.createElement('div');row.className='task-message';const title=document.createElement('b');title.textContent=fmt(m.tick)+' · '+(m.body.request_id||m.body.schema_version||'消息');const detail=document.createElement('p');detail.textContent=String(m.sender)+' → '+String(m.controller)+(m.body.status?' · '+m.body.status:'')+(m.trustedSource?' · 已声明可信源':'');const body=document.createElement('details');const summary=document.createElement('summary');summary.textContent='原始消息';const pre=document.createElement('pre');pre.textContent=m.payload;body.append(summary,pre);row.append(title,detail,body);inbox.appendChild(row)}
 if(!messages.length){const row=document.createElement('p');row.textContent='当前未记录到已投递消息；无需报告的任务可能全程没有消息。';inbox.appendChild(row)}
 for(const row of $('task-zone-list').children)row.hidden=t<Number(row.dataset.tick)||(perspective!=='god'&&perspective!==participantSide());
}
