/* Map manipulation and post-game observation comparison. No hidden contacts inferred. */
'use strict';
let perspective='god',dragMode='pan',frameMode=false,smoothDisplay=false;
function participantSide(d=DATA){return d.meta.participantSide||'red'}
function sideName(side,d=DATA){if(side==='neutral')return '第三方观测浮标';if(!d.meta.track||d.meta.track==='interception')return side==='blue'?'蓝方（走私方）':'红方（海警）';const role=d.meta.factionConvention?.roles?.[side];const label={reconnaissance:'巡查',responder:'应急响应',environment:'环境',opposing_targets:'目标'}[role];return (side==='blue'?'蓝方':'红方')+(label?'（'+label+'）':'')}
let perspectiveOwn=new Map(),perspectiveContacts=[],perspectiveTick=null;
let perspectiveCache='',contactLayer=null,contactLabelEntries=[];
let viewHistoryCache='',viewHistories=new Map();
const pointerPositions=new Map();
const tickCache=new WeakMap(),missileIdCache=new WeakMap(),legacyViewCache=new WeakMap();
const detectionCache=new WeakMap(),trajectoryCache=new WeakMap();
async function inflateEpisode(text){const bytes=Uint8Array.from(atob(text),c=>c.charCodeAt(0));return JSON.parse(await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text())}

function snapshotsFor(side=perspective){
 if(DATA.perspectives?.[side])return DATA.perspectives[side];
 if(DATA.meta.observationScope==='own-samples-only'){
  if(!legacyViewCache.has(DATA)){
   const views={blue:[],red:[]};
   for(const side of ['blue','red']){const byTick=new Map();for(const e of DATA.entities){if(e.side!==side||e.source!=='native')continue;for(const s of e.samples){if(s[0]>(e.lastNativeTick??s[0])||s.length<8)continue;if(!byTick.has(s[0]))byTick.set(s[0],{tick:s[0],own:[],contacts:[]});byTick.get(s[0]).own.push({entity_id:e.id,position_m:s.slice(1,4),heading_deg:s[4],health:s[5],ammunition:{total:s[6]},lifecycle_state:s[7]})}}views[side]=[...byTick.values()].sort((a,b)=>a.tick-b.tick)}
   legacyViewCache.set(DATA,views);
  }return legacyViewCache.get(DATA)[side]||[];
 }
 return DATA.recording?.views?.[side]||[];
}
function tickSeconds(){return Number(DATA.recording?.metadata.tick_seconds||DATA.meta.nativeTickSeconds||1)}
function priorIndex(items,t,key=x=>x.tick){let lo=0,hi=items.length-1;if(!items.length||key(items[0])>t)return -1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(key(items[mid])<=t)lo=mid;else hi=mid-1}return lo}
function replayTicks(){if(!tickCache.has(DATA))tickCache.set(DATA,DATA.recording?.ticks||Array.from(new Set([0,END,...DATA.entities.filter(e=>e.source!=='anchors').flatMap(e=>e.samples.map(s=>s[0]))])).sort((a,b)=>a-b));return tickCache.get(DATA)}
function recordedMissileIds(){if(!missileIdCache.has(DATA))missileIdCache.set(DATA,new Set((DATA.recording?.frames||[]).flatMap(f=>(f.projectiles||[]).map(p=>p.missile_id))));return missileIdCache.get(DATA)}
function displayTime(){if(!frameMode)return time;const ticks=replayTicks(),i=priorIndex(ticks,time,x=>x);return i<0?time:ticks[i]}
function visibleInPerspective(e){return (perspective==='god'||e.side===perspective)&&truthEntityPresent(e)}
function displayedSample(e,t=time){
 return visibleInPerspective(e)?sampleAt(e,frameMode?displayTime():t):null;
}
function displayedState(e){return lifeAt(e,displayTime())}
function ownFleet(){return DATA.entities.filter(e=>e.side===perspective&&truthEntityPresent(e))}
function measurementTick(c,snapshotTick){return Number.isFinite(c.observed_tick)?c.observed_tick:snapshotTick-(Number.isFinite(c.age_ticks)?c.age_ticks:0)}
function detectionTimeline(side){
 if(!detectionCache.has(DATA))detectionCache.set(DATA,{});
 const cache=detectionCache.get(DATA);if(cache[side])return cache[side];
 const samples=new Map(),ticks=new Map();
 for(const snapshot of snapshotsFor(side))for(const c of snapshot.contacts||[]){
  const measured=measurementTick(c,snapshot.tick),available=snapshot.tick;
  if(!Number.isFinite(measured)||!Number.isFinite(available)||measured<0||measured>available||c.detected===false)continue;
  // A sensor measurement is usable only after it first enters this controller's
  // authorized observation. Never move later arrivals back to their sensor tick.
  // Repeated stale copies do not create new detections or extend the blink.
  const key=JSON.stringify([c.replay_contact_key||c.contact_id,measured]);
  const previous=samples.get(key);
  if(!previous||available<previous.replay_available_tick)samples.set(key,{...c,replay_measurement_tick:measured,replay_available_tick:available});
 }
 for(const c of samples.values()){const tick=c.replay_available_tick;if(!ticks.has(tick))ticks.set(tick,[]);ticks.get(tick).push(c)}
 return cache[side]=[...ticks].sort((a,b)=>a[0]-b[0]).map(([tick,contacts])=>({tick,contacts}));
}
function contactsAt(side,t){const timeline=detectionTimeline(side),i=priorIndex(timeline,t);return i>=0&&t-timeline[i].tick<1-1e-8?timeline[i].contacts:[]}
function setSmoothing(value){smoothDisplay=!!value;$('smooth-display').setAttribute('aria-pressed',String(smoothDisplay));$('smooth-display').textContent=smoothDisplay?'平滑处理：开':'平滑处理：关';perspectiveCache='';for(const e of DATA.entities)updateTrail(e,meshes.get(e.id),time);updateEntities();updateShots();updateUI(true)}
function stopCameraAutomation(){following=false;tour=false;$('follow').setAttribute('aria-pressed','false');$('tour').checked=false}
function setDragMode(value){dragMode=value;for(const mode of ['pan','orbit'])$('drag-'+mode).setAttribute('aria-pressed',String(mode===value));renderScene.dataset.dragMode=value}
function setPerspective(value){
 if(DATA.meta.participantOnly)value=participantSide();
 if(!['blue','red','god'].includes(value))return;
 perspective=value;perspectiveCache='';viewHistoryCache='';stopCameraAutomation();
 // Hold the current view, including when a previous camera move is still easing.
 targetOrbit.center.copy(orbit.center);targetOrbit.theta=orbit.theta;targetOrbit.phi=orbit.phi;targetOrbit.span=orbit.span;
 document.body.classList.toggle('faction-view',value!=='god');
 for(const side of ['blue','red','god'])$('view-'+side).setAttribute('aria-pressed',String(side===value));
 refreshPerspective();
 if(value!=='god'&&(!E.has(selected)||!visibleInPerspective(E.get(selected))))selected=ownFleet()[0]?.id||null;
 updateEntities();updateUI(true);updateShots();for(const e of DATA.entities)updateTrail(e,meshes.get(e.id),time);
}
function clearContactMarkers(){
 if(contactLayer){contactLayer.traverse(o=>{o.geometry?.dispose();if(o.material)o.material.dispose()});world?.remove(contactLayer);contactLayer=null}
 for(const {label} of contactLabelEntries)label.remove();
 contactLabelEntries=[];
}
function refreshPerspective(){
 const observationSide=perspective==='god'&&DATA.meta.observationScope==='participant-trace'?participantSide():perspective;
 const snapshots=snapshotsFor(observationSide),index=observationSide==='god'?-1:priorIndex(snapshots,displayTime());
 const detections=observationSide==='god'?[]:contactsAt(observationSide,displayTime());
 const cache=episodeIndex+'|'+perspective+'|'+index+'|'+detections.map(c=>c.replay_contact_key+':'+c.replay_measurement_tick+':'+c.replay_available_tick).join(',');
 if(cache===perspectiveCache)return;
 perspectiveCache=cache;clearContactMarkers();
 const snapshot=index>=0?snapshots[index]:null;
 perspectiveOwn=new Map((snapshot?.own||[]).map(o=>[o.entity_id,o]));perspectiveContacts=detections;perspectiveTick=snapshot?.tick??null;
 if(perspective!=='god'&&selected&&(!E.has(selected)||!visibleInPerspective(E.get(selected))))selected=ownFleet()[0]?.id||null;
 let note;
 if(perspective==='god'&&DATA.meta.observationScope==='participant-trace')note='历史授权观察 · 己方逐 tick 位置；蓝色为实际接触估计，未补造目标真值轨迹 · 原调优版本，非整合版重跑';
 else if(perspective==='god')note=(DATA.recording?'双方逐 tick 记录':'历史回放 · 虚线为稀疏锚点示意')+' · 轨迹连接记录点；平滑仅改变显示';
 else note=sideName(perspective)+'全队 '+ownFleet().length+' 个单位 / 新到探测 '+perspectiveContacts.length+' 条报告 · 阵营各控制器观测汇总，非单机全知 · 按首次收到时间闪现，不提前显示、不延用旧报告 · 显示本方发射'+(!(DATA.perspectives?.[perspective]?.length||DATA.recording?.views?.[perspective]?.length)?(DATA.recording?' · 本场景没有该方控制器观测':' · 此旧局缺少该方敌情记录，不能据此判断未发现敌人'):'')+(ownFleet().some(e=>e.source==='anchors')?' · 本方部分位置仅有锚点，虚线示意':'');
 $('perspective-note').textContent=note;
 if(observationSide==='god')return;
 contactLayer=new THREE.Group();world.add(contactLayer);
 const autonomous=DATA.engagement?.contract?.schema_version==='autonomous-contact-judgment@2';
 if(autonomous){
  const histories=new Map(),now=displayTime();
  for(let i=Math.max(0,index-1200);i<=index;i++)for(const c of snapshots[i]?.contacts||[]){
   if(c.observed_tick>now)continue;
   const key=c.replay_contact_key||c.contact_id;if(!histories.has(key))histories.set(key,new Map());histories.get(key).set(c.observed_tick,c.estimated_position_m);
  }
  for(const samples of histories.values()){
   const ordered=[...samples].sort((a,b)=>a[0]-b[0]),stride=Math.max(1,Math.floor(ordered.length/120));
   const points=ordered.filter((_,i)=>i%stride===0).map(p=>point(p[1]));
   if(points.length>1)contactLayer.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),new THREE.LineBasicMaterial({color:0x9cb5bb,transparent:true,opacity:.38})));
  }
 }
 // Aggregate exact coincident reports for drawing only, without fusing identities.
 const markers=new Map();for(const c of perspectiveContacts){const key=JSON.stringify([c.replay_measurement_tick,c.estimated_position_m]);if(markers.has(key))markers.get(key).replay_reports++;else markers.set(key,{...c,replay_reports:1})}
 for(const contact of markers.values()){
  const identifiedBuoy=contact.identification?.status==='observation_buoy';
  const side=identifiedBuoy?'neutral':observationSide==='blue'?'red':'blue',air=contact.estimated_position_m[2]>15;
  const marker=autonomous?new THREE.Mesh(new THREE.SphereGeometry(.045,10,8),new THREE.MeshBasicMaterial({color:0x9cb5bb})):identifiedBuoy?makeBuoy():air?makePlane(side):makeBoat(side);marker.userData.contact=true;marker.userData.kind=air?'air':'surface';marker.position.copy(point(contact.estimated_position_m));
  if(Number.isFinite(contact.heading_deg))marker.rotation.y=-contact.heading_deg*Math.PI/180;
  marker.traverse(o=>{if(o.isMesh){o.material.transparent=true;o.material.opacity=.72}});contactLayer.add(marker);
  const label=document.createElement('div');label.className='contact-label';label.textContent=(air?'空中':'水面')+'触点 · '+(contact.contact_id||'').slice(-6)+(contact.replay_reports>1?' ×'+contact.replay_reports:'');label.title='传感器估计位置；外形按估计高度示意，身份未识别。测量 tick '+contact.replay_measurement_tick+'；本控制器首次收到 tick '+contact.replay_available_tick+'；'+contact.replay_reports+' 条完全重合的报告合并绘制，不合并身份。';$('labels').appendChild(label);
  contactLabelEntries.push({marker,label});
  if(contact.identification?.status&&contact.identification.status!=='unknown'){
   const identity=identifiedBuoy?'已识别观测浮标':'已识别武装艇';
   label.textContent=identity+' · '+(contact.contact_id||'').slice(-6);
   label.title='实际送达的光电设备特征：'+contact.identification.observed_features?.equipment+'；证据 tick '+contact.identification.evidence_tick;
   if(identifiedBuoy){label.style.color='#e9ce91';label.style.borderColor='#665f3f'}
  }
 }
}
function updateContactLabels(){
 const occupied=[];
 for(const {marker,label} of contactLabelEntries){
  vec.copy(marker.position);vec.y+=.25;vec.project(camera);
  const x=(vec.x*.5+.5)*W,y=(-vec.y*.5+.5)*H;
  label.style.display='none';
  if(Math.abs(vec.z)>1||x<65||x>W-65||y<90||y>H-65)continue;
  let top=y;
  const overlaps=()=>occupied.some(p=>Math.abs(p.x-x)<130&&Math.abs(p.y-top)<21);
  for(let attempt=0;attempt<3&&overlaps();attempt++)top-=22;
  if(top<90||overlaps())continue;
  occupied.push({x,y:top});
  label.style.display='block';label.style.left=x+'px';label.style.top=top+'px';
 }
}
function nativeFrame(){const frames=DATA.recording?.frames||[];const i=priorIndex(frames,displayTime());return i<0?null:frames[i]}
function truthEntityPresent(e){const frame=nativeFrame();return frame?frame.entities.some(x=>x.entity_id===e.id):displayTime()>=e.spawn}
function recordedMissilePoint(shot){const frames=DATA.recording?.frames;if(!frames)return null;const i=priorIndex(frames,displayTime());if(i<0)return null;const a=frames[i].projectiles?.find(p=>p.missile_id===shot.id);if(!a)return null;const p=point(a.position_m);if(!frameMode&&smoothDisplay&&i+1<frames.length){const b=frames[i+1].projectiles?.find(p=>p.missile_id===shot.id);if(b)p.lerp(point(b.position_m),(time-frames[i].tick)/(frames[i+1].tick-frames[i].tick))}return p}
function stepFrame(direction){setPlaying(false);stopCameraAutomation();frameMode=true;$('frame-mode').checked=true;const ticks=replayTicks();const i=direction>0?ticks.findIndex(t=>t>time+1e-8):priorIndex(ticks,time-1e-8,x=>x);if(i>=0)seek(ticks[i]);updateFrameTools()}
function updateFrameTools(){const ticks=replayTicks(),i=priorIndex(ticks,time,x=>x);$('frame-position').textContent=(DATA.recording?'原始帧 ':'观测样本 ')+(Math.max(i,0)+1)+' / '+ticks.length+' · tick '+(i<0?0:ticks[i]);$('frame-prev').disabled=time<=ticks[0];$('frame-next').disabled=time>=ticks.at(-1);$('export-frames').disabled=!DATA.recording;$('export-frames').title=DATA.recording?'导出当前局原始逐 tick 记录（含赛后真值与双方观测）':'历史日志没有逐 tick 真值，无法补造导出';$('tour').disabled=perspective!=='god'}
function downloadFrames(){if(!DATA.recording)return;const blob=new Blob(DATA.recording.frames.map(f=>JSON.stringify(f)+'\n'),{type:'application/x-ndjson;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=DATA.meta.scenario+'-'+String(DATA.meta.seed)+'-replay.frames.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function panMap(dx,dy){
 const units=(camera.right-camera.left)/W,theta=orbit.theta,vertical=units/Math.max(.08,Math.cos(orbit.phi));
 targetOrbit.center.x-=dx*units*Math.cos(theta)+dy*vertical*Math.sin(theta);
 targetOrbit.center.z+=dx*units*Math.sin(theta)-dy*vertical*Math.cos(theta);
}
function setupMapPointer(){
 const surface=viewport;surface.style.touchAction='none';renderScene.dataset.dragMode=dragMode;
 surface.addEventListener('contextmenu',e=>e.preventDefault());
 const pinch=()=>{const p=[...pointerPositions.values()];return {x:(p[0].x+p[1].x)/2,y:(p[0].y+p[1].y)/2,distance:Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y)}};
 let gesture=null;
 surface.addEventListener('pointerdown',e=>{if(e.target.closest('button,input,select,a,.finish')&&!e.target.closest('.entity-label'))return;if(![0,1,2].includes(e.button)&&e.pointerType!=='touch')return;pointerPositions.set(e.pointerId,{x:e.clientX,y:e.clientY});surface.setPointerCapture(e.pointerId);drag={id:e.pointerId,x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,button:e.button,entityId:e.target.closest('.entity-label')?.dataset.entityId};wasDrag=false;if(pointerPositions.size===2){gesture=pinch();wasDrag=true;stopCameraAutomation()}renderScene.classList.add('dragging')});
 surface.addEventListener('pointermove',e=>{
  if(!pointerPositions.has(e.pointerId))return;pointerPositions.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pointerPositions.size>=2){const next=pinch();if(gesture){panMap(next.x-gesture.x,next.y-gesture.y);targetOrbit.span=Math.max(isCombat()?4:1.2,Math.min(250,targetOrbit.span*gesture.distance/Math.max(1,next.distance)))}gesture=next;wasDrag=true;stopCameraAutomation();return}
  if(!drag||drag.id!==e.pointerId)return;
  const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)>3)wasDrag=true;
  if(wasDrag){stopCameraAutomation();const rotate=drag.button===2||e.shiftKey||(drag.button===0&&dragMode==='orbit');
   if(rotate){targetOrbit.theta-=dx*.006;targetOrbit.phi=Math.max(.001,Math.min(1.2,targetOrbit.phi+dy*.005));$('oblique').setAttribute('aria-pressed',String(targetOrbit.phi>.1));$('topdown').setAttribute('aria-pressed',String(targetOrbit.phi<=.1))}else panMap(dx,dy);
  }drag.x=e.clientX;drag.y=e.clientY;
 });
 const finish=e=>{if(!pointerPositions.has(e.pointerId))return;const picked=!wasDrag&&e.button===0&&e.type==='pointerup';pointerPositions.delete(e.pointerId);if(picked&&drag?.entityId)selectEntity(drag.entityId);else if(picked){const bounds=renderScene.getBoundingClientRect();raycaster.setFromCamera(new THREE.Vector2((e.clientX-bounds.left)/W*2-1,-(e.clientY-bounds.top)/H*2+1),camera);const hit=raycaster.intersectObjects([...meshes.values()].filter(m=>m.g.visible).map(m=>m.g),true)[0];if(hit){let o=hit.object;while(o&&!o.userData.entityId)o=o.parent;if(o)selectEntity(o.userData.entityId)}}gesture=null;drag=null;renderScene.classList.remove('dragging');if(surface.hasPointerCapture(e.pointerId))surface.releasePointerCapture(e.pointerId);if(pointerPositions.size===1){const [id,p]=pointerPositions.entries().next().value;drag={id,x:p.x,y:p.y,startX:p.x,startY:p.y,button:0};wasDrag=true}};
 for(const type of ['pointerup','pointercancel','lostpointercapture'])surface.addEventListener(type,finish);
 surface.addEventListener('wheel',e=>{e.preventDefault();stopCameraAutomation();targetOrbit.span=Math.max(isCombat()?4:1.2,Math.min(Math.max(250,DATA.meta.overview.span*2),targetOrbit.span*Math.exp(Math.max(-.5,Math.min(.5,e.deltaY*.001)))))},{passive:false});
}
function mountReplayControls(){$('smooth-display').addEventListener('click',()=>setSmoothing(!smoothDisplay));for(const side of ['blue','red','god'])$('view-'+side).addEventListener('click',()=>setPerspective(side));for(const mode of ['pan','orbit'])$('drag-'+mode).addEventListener('click',()=>setDragMode(mode));$('frame-prev').addEventListener('click',()=>stepFrame(-1));$('frame-next').addEventListener('click',()=>stepFrame(1));$('frame-mode').addEventListener('change',e=>{frameMode=e.target.checked;if(frameMode){stopCameraAutomation();seek(displayTime())}perspectiveCache=''});$('export-frames').addEventListener('click',downloadFrames);setDragMode('pan');updateFrameTools();document.addEventListener('keydown',e=>{if(document.querySelector('dialog[open]')||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName))return;if(e.key===','||e.key==='.'){e.preventDefault();stepFrame(e.key==='.'?1:-1)}})}
